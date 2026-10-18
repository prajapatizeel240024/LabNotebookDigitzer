import threading

from lnd import store
from lnd.db import connection
from lnd.gateway import LLMGateway
from lnd.pipeline import process_job
from lnd.sandbox_client import SandboxClient
from lnd.worker import make_queue

from .conftest import StubSandbox
from .helpers import drain, events_of, upload_image, upload_pdf


def types(events):
    return [e["type"] for e in events]


def seqs(events):
    return [e["seq"] for e in events]


def test_single_photo_end_to_end(clean, model, api):
    doc = upload_image(api)
    sandbox = StubSandbox()
    drain(make_queue(), LLMGateway(base_url=model.url), sandbox)

    detail = api.get(f"/api/documents/{doc['id']}").json()
    assert detail["status"] == "completed"
    assert detail["pages_by_status"]["done"] == 1
    page = detail["pages"][0]
    record = api.get(f"/api/pages/{page['id']}/record").json()
    assert record["record"]["version"] == 1 and record["record"]["source"] == "model"
    assert record["record"]["data"]["title"] == "Synthesis of aspirin"
    assert record["checks"]["result"]["status"] == "ok"
    assert len(sandbox.calls) == 1  # check code went to the sandbox, not run here
    assert "def check" in sandbox.calls[0][0]
    ev = events_of(api, doc["id"])
    assert types(ev) == ["document_created", "page_done", "document_done"]
    assert seqs(ev) == [1, 2, 3]


def test_pdf_makes_one_job_per_page_and_renders_in_the_worker(clean, model, api):
    doc = upload_pdf(api, pages=6)
    q = make_queue()
    assert q.stats()["ready"] == 6
    drain(q, LLMGateway(base_url=model.url), StubSandbox())
    detail = api.get(f"/api/documents/{doc['id']}").json()
    assert detail["status"] == "completed"
    assert [p["status"] for p in detail["pages"]] == ["done"] * 6
    # every page got its own image, and the API can serve it
    for p in detail["pages"]:
        res = api.get(f"/api/pages/{p['id']}/image")
        assert res.status_code == 200 and res.content.startswith(b"\x89PNG")
    ev = events_of(api, doc["id"])
    assert types(ev).count("page_done") == 6 and types(ev).count("document_done") == 1
    assert seqs(ev) == list(range(1, 9))  # no gaps
    done_counts = [e["data"]["done"] for e in ev if e["type"] == "page_done"]
    assert done_counts == [1, 2, 3, 4, 5, 6]


def test_running_the_same_job_twice_changes_nothing(clean, model, api):
    doc = upload_image(api)
    q = make_queue()
    gw, sb = LLMGateway(base_url=model.url), StubSandbox()
    job = q.pop(60)
    raw_copy = dict(job.data)
    assert process_job(job, q, gw, sb) == "done"
    calls_after_first = model.stats()["page_requests"]

    q.enqueue(
        {k: raw_copy[k] for k in ("page_id", "document_id", "page_no", "tenant")}
    )  # a duplicate delivery
    assert process_job(q.pop(60), q, gw, sb) == "skipped"

    assert model.stats()["page_requests"] == calls_after_first
    assert types(events_of(api, doc["id"])) == ["document_created", "page_done", "document_done"]
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    assert len(api.get(f"/api/pages/{page['id']}/versions").json()) == 1


def test_finishing_a_page_twice_is_a_no_op(clean, model, api):
    doc = upload_image(api)
    page = store.list_pages(doc["id"])[0]
    data = {"title": "t", "date": None, "narrative": "n", "reagents": [], "notes": ""}
    args = {
        "page_id": str(page["id"]),
        "document_id": doc["id"],
        "extraction": data,
        "check_code": None,
        "check_result": None,
    }
    assert store.finalize_page_done(**args) is True
    assert store.finalize_page_done(**args) is False
    assert types(events_of(api, doc["id"])) == ["document_created", "page_done", "document_done"]


def test_failed_attempt_is_retried_with_backoff_and_then_succeeds(clean, model, api):
    doc = upload_image(api)
    model.control(fail_next=1)
    q = make_queue()
    outcomes = drain(q, LLMGateway(base_url=model.url), StubSandbox())
    assert outcomes == ["retry", "done"]
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    assert page["status"] == "done"
    assert page["attempts"] == 2
    assert q.stats()["dead"] == 0


def test_three_failures_move_the_job_to_the_dead_letter_list(clean, model, api):
    doc = upload_image(api)
    model.control(fail_next=1000)
    q = make_queue()
    outcomes = drain(q, LLMGateway(base_url=model.url), StubSandbox())
    assert outcomes == ["retry", "retry", "dead"]
    assert q.stats() == {"ready": 0, "delayed": 0, "inflight": 0, "dead": 1}
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    assert page["status"] == "failed"
    assert "gave up after 3 attempts" in page["error"]
    ev = events_of(api, doc["id"])
    assert types(ev) == ["document_created", "page_failed", "document_done"]
    assert ev[-1]["data"] == {"total": 1, "done": 0, "failed": 1}


