import json
import threading
import time

import httpx
import pytest
import uvicorn

from lnd.fakes import sandbox_stub
from lnd.gateway import LLMGateway
from lnd.worker import make_queue

from .conftest import StubSandbox, free_port, spawn, stop, wait_http
from .helpers import drain, upload_pdf


def parse_sse(lines):
    """Yield (id, event, data) from an iterator of text lines."""
    cur: dict = {}
    for line in lines:
        if line == "":
            if "id" in cur:
                yield int(cur["id"]), cur.get("event", "message"), json.loads(cur.get("data", "{}"))
            cur = {}
        elif line.startswith(":"):
            continue
        elif ":" in line:
            key, _, value = line.partition(":")
            cur[key] = value.lstrip(" ")


def read_stream(api, doc_id, headers=None, params=None):
    out = []
    with api.stream(
        "GET", f"/api/documents/{doc_id}/events", headers=headers or {}, params=params or {}
    ) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        out.extend(parse_sse(res.iter_lines()))
    return out


def test_stream_replays_everything_and_ends_after_document_done(clean, api, model):
    doc = upload_pdf(api, pages=5)
    drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox())
    got = read_stream(api, doc["id"])
    assert [e[0] for e in got] == list(range(1, 8))
    assert [e[1] for e in got] == ["document_created"] + ["page_done"] * 5 + ["document_done"]
    assert got[-1][2] == {"total": 5, "done": 5, "failed": 0}


def test_last_event_id_returns_only_what_was_missed(clean, api, model):
    doc = upload_pdf(api, pages=8)
    drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox())
    got = read_stream(api, doc["id"], headers={"Last-Event-ID": "4"})
    assert [e[0] for e in got] == [5, 6, 7, 8, 9, 10]  # 4 is not sent again
    got = read_stream(api, doc["id"], headers={"Last-Event-ID": "10"})
    assert got == []  # nothing newer; stream ends at once because the document is done


def test_after_query_parameter_works_without_a_header(clean, api, model):
    doc = upload_pdf(api, pages=3)
    drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox())
    got = read_stream(api, doc["id"], params={"after": 3})
    assert [e[0] for e in got] == [4, 5]


def test_garbage_last_event_id_starts_from_the_beginning(clean, api, model):
    doc = upload_pdf(api, pages=2)
    drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox())
    got = read_stream(api, doc["id"], headers={"Last-Event-ID": "banana"})
    assert [e[0] for e in got] == [1, 2, 3, 4]


def test_stream_for_another_tenants_document_is_404(clean, api, model):
    doc = upload_pdf(api, pages=1, tenant="alice")
    res = api.get(f"/api/documents/{doc['id']}/events", headers={"X-Tenant": "bob"})
    assert res.status_code == 404


# ---- restart the API in the middle of a job ---------------------------------------------------
class Reader(threading.Thread):
    """Follows one SSE connection and records every event until it ends or breaks."""

    def __init__(self, base, doc_id, last_id):
        super().__init__(daemon=True)
        self.base, self.doc_id, self.last_id = base, doc_id, last_id
        self.events: list[tuple[int, str]] = []
        self.finished = False
        self.broke = False

    def run(self):
        headers = {"X-Tenant": "default"}
        if self.last_id:
            headers["Last-Event-ID"] = str(self.last_id)
        try:
            with httpx.stream(
                "GET",
                f"{self.base}/api/documents/{self.doc_id}/events",
                headers=headers,
                timeout=httpx.Timeout(30, read=30),
            ) as res:
                for seq, typ, _ in parse_sse(res.iter_lines()):
                    self.events.append((seq, typ))
                    if typ == "document_done":
                        self.finished = True
        except httpx.HTTPError:
            self.broke = True


@pytest.mark.slow
def test_restarting_the_api_mid_job_loses_and_duplicates_nothing(clean, api):
    pages = 40
    procs = []
    sandbox_port = free_port()
    sandbox = uvicorn.Server(
        uvicorn.Config(sandbox_stub.app, host="127.0.0.1", port=sandbox_port, log_level="error")
    )
    threading.Thread(target=sandbox.run, daemon=True).start()
    wait_http(f"http://127.0.0.1:{sandbox_port}/health")

    model_port, api_port = free_port(), free_port()
    base = f"http://127.0.0.1:{api_port}"
    api_cmd = [
        "-m",
        "uvicorn",
        "lnd.api:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(api_port),
        "--log-level",
        "warning",
    ]
    try:
        procs.append(
            spawn(
                ["-m", "lnd.fakes.model_server"],
                {"PORT": str(model_port), "FAKE_LATENCY_MS": "150"},
            )
        )
        wait_http(f"http://127.0.0.1:{model_port}/health")
        api_proc = spawn(api_cmd)
        wait_http(base + "/healthz")

        # Upload through the real server, then start two worker processes.
        from .conftest import make_pdf

        res = httpx.post(
            base + "/api/documents",
            files={"file": ("book.pdf", make_pdf(pages))},
            headers={"X-Tenant": "default"},
            timeout=30,
        )
        assert res.status_code == 202, res.text
        doc_id = res.json()["id"]
        worker_env = {
            "MODEL_BASE_URL": f"http://127.0.0.1:{model_port}",
            "SANDBOX_URL": f"http://127.0.0.1:{sandbox_port}",
        }
        procs += [spawn(["-m", "lnd.worker"], worker_env) for _ in range(2)]

        first = Reader(base, doc_id, 0)
        first.start()
        deadline = time.time() + 60
        while sum(1 for _, t in first.events if t == "page_done") < 8:
            assert time.time() < deadline, "no progress before the restart"
            time.sleep(0.05)

        # Kill the API hard, mid-job. Workers keep going without it.
        stop(api_proc, sig_kill=True)
        first.join(timeout=15)
        assert first.broke and not first.finished, "the first stream should have been cut"
        seen_before = len(first.events)
        assert 0 < seen_before < pages + 2
        time.sleep(1.5)

        api_proc = spawn(api_cmd)
        procs.append(api_proc)
        wait_http(base + "/healthz")
        last_seen = first.events[-1][0]
        second = Reader(base, doc_id, last_seen)
        second.start()
        second.join(timeout=90)
        assert second.finished, "the second stream never reached document_done"
    finally:
        for p in procs:
            stop(p, sig_kill=True)
        sandbox.should_exit = True

    all_ids = [seq for seq, _ in first.events] + [seq for seq, _ in second.events]
    kinds = [t for _, t in first.events] + [t for _, t in second.events]
    # exactly once: ids are 1..N in order, nothing repeated, nothing missing
    assert all_ids == list(range(1, pages + 3)), all_ids
    assert kinds[0] == "document_created" and kinds[-1] == "document_done"
    assert kinds.count("page_done") == pages
    assert kinds.count("document_done") == 1
    assert second.events[0][0] == last_seen + 1
    assert len(second.events) > 0
