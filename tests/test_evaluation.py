"""The accuracy harness: scorer rules, the answer key, and a full run against the oracle fake.

Nothing here measures a real model. The oracle fake answers with the true page (or the true
page with a known share of fields damaged), which proves the scoring and the plumbing: a
perfect answer scores 100 percent and a damaged one scores lower by about the damage.
"""

import copy
import json
import threading
import time
from pathlib import Path

import pytest
from evaluation import render_pages, run_eval
from evaluation.score import check_report, norm, same_number, score_page, summarise

from lnd.gateway import LLMGateway
from lnd.pipeline import process_job
from lnd.schemas import PageExtraction
from lnd.worker import make_queue

from .conftest import StubSandbox, free_port, wait_http

EVAL = Path(run_eval.__file__).resolve().parent
FACTS = json.loads((EVAL / "facts.json").read_text())["pages"]


def answer(page):
    return {k: page[k] for k in ("title", "date", "narrative", "reagents", "notes")}


# ---- answer key ---------------------------------------------------------------------------


def test_answer_key_has_20_valid_pages():
    assert len(FACTS) == 20 and len({p["id"] for p in FACTS}) == 20
    for p in FACTS:
        PageExtraction.model_validate(answer(p))


def test_answer_key_numbers_are_consistent_except_the_planted_ones():
    """moles = mass / molar mass; equivalents = moles / moles of the first reagent."""

    def moles(r):
        mg = r["amount"] * (1000 if r["unit"] == "g" else 1)
        return mg / r["molar_mass_g_mol"]

    flagged = []
    for p in FACTS:
        base = moles(p["reagents"][0])
        bad = [
            r["name"]
            for r in p["reagents"]
            if abs(moles(r) / base - r["equivalents"]) > 0.05 * r["equivalents"]
        ]
        if bad:
            flagged.append((p["id"], bad))
            assert p["expect_check_issue"] and bad == [p["issue_reagent"]]
        else:
            assert not p["expect_check_issue"], p["id"]
    assert len(flagged) == 3


def test_rendered_pages_match_the_committed_index(tmp_path):
    """Same code, same seeds, same pixels: re-rendering must reproduce index.json."""
    import hashlib

    render_pages.main(tmp_path)
    fresh = json.loads((tmp_path / "index.json").read_text())
    committed = json.loads((EVAL / "pages" / "index.json").read_text())
    assert fresh.values() and sorted(fresh.values()) == sorted(committed.values())
    if fresh != committed:  # different Pillow/numpy build: pixels differ, files must still exist
        pytest.skip("pixels differ on this library version; run `make eval-pages` to refresh")
    for p in FACTS:
        data = (EVAL / "pages" / f"{p['id']}.jpg").read_bytes()
        assert hashlib.sha256(data).hexdigest() in committed


def test_pages_are_small_enough_to_be_stored_unchanged():
    from PIL import Image

    for p in FACTS:
        w, h = Image.open(EVAL / "pages" / f"{p['id']}.jpg").size
        assert max(w, h) <= 2000


# ---- scorer -------------------------------------------------------------------------------


def test_perfect_answer_scores_everything():
    page = FACTS[2]
    fields = score_page(page, answer(page))
    assert all(fields.values())
    assert len(fields) == 5 + 5 * len(page["reagents"])


def test_each_kind_of_mistake_is_one_wrong_field():
    page = FACTS[2]
    pred = copy.deepcopy(answer(page))
    pred["title"] = "Something else"
    pred["date"] = "2025-01-01"
    pred["reagents"][1]["amount"] *= 1.5
    pred["reagents"][2]["unit"] = "g" if pred["reagents"][2]["unit"] == "mg" else "mg"
    wrong = sorted(k for k, ok in score_page(page, pred).items() if not ok)
    assert wrong == ["date", "reagent.amount#1", "reagent.unit#2", "title"]


def test_missing_rows_and_missing_page():
    page = FACTS[2]
    pred = copy.deepcopy(answer(page))
    pred["reagents"] = pred["reagents"][:-1]
    wrong = [k for k, ok in score_page(page, pred).items() if not ok]
    assert "reagent_count" in wrong and sum(k.endswith("#3") for k in wrong) == 5
    assert not any(score_page(page, None).values())


def test_text_and_number_tolerance():
    assert norm("  Aspirin,  from SALICYLIC-acid ") == "aspirin from salicylic acid"
    assert same_number(100, 100.9) and not same_number(100, 102)
    assert same_number(None, None) and not same_number(None, 0)
    page = FACTS[0]
    pred = answer(page)
    pred = {**pred, "title": pred["title"].upper(), "narrative": pred["narrative"] + " ok"}
    assert score_page(page, pred)["title"] and score_page(page, pred)["narrative"]


def test_summary_counts_per_field():
    per_page = {p["id"]: score_page(p, answer(p)) for p in FACTS[:3]}
    per_page["x"] = score_page(FACTS[0], None)
    s = summarise(per_page)
    assert s["pages"] == 4 and s["pages_perfect"] == 3
    assert 0 < s["accuracy"] < 1 and s["by_field"]["title"]["n"] == 4
    assert s["by_field"]["title"]["accuracy"] == 0.75


def test_check_report_counts_catches_and_false_alarms():
    checks = {p["id"]: {"status": "ok", "issues": []} for p in FACTS}
    planted = [p["id"] for p in FACTS if p["expect_check_issue"]]
    checks[planted[0]] = {"status": "ok", "issues": ["bad"]}
    checks[FACTS[0]["id"]] = {"status": "ok", "issues": ["oops"]}
    checks[FACTS[1]["id"]] = None
    r = check_report(FACTS, checks)
    assert (r["planted"], r["caught"], r["false_alarms"], r["not_run"]) == (3, 1, 1, 1)


# ---- end to end through the real API, queue and worker -----------------------------------


@pytest.fixture
def oracle_model(monkeypatch):
    import uvicorn

    from lnd.fakes.model_server import create_app

    monkeypatch.setenv("FAKE_ORACLE_DIR", str(EVAL))
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_http(f"http://127.0.0.1:{port}/health")
    base = f"http://127.0.0.1:{port}"
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def run_with_worker(api, base_url):
    queue = make_queue()
    gateway, sandbox = LLMGateway(base_url=base_url), StubSandbox()
    stop = threading.Event()

    def work():
        while not stop.is_set():
            job = queue.pop(60)
            if job is None:
                time.sleep(0.02)
                continue
            process_job(job, queue, gateway, sandbox)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    try:
        return run_eval.run(api, "eval-test", EVAL / "pages", timeout=120)
    finally:
        stop.set()
        t.join(timeout=5)


def test_oracle_model_scores_100_percent_end_to_end(clean, oracle_model, api):
    res = run_with_worker(api, oracle_model)
    assert res["failed_pages"] == []
    assert res["summary"]["accuracy"] == 1.0
    assert res["summary"]["pages_perfect"] == 20
    assert res["usage"]["tokens_used"] > 0
    assert res["checks"]["not_run"] == 0
    assert "100.0%" in run_eval.table(res)


def test_damaged_model_scores_lower(clean, oracle_model, api):
    import httpx

    httpx.post(oracle_model + "/_control", json={"error_rate": 0.3}).raise_for_status()
    res = run_with_worker(api, oracle_model)
    acc = res["summary"]["accuracy"]
    assert 0.5 < acc < 0.95, acc
    assert res["summary"]["pages_perfect"] < 20
