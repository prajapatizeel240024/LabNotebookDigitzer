"""The only place that talks to the model server.

Every model call in the project goes through LLMGateway.chat(). It:
  * rejects the call if the tenant has used up its token budget,
  * calls the OpenAI-compatible endpoint (llama.cpp's llama-server),
  * records prompt tokens, completion tokens, latency, tenant and job in llm_calls,
  * adds the tokens to the tenant's running total.

tests/test_architecture.py checks that nothing else in the package calls the model endpoint.

Budget note: the check happens before the call and the tokens are added after it, so a few
calls that were already running when the budget ran out can push usage slightly past it.
The overshoot is bounded by (calls in flight) x (tokens per call). Reserving tokens up front
would remove that, at the cost of holding reservations when a worker dies; see DESIGN.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import get_settings
from .db import connection, ensure_tenant

CHAT_PATH = "/v1/chat/completions"


class BudgetExceeded(Exception):
    def __init__(self, tenant: str, used: int, budget: int) -> None:
        super().__init__(f"tenant {tenant!r} has used {used} of {budget} tokens")
        self.tenant = tenant
        self.used = used
        self.budget = budget


class ModelError(Exception):
    """The model server was unreachable or returned something unusable."""


@dataclass(frozen=True)
class ChatResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


class LLMGateway:
    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        s = get_settings()
        self.base_url = (base_url or s.model_base_url).rstrip("/")
        self.model = model or s.model_name
        self._client = client or httpx.Client(timeout=timeout_s or s.model_timeout_s)

    # ---- budget ----------------------------------------------------------------------------
    def _check_budget(self, tenant: str, job_id: str | None, purpose: str) -> None:
        with connection() as conn:
            ensure_tenant(conn, tenant)
            row = conn.execute(
                "SELECT token_budget, tokens_used FROM tenants WHERE id = %s", (tenant,)
            ).fetchone()
            budget, used = row["token_budget"], row["tokens_used"]
            if budget is not None and used >= budget:
                conn.execute(
                    "INSERT INTO llm_calls (tenant_id, job_id, purpose, model, status, error) "
                    "VALUES (%s, %s, %s, %s, 'rejected', 'token budget spent')",
                    (tenant, job_id, purpose, self.model),
                )
                conn.commit()
                raise BudgetExceeded(tenant, used, budget)

    def _record(
        self,
        tenant: str,
        job_id: str | None,
        purpose: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        error: str | None,
    ) -> None:
        with connection() as conn:
            conn.execute(
                "INSERT INTO llm_calls (tenant_id, job_id, purpose, model, status, prompt_tokens, "
                "completion_tokens, latency_ms, error) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    tenant,
                    job_id,
                    purpose,
                    self.model,
                    status,
                    prompt_tokens,
                    completion_tokens,
                    latency_ms,
                    error,
                ),
            )
            if prompt_tokens or completion_tokens:
                conn.execute(
                    "UPDATE tenants SET tokens_used = tokens_used + %s WHERE id = %s",
                    (prompt_tokens + completion_tokens, tenant),
                )

    # ---- readiness -------------------------------------------------------------------------
    def model_ready(self) -> bool:
        """True once the model server's health check answers 200 (llama-server answers 503
        while it is still loading the model)."""
        try:
            return self._client.get(self.base_url + "/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False

    # ---- the call --------------------------------------------------------------------------
    def chat(
        self,
        *,
        tenant: str,
        job_id: str | None,
        purpose: str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> ChatResult:
        self._check_budget(tenant, job_id, purpose)

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "page", "strict": True, "schema": json_schema},
            }

        started = time.perf_counter()
        try:
            resp = self._client.post(self.base_url + CHAT_PATH, json=body)
            resp.raise_for_status()
            payload = resp.json()
            text = payload["choices"][0]["message"]["content"] or ""
            usage = payload.get("usage") or {}
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            self._record(tenant, job_id, purpose, "error", 0, 0, latency_ms, str(exc)[:500])
            raise ModelError(f"model call failed: {exc}") from exc

        latency_ms = (time.perf_counter() - started) * 1000
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        note = None
        if not usage:
            # Server gave no usage block. Count roughly so the budget still moves.
            completion = max(1, len(text) // 4)
            note = "usage missing from response; completion tokens estimated"
        self._record(tenant, job_id, purpose, "ok", prompt, completion, latency_ms, note)
        return ChatResult(text, prompt, completion, latency_ms)


def usage_summary(tenant: str) -> dict[str, Any]:
    """What the usage endpoint returns for one tenant."""
    with connection() as conn:
        t = conn.execute(
            "SELECT id, token_budget, tokens_used FROM tenants WHERE id = %s", (tenant,)
        ).fetchone()
        if t is None:
            return {
                "tenant": tenant,
                "token_budget": None,
                "tokens_used": 0,
                "tokens_remaining": None,
                "calls": {"ok": 0, "error": 0, "rejected": 0},
                "by_purpose": [],
                "latency_ms": {"avg": None, "max": None},
            }
        calls = conn.execute(
            "SELECT status, count(*) AS n FROM llm_calls WHERE tenant_id = %s GROUP BY status",
            (tenant,),
        ).fetchall()
        by_purpose = conn.execute(
            "SELECT purpose, count(*) AS calls, coalesce(sum(prompt_tokens),0) AS prompt_tokens, "
            "coalesce(sum(completion_tokens),0) AS completion_tokens, "
            "round(avg(latency_ms)::numeric, 1) AS avg_latency_ms "
            "FROM llm_calls WHERE tenant_id = %s AND status = 'ok' "
            "GROUP BY purpose ORDER BY purpose",
            (tenant,),
        ).fetchall()
        lat = conn.execute(
            "SELECT avg(latency_ms) AS avg, max(latency_ms) AS max FROM llm_calls "
            "WHERE tenant_id = %s AND status = 'ok'",
            (tenant,),
        ).fetchone()
    status_counts = {"ok": 0, "error": 0, "rejected": 0}
    status_counts.update({c["status"]: int(c["n"]) for c in calls})
    budget = t["token_budget"]
    return {
        "tenant": t["id"],
        "token_budget": budget,
        "tokens_used": int(t["tokens_used"]),
        "tokens_remaining": None if budget is None else max(0, int(budget) - int(t["tokens_used"])),
        "calls": status_counts,
        "by_purpose": [
            {
                "purpose": r["purpose"],
                "calls": int(r["calls"]),
                "prompt_tokens": int(r["prompt_tokens"]),
                "completion_tokens": int(r["completion_tokens"]),
                "avg_latency_ms": float(r["avg_latency_ms"]),
            }
            for r in by_purpose
        ],
        "latency_ms": {
            "avg": None if lat["avg"] is None else round(float(lat["avg"]), 1),
            "max": None if lat["max"] is None else round(float(lat["max"]), 1),
        },
    }
