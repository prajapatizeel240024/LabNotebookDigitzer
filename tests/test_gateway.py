from pathlib import Path

import pytest

from lnd.db import connection
from lnd.gateway import BudgetExceeded, LLMGateway, ModelError, usage_summary

from .conftest import make_png

SRC = Path(__file__).resolve().parent.parent / "src" / "lnd"


def ask(gw: LLMGateway, tenant="acme", job="doc:1", purpose="extract"):
    from lnd.extraction import image_data_url

    return gw.chat(
        tenant=tenant,
        job_id=job,
        purpose=purpose,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read this"},
                    {"type": "image_url", "image_url": {"url": image_data_url(make_png())}},
                ],
            }
        ],
        max_tokens=100,
    )


def test_call_is_recorded_with_tokens_latency_tenant_and_job(clean, model):
    gw = LLMGateway(base_url=model.url)
    result = ask(gw)
    assert result.prompt_tokens == 1100 and result.completion_tokens > 0
    with connection() as conn:
        rows = conn.execute("SELECT * FROM llm_calls").fetchall()
        used = conn.execute("SELECT tokens_used FROM tenants WHERE id = 'acme'").fetchone()
    assert len(rows) == 1
    row = rows[0]
    assert (row["tenant_id"], row["job_id"], row["purpose"], row["status"]) == (
        "acme",
        "doc:1",
        "extract",
        "ok",
    )
    assert row["prompt_tokens"] == 1100
    assert row["completion_tokens"] == result.completion_tokens
    assert row["latency_ms"] > 0
    assert used["tokens_used"] == 1100 + result.completion_tokens


def test_usage_endpoint_numbers_match_the_calls(clean, model, api):
    gw = LLMGateway(base_url=model.url)
    for i in range(3):
        ask(gw, job=f"doc:{i}")
    summary = api.get("/api/tenants/acme/usage").json()
    assert summary["calls"] == {"ok": 3, "error": 0, "rejected": 0}
    assert summary["tokens_used"] == sum(
        p["prompt_tokens"] + p["completion_tokens"] for p in summary["by_purpose"]
    )
    assert summary["by_purpose"][0]["purpose"] == "extract"
    assert summary["token_budget"] is None and summary["tokens_remaining"] is None


def test_usage_is_per_tenant(clean, model):
    gw = LLMGateway(base_url=model.url)
    ask(gw, tenant="a")
    ask(gw, tenant="a")
    ask(gw, tenant="b")
    assert usage_summary("a")["calls"]["ok"] == 2
    assert usage_summary("b")["calls"]["ok"] == 1
    assert usage_summary("nobody")["tokens_used"] == 0


def test_budget_rejects_once_spent_and_does_not_call_the_model(clean, model, api):
    gw = LLMGateway(base_url=model.url)
    api.put("/api/tenants/acme/budget", json={"token_budget": 2500})
    ask(gw)  # about 1,170 tokens
    ask(gw)  # about 2,340
    ask(gw)  # still under 2,500 when it starts, so it runs
    before = model.stats()["requests"]
    with pytest.raises(BudgetExceeded) as err:
        ask(gw)
    assert err.value.budget == 2500 and err.value.used >= 2500
    assert model.stats()["requests"] == before  # rejected without touching the model
    summary = usage_summary("acme")
    assert summary["calls"]["rejected"] == 1
    assert summary["tokens_remaining"] == 0


def test_raising_the_budget_lets_calls_through_again(clean, model, api):
    gw = LLMGateway(base_url=model.url)
    api.put("/api/tenants/acme/budget", json={"token_budget": 10})
    ask(gw)
    with pytest.raises(BudgetExceeded):
        ask(gw)
    api.put("/api/tenants/acme/budget", json={"token_budget": None})
    ask(gw)


def test_model_failure_is_recorded_and_costs_no_tokens(clean, model):
    gw = LLMGateway(base_url=model.url)
    model.control(fail_next=1)
    with pytest.raises(ModelError):
        ask(gw)
    summary = usage_summary("acme")
    assert summary["calls"]["error"] == 1
    assert summary["tokens_used"] == 0


def test_unreachable_model_is_a_model_error(clean):
    gw = LLMGateway(base_url="http://127.0.0.1:9")
    with pytest.raises(ModelError):
        ask(gw)


def test_only_the_gateway_talks_to_the_model():
    """Every model call has to be metered, so only gateway.py may know the endpoint."""
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        if path.name in {"gateway.py", "model_server.py"}:
            continue
        if "/v1/chat/completions" in text or "CHAT_PATH" in text:
            offenders.append(str(path.relative_to(SRC)))
        if "model_base_url" in text and path.name != "config.py":
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"model endpoint used outside the gateway: {offenders}"