def test_a_failed_page_can_be_retried_by_hand(clean, model, api):
    doc = upload_image(api)
    model.control(fail_next=1000)
    q = make_queue()
    gw = LLMGateway(base_url=model.url)
    drain(q, gw, StubSandbox())
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]

    model.control(fail_next=0)
    assert api.post(f"/api/pages/{page['id']}/retry").status_code == 200
    assert q.stats()["dead"] == 0  # the old dead-letter entry was cleared
    drain(q, gw, StubSandbox())
    detail = api.get(f"/api/documents/{doc['id']}").json()
    assert detail["pages"][0]["status"] == "done" and detail["status"] == "completed"
    assert types(events_of(api, doc["id"])) == [
        "document_created",
        "page_failed",
        "document_done",
        "page_requeued",
        "page_done",
        "document_done",
    ]
    assert api.post(f"/api/pages/{page['id']}/retry").status_code == 409  # only failed pages


def test_invalid_json_once_still_gives_a_page(clean, model, api):
    upload_image(api)
    model.control(invalid_next=1)
    outcomes = drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox())
    assert outcomes == ["done"]
    with connection() as conn:
        purposes = [r["purpose"] for r in conn.execute("SELECT purpose FROM llm_calls ORDER BY id")]
    assert purposes == ["extract", "extract_retry", "check_codegen"]


def test_budget_spent_fails_pages_without_burning_retries(clean, model, api):
    api.put("/api/tenants/default/budget", json={"token_budget": 2500})
    doc = upload_pdf(api, pages=6)
    q = make_queue()
    outcomes = drain(q, LLMGateway(base_url=model.url), StubSandbox())
    assert outcomes.count("budget") >= 1
    assert "retry" not in outcomes and q.stats()["dead"] == 0
    detail = api.get(f"/api/documents/{doc['id']}").json()
    by = detail["pages_by_status"]
    assert by["done"] >= 1 and by["failed"] >= 1 and by["done"] + by["failed"] == 6
    failed = [p for p in detail["pages"] if p["status"] == "failed"]
    assert all(p["error"].startswith("budget_exceeded") for p in failed)
    usage = api.get("/api/tenants/default/usage").json()
    assert usage["calls"]["rejected"] >= 1
    # Raising the budget and retrying a page works.
    api.put("/api/tenants/default/budget", json={"token_budget": None})
    assert api.post(f"/api/pages/{failed[0]['id']}/retry").status_code == 200
    drain(q, LLMGateway(base_url=model.url), StubSandbox())
    again = api.get(f"/api/pages/{failed[0]['id']}/record").json()
    assert again["page"]["status"] == "done"


def test_sandbox_down_does_not_lose_the_extraction(clean, model, api):
    upload_image(api)
    drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox(fail=True))
    doc = api.get("/api/documents").json()[0]
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    record = api.get(f"/api/pages/{page['id']}/record").json()
    assert record["page"]["status"] == "done"
    assert record["checks"]["result"]["status"] == "error"
    assert "sandbox unavailable" in record["checks"]["result"]["error"]


def test_check_issues_are_stored(clean, model, api):
    upload_image(api)
    drain(make_queue(), LLMGateway(base_url=model.url), StubSandbox(issues=["row 2 is off"]))
    doc = api.get("/api/documents").json()[0]
    page = api.get(f"/api/documents/{doc['id']}").json()["pages"][0]
    checks = api.get(f"/api/pages/{page['id']}/record").json()["checks"]["result"]
    assert checks["passed"] is False and checks["issues"] == ["row 2 is off"]


def test_workers_finishing_together_emit_one_document_done(clean, model, api):
    """Eight workers, one document: exactly one document_done and no gaps in the ids."""
    pages = 40
    doc = upload_pdf(api, pages=pages)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            q = make_queue()
            drain(q, LLMGateway(base_url=model.url), StubSandbox(), timeout=60)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ev = events_of(api, doc["id"])
    assert types(ev).count("document_done") == 1
    assert types(ev).count("page_done") == pages
    assert seqs(ev) == list(range(1, pages + 3))
    assert types(ev)[-1] == "document_done"
    assert api.get(f"/api/documents/{doc['id']}").json()["status"] == "completed"


def test_worker_that_vanishes_has_its_job_recovered(clean, model, api):
    """A job whose worker crashed (lease runs out) is run by someone else."""
    doc = upload_image(api)
    q = make_queue()
    taken = q.pop(0.05)  # worker A takes the job and dies without a word
    assert taken is not None
    import time

    time.sleep(0.15)
    drain(q, LLMGateway(base_url=model.url), StubSandbox())
    detail = api.get(f"/api/documents/{doc['id']}").json()
    assert detail["status"] == "completed"
    assert detail["pages"][0]["status"] == "done"


def test_default_sandbox_client_builds_without_a_server():
    assert SandboxClient().base_url.startswith("http")
