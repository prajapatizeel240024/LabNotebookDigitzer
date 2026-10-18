"""Progress events stored in Postgres, with an id that is safe to resume from.

Why ids are per document and assigned under a row lock: a global BIGSERIAL can commit out
of order (id 6 becomes visible before id 5), so a client that resumes from "last seen = 6"
would miss 5 forever. Here every event is created by first bumping documents.last_seq,
which locks that row until the transaction commits. Events for one document therefore
commit in id order and the ids have no gaps. "Give me everything after N" is then exact.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg


def emit(conn: psycopg.Connection, document_id: str, type_: str, data: dict[str, Any]) -> int:
    """Add an event inside the caller's transaction. Returns its seq."""
    row = conn.execute(
        "UPDATE documents SET last_seq = last_seq + 1 WHERE id = %s RETURNING last_seq",
        (document_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"document {document_id} not found")
    seq = int(row["last_seq"])
    conn.execute(
        "INSERT INTO events (document_id, seq, type, data) VALUES (%s, %s, %s, %s::jsonb)",
        (document_id, seq, type_, json.dumps(data)),
    )
    return seq


def fetch_after(
    conn: psycopg.Connection, document_id: str, after_seq: int, limit: int = 500
) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT seq, type, data FROM events WHERE document_id = %s AND seq > %s "
        "ORDER BY seq LIMIT %s",
        (document_id, after_seq, limit),
    ).fetchall()


def format_sse(seq: int, type_: str, data: dict[str, Any]) -> str:
    """One Server-Sent Event frame. The id line is what a browser sends back as Last-Event-ID."""
    return f"id: {seq}\nevent: {type_}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
