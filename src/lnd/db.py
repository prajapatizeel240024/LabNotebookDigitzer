"""Postgres access: one small connection pool per process, plus schema setup."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

_pool: ConnectionPool | None = None
_lock = threading.Lock()

# Arbitrary constant: serialises schema setup when several containers start at once.
_SCHEMA_LOCK_ID = 7_410_203


def get_pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                get_settings().database_url,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        return _pool


def close_pool() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection. Commits on success, rolls back on error."""
    with get_pool().connection() as conn:
        yield conn


def init_schema() -> None:
    sql = resources.files("lnd").joinpath("schema.sql").read_text()
    with connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_ID,))
        try:
            conn.execute(sql)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_ID,))


def ensure_tenant(conn: psycopg.Connection, tenant: str) -> None:
    budget = get_settings().default_token_budget
    conn.execute(
        "INSERT INTO tenants (id, token_budget) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (tenant, budget),
    )
