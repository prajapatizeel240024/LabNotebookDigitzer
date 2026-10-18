"""Shared fixtures.

Integration tests need Postgres and Redis. Point them at your own with
LND_TEST_DATABASE_URL and LND_TEST_REDIS_URL, or run `make test-deps` to start the ones in
docker-compose.test.yml. Without them those tests are skipped locally and fail in CI.

Environment is set here, before anything from lnd is imported, because settings are read
once per process.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import redis as redis_lib

TEST_DB = os.environ.get("LND_TEST_DATABASE_URL", "postgresql://lnd@127.0.0.1:55432/lnd_test")
TEST_REDIS = os.environ.get("LND_TEST_REDIS_URL", "redis://127.0.0.1:56379/15")
_DATA_DIR = tempfile.mkdtemp(prefix="lnd-test-data-")
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)

os.environ["DATABASE_URL"] = TEST_DB
os.environ["REDIS_URL"] = TEST_REDIS
os.environ["DATA_DIR"] = _DATA_DIR
os.environ["BACKOFF_BASE_S"] = "0.05"
os.environ["BACKOFF_CAP_S"] = "0.2"
os.environ["MAX_ATTEMPTS"] = "3"
os.environ["PDF_DPI"] = "72"
os.environ.pop("DEFAULT_TOKEN_BUDGET", None)

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up")


def _unavailable(what: str, exc: Exception) -> None:
    msg = f"{what} is not reachable ({exc}). Run `make test-deps` or set LND_TEST_* variables."
    if os.environ.get("CI"):
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="session")
def services() -> None:
    import psycopg

    try:
        with psycopg.connect(TEST_DB, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        _unavailable("Postgres", exc)
    try:
        redis_lib.Redis.from_url(TEST_REDIS).ping()
    except Exception as exc:
        _unavailable("Redis", exc)
    from lnd.db import init_schema

    init_schema()


@pytest.fixture(scope="session")
def redis_service() -> None:
    try:
        redis_lib.Redis.from_url(TEST_REDIS).ping()
    except Exception as exc:
        _unavailable("Redis", exc)


@pytest.fixture
def clean(services: None) -> Iterator[None]:
    """Empty database and queue before each integration test."""
    from lnd.db import connection

    with connection() as conn:
        conn.execute(
            "TRUNCATE llm_calls, events, page_checks, page_versions, pages, documents, tenants "
            "RESTART IDENTITY CASCADE"
        )
    redis_lib.Redis.from_url(TEST_REDIS).flushdb()
    shutil.rmtree(Path(_DATA_DIR) / "docs", ignore_errors=True)
    yield


class FakeModel:
    """The fake model server running in a background thread of the test process."""

    def __init__(self, port: int, server: Any) -> None:
        self.url = f"http://127.0.0.1:{port}"
        self._server = server

    def control(self, **kwargs: Any) -> None:
        httpx.post(self.url + "/_control", json=kwargs, timeout=5).raise_for_status()

    def reset(self) -> None:
        self.control(reset=True)

    def stats(self) -> dict[str, Any]:
        return httpx.get(self.url + "/_stats", timeout=5).json()


@pytest.fixture(scope="session")
def fake_model() -> Iterator[FakeModel]:
    import uvicorn

    from lnd.fakes.model_server import create_app

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_http(f"http://127.0.0.1:{port}/health")
    yield FakeModel(port, server)
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def model(fake_model: FakeModel) -> FakeModel:
    fake_model.reset()
    return fake_model


class StubSandbox:
    """Stands in for the sandbox manager in tests that are not about the sandbox."""

    def __init__(self, issues: list[str] | None = None, fail: bool = False) -> None:
        self.issues = issues or []
        self.fail = fail
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def run(self, code: str, rows: list[dict[str, Any]], mode: str = "auto") -> dict[str, Any]:
        self.calls.append((code, rows))
        if self.fail:
            from lnd.sandbox_client import SandboxUnavailable

            raise SandboxUnavailable("stub: sandbox down")
        return {
            "status": "ok",
            "result": {"ok": True, "issues": self.issues},
            "start": "warm",
            "timings_ms": {"total": 1.0},
        }


@pytest.fixture
def stub_sandbox() -> StubSandbox:
    return StubSandbox()


def spawn(args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.Popen(  # noqa: S603
        [sys.executable, *args],
        env=full_env,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop(proc: subprocess.Popen[bytes], sig_kill: bool = False) -> None:
    if proc.poll() is not None:
        return
    if sig_kill:
        proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def make_png(text: str = "page", size: tuple[int, int] = (400, 300)) -> bytes:
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).text((20, 20), text, fill="black")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def make_pdf(pages: int) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"Notebook page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark tests by what they need, so `pytest -m "not integration and not docker"` works."""
    for item in items:
        names = getattr(item, "fixturenames", [])
        if {"services", "clean", "redis_service"} & set(names):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def api(services: None) -> Iterator[Any]:
    """The real FastAPI app, served in-process by Starlette's TestClient."""
    from fastapi.testclient import TestClient

    from lnd.api import app

    with TestClient(app) as client:
        yield client
