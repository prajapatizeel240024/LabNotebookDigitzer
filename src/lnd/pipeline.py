"""What a worker does with one page job.

Idempotent by construction: if the page is already done the job is just acknowledged, and
finishing a page is a no-op the second time. A crash at any point therefore only costs a
repeat of the work, never a duplicate result or a duplicate event.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import store
from .checks import run_checks
from .config import Settings, get_settings
from .extraction import extract_page
from .gateway import BudgetExceeded, LLMGateway
from .ingest import render_pdf_page, write_atomic
from .queue import Job, JobQueue
from .sandbox_client import SandboxClient

log = logging.getLogger("lnd.pipeline")


def _page_image(page: dict[str, Any], settings: Settings) -> tuple[bytes, str]:
    """Bytes and mime type of the page image, rendering a PDF page if it is not there yet."""
    data_dir = settings.data_dir
    if page["kind"] == "image":
        path = data_dir / page["image_path"]
        mime = "image/png" if path.suffix == ".png" else "image/jpeg"
        return path.read_bytes(), mime
    rel = Path("docs") / str(page["document_id"]) / "pages" / f"{page['page_no']:04d}.png"
    path = data_dir / rel
    if not path.exists():
        png = render_pdf_page(
            data_dir / page["source_path"],
            page["page_no"] - 1,
            settings.pdf_dpi,
            settings.max_image_px,
        )
        write_atomic(path, png)
    if not page.get("image_path"):
        store.set_image_path(str(page["id"]), str(rel))
    return path.read_bytes(), "image/png"


def process_job(
    job: Job,
    queue: JobQueue,
    gateway: LLMGateway,
    sandbox: SandboxClient,
    settings: Settings | None = None,
) -> str:
    """Run one job. Returns 'done', 'skipped', 'retry', 'dead' or 'budget'."""
    settings = settings or get_settings()
    page_id = job.data["page_id"]
    page = store.get_page_ctx(page_id)
    if page is None or page["status"] == "done":
        queue.ack(job)
        return "skipped"

    document_id = str(page["document_id"])
    tenant = page["tenant_id"]
    job_id = f"{document_id}:{page['page_no']}"
    store.mark_processing(page_id, job.attempts + 1)

    try:
        image, mime = _page_image(page, settings)
        extraction = extract_page(gateway, image=image, mime=mime, tenant=tenant, job_id=job_id)
        data = extraction.model_dump()

        check_code: str | None = None
        check_result: dict[str, Any] | None = None
        if data["reagents"]:
            try:
                check_code, check_result = run_checks(
                    gateway, sandbox, rows=data["reagents"], tenant=tenant, job_id=job_id
                )
            except BudgetExceeded:
                check_result = {"status": "skipped", "error": "token budget spent"}
            except Exception as exc:  # checks are best effort; the extraction is still good
                log.warning("check failed for %s: %s", job_id, exc)
                check_result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:500]}

        store.finalize_page_done(
            page_id=page_id,
            document_id=document_id,
            extraction=data,
            check_code=check_code,
            check_result=check_result,
        )
        queue.ack(job)
        return "done"
    except BudgetExceeded as exc:
        # Retrying cannot help until the budget is raised, so do not burn attempts on it.
        store.finalize_page_failed(
            page_id=page_id, document_id=document_id, error=f"budget_exceeded: {exc}"
        )
        queue.ack(job)
        return "budget"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        outcome = queue.fail(job, error)
        if outcome == "dead":
            store.finalize_page_failed(
                page_id=page_id,
                document_id=document_id,
                error=f"gave up after {queue.max_attempts} attempts: {error}",
            )
        else:
            store.note_retry(page_id, error)
        log.warning("job %s failed (%s): %s", job_id, outcome, error)
        return outcome


def finalize_dead(jobs: list[dict[str, Any]]) -> None:
    """Jobs the reaper moved to the dead-letter list because their worker vanished."""
    for j in jobs:
        store.finalize_page_failed(
            page_id=j["page_id"],
            document_id=j["document_id"],
            error=f"gave up after {j['attempts']} attempts: {j.get('last_error', 'unknown')}",
        )
