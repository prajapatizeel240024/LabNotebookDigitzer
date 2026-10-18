"""HTTP API and the review UI.

Bound to 127.0.0.1 by the compose file. There is no login: the X-Tenant header names the
tenant so usage can be metered per tenant, but it is not authentication. See DESIGN.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import events, exports, store
from .checks import run_code
from .config import get_settings
from .db import close_pool, connection, ensure_tenant, init_schema
from .gateway import usage_summary
from .ingest import (
    BadUpload,
    normalize_image,
    pdf_page_count,
    sniff_kind,
    write_atomic,
)
from .queue import JobQueue, connect
from .sandbox_client import SandboxClient
from .schemas import BudgetUpdate, RecordUpdate

log = logging.getLogger("lnd.api")

TENANT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
MAX_IMAGE_UPLOAD = 50 * 1024 * 1024
POLL_S = 0.25
KEEPALIVE_S = 15.0


def tenant_dep(
    x_tenant: str | None = Header(default=None),
    tenant: str | None = Query(default=None),
) -> str:
    """Tenant from the X-Tenant header, or ?tenant= for things a browser fetches without
    custom headers (images, downloads, EventSource). Falls back to "default"."""
    name = x_tenant or tenant or "default"
    if not TENANT_RE.match(name):
        raise HTTPException(400, "tenant must be 1-64 characters of letters, digits, _ . -")
    return name


def _uuid(value: str) -> str:
    if not UUID_RE.match(value):
        raise HTTPException(404, "not found")
    return value


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        init_schema()
        app.state.queue = JobQueue(
            connect(settings.redis_url),
            max_attempts=settings.max_attempts,
            backoff_base_s=settings.backoff_base_s,
            backoff_cap_s=settings.backoff_cap_s,
        )
        app.state.sandbox = SandboxClient()
        yield
        close_pool()

    app = FastAPI(title="Lab Notebook Digitizer", version="0.1.0", lifespan=lifespan)

    def queue_of(request: Request) -> JobQueue:
        return request.app.state.queue

    # ---- health and queue ------------------------------------------------------------------
    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        with connection() as conn:
            conn.execute("SELECT 1")
        queue_of(request).r.ping()
        return {"status": "ok"}

    @app.get("/api/queue")
    def queue_stats(request: Request) -> dict[str, Any]:
        q = queue_of(request)
        return {**q.stats(), "dead_jobs": q.dead_jobs(20)}

    # ---- documents -------------------------------------------------------------------------
    @app.post("/api/documents", status_code=202)
    def upload(
        request: Request,
        file: UploadFile = File(...),
        tenant: str = Depends(tenant_dep),
    ) -> dict[str, Any]:
        doc_id = str(uuid.uuid4())
        folder = settings.data_dir / "docs" / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        try:
            head = file.file.read(16)
            kind = sniff_kind(head)
            file.file.seek(0)
            if kind == "pdf":
                rel = Path("docs") / doc_id / "source.pdf"
                limit = settings.max_upload_bytes
                written = 0
                with open(settings.data_dir / rel, "wb") as out:
                    while chunk := file.file.read(1024 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise HTTPException(
                                413, f"file is larger than {settings.max_upload_mb} MB"
                            )
                        out.write(chunk)
                page_count = pdf_page_count(settings.data_dir / rel)
            else:
                raw = file.file.read(MAX_IMAGE_UPLOAD + 1)
                if len(raw) > MAX_IMAGE_UPLOAD:
                    raise HTTPException(413, "image is larger than 50 MB")
                data, mime = normalize_image(raw, settings.max_image_px)
                rel = Path("docs") / doc_id / ("page.png" if mime == "image/png" else "page.jpg")
                write_atomic(settings.data_dir / rel, data)
                page_count = 1
            jobs = store.create_document(
                doc_id=doc_id,
                tenant=tenant,
                filename=(file.filename or "upload")[:200],
                kind=kind,
                source_path=str(rel),
                page_count=page_count,
            )
        except BadUpload as exc:
            shutil.rmtree(folder, ignore_errors=True)
            raise HTTPException(400, str(exc)) from exc
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        queue_of(request).enqueue_many(jobs)
        return {"id": doc_id, "kind": kind, "page_count": page_count, "status": "processing"}

    @app.get("/api/documents")
    def documents(tenant: str = Depends(tenant_dep)) -> list[dict[str, Any]]:
        return store.list_documents(tenant)

    def _doc_or_404(doc_id: str, tenant: str) -> dict[str, Any]:
        doc = store.get_document(_uuid(doc_id), tenant)
        if doc is None:
            raise HTTPException(404, "document not found")
        return doc

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str, tenant: str = Depends(tenant_dep)) -> dict[str, Any]:
        doc = _doc_or_404(doc_id, tenant)
        return {**doc, "pages": store.list_pages(doc_id)}

    @app.post("/api/documents/{doc_id}/requeue")
    def requeue(doc_id: str, request: Request, tenant: str = Depends(tenant_dep)) -> dict[str, Any]:
        """Re-enqueue pages still marked queued (recovery if the enqueue after upload failed)."""
        _doc_or_404(doc_id, tenant)
        jobs = store.queued_page_jobs(doc_id)
        queue_of(request).enqueue_many(jobs)
        return {"enqueued": len(jobs)}

    @app.get("/api/documents/{doc_id}/export.json")
    def export_json(doc_id: str, tenant: str = Depends(tenant_dep)) -> Response:
        doc = _doc_or_404(doc_id, tenant)
        body = exports.to_json(doc, store.export_rows(doc_id))
        return JSONResponse(
            body, headers={"Content-Disposition": f'attachment; filename="{doc_id}.json"'}
        )

    @app.get("/api/documents/{doc_id}/export.csv")
    def export_csv(doc_id: str, tenant: str = Depends(tenant_dep)) -> Response:
        _doc_or_404(doc_id, tenant)
        return Response(
            exports.to_csv(store.export_rows(doc_id)),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{doc_id}.csv"'},
        )

    # ---- progress stream -------------------------------------------------------------------
    @app.get("/api/documents/{doc_id}/events")
    async def stream_events(
        doc_id: str,
        request: Request,
        tenant: str = Depends(tenant_dep),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after: int | None = Query(default=None, ge=0),
    ) -> StreamingResponse:
        await run_in_threadpool(_doc_or_404, doc_id, tenant)
        cursor = after or 0
        if last_event_id is not None:
            try:
                cursor = max(0, int(last_event_id))
            except ValueError:
                cursor = 0

        def fetch(c: int) -> tuple[list[dict[str, Any]], int, bool]:
            with connection() as conn:
                rows = events.fetch_after(conn, doc_id, c)
                doc = conn.execute(
                    "SELECT last_seq, status FROM documents WHERE id = %s", (doc_id,)
                ).fetchone()
            return rows, int(doc["last_seq"]), doc["status"] == "completed"

        async def gen() -> AsyncIterator[str]:
            nonlocal cursor
            yield "retry: 1000\n\n"
            quiet = 0.0
            while True:
                if await request.is_disconnected():
                    return
                rows, last_seq, completed = await run_in_threadpool(fetch, cursor)
                for r in rows:
                    cursor = int(r["seq"])
                    yield events.format_sse(cursor, r["type"], r["data"])
                # The document is finished and the client has everything: end the stream, so
                # a client that reconnects after document_done does not wait forever.
                if completed and cursor >= last_seq:
                    return
                if rows:
                    quiet = 0.0
                    continue
                await asyncio.sleep(POLL_S)
                quiet += POLL_S
                if quiet >= KEEPALIVE_S:
                    quiet = 0.0
                    yield ": keepalive\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- pages: image, record, history -----------------------------------------------------
    def _page_or_404(page_id: str, tenant: str) -> dict[str, Any]:
        page = store.get_page_for_tenant(_uuid(page_id), tenant)
        if page is None:
            raise HTTPException(404, "page not found")
        return page

    @app.get("/api/pages/{page_id}/image")
    def page_image(page_id: str, tenant: str = Depends(tenant_dep)) -> FileResponse:
        page = _page_or_404(page_id, tenant)
        if not page["image_path"]:
            raise HTTPException(404, "page image is not ready yet")
        path = settings.data_dir / page["image_path"]
        if not path.exists():
            raise HTTPException(404, "page image is not ready yet")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/pages/{page_id}/record")
    def get_record(page_id: str, tenant: str = Depends(tenant_dep)) -> dict[str, Any]:
        page = _page_or_404(page_id, tenant)
        rec = store.get_record(page_id)
        return {
            "page": {k: page[k] for k in ("id", "page_no", "status", "error")},
            "record": rec["current"] if rec else None,
            "checks": rec["checks"] if rec else None,
        }

    @app.put("/api/pages/{page_id}/record")
    def put_record(
        page_id: str,
        body: RecordUpdate,
        request: Request,
        tenant: str = Depends(tenant_dep),
    ) -> dict[str, Any]:
        _page_or_404(page_id, tenant)
        data = body.data.model_dump()
        version = store.add_user_version(page_id, data, body.editor, body.note)
        if version is None:
            raise HTTPException(409, "this page has no extracted record to edit yet")
        checks = None
        code = store.get_check_code(page_id)
        if code and data["reagents"]:
            checks = run_code(request.app.state.sandbox, code, data["reagents"])
            store.save_check_result(page_id, checks)
        return {"version": version, "checks": checks}

    @app.get("/api/pages/{page_id}/versions")
    def versions(page_id: str, tenant: str = Depends(tenant_dep)) -> list[dict[str, Any]]:
        _page_or_404(page_id, tenant)
        return store.list_versions(page_id)

    @app.get("/api/pages/{page_id}/versions/{version}")
    def one_version(
        page_id: str, version: int, tenant: str = Depends(tenant_dep)
    ) -> dict[str, Any]:
        _page_or_404(page_id, tenant)
        v = store.get_version(page_id, version)
        if v is None:
            raise HTTPException(404, "version not found")
        return v

    @app.post("/api/pages/{page_id}/retry")
    def retry_page(
        page_id: str, request: Request, tenant: str = Depends(tenant_dep)
    ) -> dict[str, Any]:
        page = _page_or_404(page_id, tenant)
        q = queue_of(request)
        if not store.reset_failed_page(page_id, str(page["document_id"])):
            raise HTTPException(409, "only failed pages can be retried")
        q.purge_dead(page_id)
        q.enqueue(
            {
                "page_id": page_id,
                "document_id": str(page["document_id"]),
                "page_no": page["page_no"],
                "tenant": tenant,
            }
        )
        return {"status": "queued"}

    # ---- usage and budgets -----------------------------------------------------------------
    @app.get("/api/tenants/{tenant}/usage")
    def usage(tenant: str) -> dict[str, Any]:
        if not TENANT_RE.match(tenant):
            raise HTTPException(400, "bad tenant name")
        return usage_summary(tenant)

    @app.put("/api/tenants/{tenant}/budget")
    def set_budget(tenant: str, body: BudgetUpdate) -> dict[str, Any]:
        if not TENANT_RE.match(tenant):
            raise HTTPException(400, "bad tenant name")
        with connection() as conn:
            ensure_tenant(conn, tenant)
            conn.execute(
                "UPDATE tenants SET token_budget = %s WHERE id = %s", (body.token_budget, tenant)
            )
        return usage_summary(tenant)

    # ---- UI --------------------------------------------------------------------------------
    static_dir = Path(str(resources.files("lnd").joinpath("static")))
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    return app


app = create_app()
