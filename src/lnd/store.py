"""All reads and writes of documents, pages, versions and events.

Anything that finishes a page takes the document row lock first. That serialises page
completions per document, which is what makes the per-document event ids gap-free and lets
"is the document done?" be answered correctly when two workers finish at the same moment.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg

from . import events
from .db import connection, ensure_tenant

TERMINAL = ("done", "failed")


# ---- documents --------------------------------------------------------------------------------
def create_document(
    *,
    doc_id: str,
    tenant: str,
    filename: str,
    kind: str,
    source_path: str,
    page_count: int,
) -> list[dict[str, str]]:
    """Insert the document and one page row per page. Returns the job payloads to enqueue."""
    jobs: list[dict[str, str]] = []
    with connection() as conn:
        ensure_tenant(conn, tenant)
        conn.execute(
            "INSERT INTO documents (id, tenant_id, filename, kind, source_path, page_count) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (doc_id, tenant, filename, kind, source_path, page_count),
        )
        for n in range(1, page_count + 1):
            page_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO pages (id, document_id, page_no, image_path) VALUES (%s,%s,%s,%s)",
                (page_id, doc_id, n, source_path if kind == "image" else None),
            )
            jobs.append(
                {
                    "page_id": page_id,
                    "document_id": doc_id,
                    "page_no": n,
                    "tenant": tenant,
                }
            )
        events.emit(
            conn, doc_id, "document_created", {"filename": filename, "page_count": page_count}
        )
    return jobs


def get_document(doc_id: str, tenant: str) -> dict[str, Any] | None:
    with connection() as conn:
        doc = conn.execute(
            "SELECT id, tenant_id, filename, kind, page_count, status, last_seq, created_at, "
            "completed_at FROM documents WHERE id = %s AND tenant_id = %s",
            (doc_id, tenant),
        ).fetchone()
        if doc is None:
            return None
        counts = conn.execute(
            "SELECT status, count(*) AS n FROM pages WHERE document_id = %s GROUP BY status",
            (doc_id,),
        ).fetchall()
    doc["pages_by_status"] = {s: 0 for s in ("queued", "processing", "done", "failed")}
    doc["pages_by_status"].update({c["status"]: int(c["n"]) for c in counts})
    return doc


def list_documents(tenant: str, limit: int = 100) -> list[dict[str, Any]]:
    with connection() as conn:
        return conn.execute(
            "SELECT d.id, d.filename, d.kind, d.page_count, d.status, d.created_at, "
            "count(p.*) FILTER (WHERE p.status = 'done') AS pages_done, "
            "count(p.*) FILTER (WHERE p.status = 'failed') AS pages_failed "
            "FROM documents d LEFT JOIN pages p ON p.document_id = d.id "
            "WHERE d.tenant_id = %s GROUP BY d.id ORDER BY d.created_at DESC LIMIT %s",
            (tenant, limit),
        ).fetchall()


def list_pages(doc_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        return conn.execute(
            "SELECT p.id, p.page_no, p.status, p.attempts, p.error, "
            "(SELECT max(version) FROM page_versions v WHERE v.page_id = p.id) AS version, "
            "(SELECT data->>'title' FROM page_versions v WHERE v.page_id = p.id "
            " ORDER BY version DESC LIMIT 1) AS title "
            "FROM pages p WHERE p.document_id = %s ORDER BY p.page_no",
            (doc_id,),
        ).fetchall()


# ---- pages ------------------------------------------------------------------------------------
def get_page_ctx(page_id: str) -> dict[str, Any] | None:
    """Everything a worker needs to process a page."""
    with connection() as conn:
        return conn.execute(
            "SELECT p.id, p.document_id, p.page_no, p.status, p.image_path, d.kind, "
            "d.source_path, d.tenant_id FROM pages p JOIN documents d ON d.id = p.document_id "
            "WHERE p.id = %s",
            (page_id,),
        ).fetchone()


def get_page_for_tenant(page_id: str, tenant: str) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            "SELECT p.id, p.document_id, p.page_no, p.status, p.error, p.image_path, d.tenant_id "
            "FROM pages p JOIN documents d ON d.id = p.document_id "
            "WHERE p.id = %s AND d.tenant_id = %s",
            (page_id, tenant),
        ).fetchone()


def set_image_path(page_id: str, image_path: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE pages SET image_path = %s WHERE id = %s", (image_path, page_id))


def mark_processing(page_id: str, attempt_no: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE pages SET status = 'processing', attempts = %s, updated_at = now() "
            "WHERE id = %s AND status <> 'done'",
            (attempt_no, page_id),
        )


def note_retry(page_id: str, error: str) -> None:
    """A failed attempt that will be retried: back to queued, with the error visible."""
    with connection() as conn:
        conn.execute(
            "UPDATE pages SET status = 'queued', error = %s, updated_at = now() "
            "WHERE id = %s AND status <> 'done'",
            (error[:500], page_id),
        )


def _counts(conn: psycopg.Connection, document_id: str) -> dict[str, int]:
    row = conn.execute(
        "SELECT count(*) AS total, count(*) FILTER (WHERE status = 'done') AS done, "
        "count(*) FILTER (WHERE status = 'failed') AS failed FROM pages WHERE document_id = %s",
        (document_id,),
    ).fetchone()
    return {"total": int(row["total"]), "done": int(row["done"]), "failed": int(row["failed"])}


def _maybe_complete(conn: psycopg.Connection, document_id: str, counts: dict[str, int]) -> None:
    if counts["done"] + counts["failed"] < counts["total"]:
        return
    row = conn.execute(
        "UPDATE documents SET status = 'completed', completed_at = now() "
        "WHERE id = %s AND status <> 'completed' RETURNING id",
        (document_id,),
    ).fetchone()
    if row is not None:
        events.emit(conn, document_id, "document_done", counts)


def _lock_document(conn: psycopg.Connection, document_id: str) -> None:
    conn.execute("SELECT 1 FROM documents WHERE id = %s FOR UPDATE", (document_id,))


def finalize_page_done(
    *,
    page_id: str,
    document_id: str,
    extraction: dict[str, Any],
    check_code: str | None,
    check_result: dict[str, Any] | None,
) -> bool:
    """Save the model's answer and announce it. Safe to call twice: the second call is a no-op.

    Returns True if this call did the work.
    """
    with connection() as conn:
        _lock_document(conn, document_id)
        row = conn.execute(
            "UPDATE pages SET status = 'done', error = NULL, updated_at = now() "
            "WHERE id = %s AND status <> 'done' RETURNING page_no",
            (page_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "INSERT INTO page_versions (page_id, version, source, data) "
            "VALUES (%s, 1, 'model', %s::jsonb) ON CONFLICT DO NOTHING",
            (page_id, json.dumps(extraction)),
        )
        if check_result is not None:
            conn.execute(
                "INSERT INTO page_checks (page_id, code, result) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (page_id) DO UPDATE SET code = EXCLUDED.code, "
                "result = EXCLUDED.result, updated_at = now()",
                (page_id, check_code, json.dumps(check_result)),
            )
        counts = _counts(conn, document_id)
        events.emit(
            conn,
            document_id,
            "page_done",
            {
                "page_no": row["page_no"],
                "title": extraction.get("title", ""),
                "reagents": len(extraction.get("reagents", [])),
                "check": _check_label(check_result),
                **counts,
            },
        )
        _maybe_complete(conn, document_id, counts)
    return True


def _check_label(result: dict[str, Any] | None) -> str:
    if result is None:
        return "none"
    if result.get("status") == "ok":
        return "passed" if result.get("passed") else "issues"
    return result.get("status", "error")


def finalize_page_failed(*, page_id: str, document_id: str, error: str) -> bool:
    with connection() as conn:
        _lock_document(conn, document_id)
        row = conn.execute(
            "UPDATE pages SET status = 'failed', error = %s, updated_at = now() "
            "WHERE id = %s AND status NOT IN ('done', 'failed') RETURNING page_no",
            (error[:500], page_id),
        ).fetchone()
        if row is None:
            return False
        counts = _counts(conn, document_id)
        events.emit(
            conn,
            document_id,
            "page_failed",
            {"page_no": row["page_no"], "error": error[:200], **counts},
        )
        _maybe_complete(conn, document_id, counts)
    return True


def reset_failed_page(page_id: str, document_id: str) -> bool:
    """Put a failed page back to queued so it can be retried by hand."""
    with connection() as conn:
        _lock_document(conn, document_id)
        row = conn.execute(
            "UPDATE pages SET status = 'queued', error = NULL, attempts = 0, updated_at = now() "
            "WHERE id = %s AND status = 'failed' RETURNING page_no",
            (page_id,),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE documents SET status = 'processing', completed_at = NULL WHERE id = %s",
            (document_id,),
        )
        events.emit(conn, document_id, "page_requeued", {"page_no": row["page_no"]})
    return True


def queued_page_jobs(document_id: str) -> list[dict[str, Any]]:
    """Pages still marked queued, as job payloads. Used to recover from a failed enqueue."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT p.id AS page_id, p.document_id, p.page_no, d.tenant_id AS tenant "
            "FROM pages p JOIN documents d ON d.id = p.document_id "
            "WHERE p.document_id = %s AND p.status = 'queued' ORDER BY p.page_no",
            (document_id,),
        ).fetchall()
    return [{**r, "page_id": str(r["page_id"]), "document_id": str(r["document_id"])} for r in rows]


