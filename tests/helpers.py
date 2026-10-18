"""Small helpers shared by the integration tests."""

from __future__ import annotations

import time
from typing import Any

from lnd.gateway import LLMGateway
from lnd.pipeline import finalize_dead, process_job
from lnd.queue import JobQueue
from lnd.worker import make_queue

from .conftest import make_pdf, make_png


def upload(api: Any, data: bytes, name: str, tenant: str = "default") -> dict[str, Any]:
    res = api.post("/api/documents", files={"file": (name, data)}, headers={"X-Tenant": tenant})
    assert res.status_code == 202, res.text
    return res.json()


def upload_image(api: Any, tenant: str = "default", label: str = "page") -> dict[str, Any]:
    return upload(api, make_png(label), f"{label}.png", tenant)


def upload_pdf(api: Any, pages: int, tenant: str = "default") -> dict[str, Any]:
    return upload(api, make_pdf(pages), "book.pdf", tenant)


def drain(
    queue: JobQueue,
    gateway: LLMGateway,
    sandbox: Any,
    *,
    timeout: float = 20.0,
) -> list[str]:
    """Run jobs until nothing is ready, delayed or in flight. Returns the outcomes."""
    outcomes: list[str] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        dead = queue.reap()
        if dead:
            finalize_dead(dead)
        job = queue.pop(60)
        if job is None:
            stats = queue.stats()
            if stats["ready"] == 0 and stats["delayed"] == 0 and stats["inflight"] == 0:
                return outcomes
            time.sleep(0.02)
            continue
        outcomes.append(process_job(job, queue, gateway, sandbox))
    raise AssertionError(f"queue did not drain: {queue.stats()}")


def events_of(api: Any, doc_id: str, tenant: str = "default") -> list[dict[str, Any]]:
    from lnd.db import connection

    with connection() as conn:
        return conn.execute(
            "SELECT seq, type, data FROM events WHERE document_id = %s ORDER BY seq", (doc_id,)
        ).fetchall()


__all__ = ["drain", "events_of", "make_queue", "upload", "upload_image", "upload_pdf"]