# ---- records and history ----------------------------------------------------------------------
def get_record(page_id: str) -> dict[str, Any] | None:
    with connection() as conn:
        v = conn.execute(
            "SELECT version, source, data, editor, note, created_at FROM page_versions "
            "WHERE page_id = %s ORDER BY version DESC LIMIT 1",
            (page_id,),
        ).fetchone()
        if v is None:
            return None
        checks = conn.execute(
            "SELECT code, result, updated_at FROM page_checks WHERE page_id = %s", (page_id,)
        ).fetchone()
    return {"current": v, "checks": checks}


def list_versions(page_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        return conn.execute(
            "SELECT version, source, editor, note, created_at FROM page_versions "
            "WHERE page_id = %s ORDER BY version DESC",
            (page_id,),
        ).fetchall()


def get_version(page_id: str, version: int) -> dict[str, Any] | None:
    with connection() as conn:
        return conn.execute(
            "SELECT version, source, data, editor, note, created_at FROM page_versions "
            "WHERE page_id = %s AND version = %s",
            (page_id, version),
        ).fetchone()


def add_user_version(page_id: str, data: dict[str, Any], editor: str, note: str) -> int | None:
    """Save an edit as a new version. Returns the version number, or None if the page has no
    model answer yet (nothing to edit)."""
    with connection() as conn:
        conn.execute("SELECT 1 FROM pages WHERE id = %s FOR UPDATE", (page_id,))
        row = conn.execute(
            "SELECT max(version) AS v FROM page_versions WHERE page_id = %s", (page_id,)
        ).fetchone()
        if row["v"] is None:
            return None
        version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO page_versions (page_id, version, source, data, editor, note) "
            "VALUES (%s, %s, 'user', %s::jsonb, %s, %s)",
            (page_id, version, json.dumps(data), editor, note),
        )
    return version


def save_check_result(page_id: str, result: dict[str, Any]) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE page_checks SET result = %s::jsonb, updated_at = now() WHERE page_id = %s",
            (json.dumps(result), page_id),
        )


def get_check_code(page_id: str) -> str | None:
    with connection() as conn:
        row = conn.execute("SELECT code FROM page_checks WHERE page_id = %s", (page_id,)).fetchone()
    return row["code"] if row else None


def export_rows(doc_id: str) -> list[dict[str, Any]]:
    """Latest version of every page that has one, in page order."""
    with connection() as conn:
        return conn.execute(
            "SELECT p.page_no, p.status, v.version, v.source, v.data "
            "FROM pages p LEFT JOIN LATERAL ("
            "  SELECT version, source, data FROM page_versions "
            "  WHERE page_id = p.id ORDER BY version DESC LIMIT 1"
            ") v ON true WHERE p.document_id = %s ORDER BY p.page_no",
            (doc_id,),
        ).fetchall()


def get_events_cursor(doc_id: str) -> int:
    with connection() as conn:
        row = conn.execute("SELECT last_seq FROM documents WHERE id = %s", (doc_id,)).fetchone()
    return int(row["last_seq"]) if row else 0
