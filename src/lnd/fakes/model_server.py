"""A fake OpenAI-compatible model server that answers instantly with canned output.

It speaks the same /v1/chat/completions shape as llama.cpp's llama-server, so the pipeline,
queue, gateway and sandbox can be tested and load tested without a GPU or a model.

Page requests (they carry an image) get a canned page. If an answer key is loaded
(FAKE_ORACLE_DIR with facts.json and pages/index.json) a known page image gets its true
answer, optionally with a fixed share of fields made wrong, which is how the scoring code
is tested. Requests without an image are check-code requests and get a working check.

Behaviour can be changed while running with POST /_control (latency, failures, invalid JSON).
Token counts in the response are made up and only exist so metering has numbers to count.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

CANNED_PAGE = {
    "title": "Synthesis of aspirin",
    "date": "2025-03-14",
    "narrative": (
        "Salicylic acid was dissolved in acetic anhydride with a few drops of phosphoric acid "
        "and warmed at 70 C for 15 min. Cooled, added water, white crystals formed."
    ),
    "reagents": [
        {
            "name": "Salicylic acid",
            "amount": 2.0,
            "unit": "g",
            "molar_mass_g_mol": 138.12,
            "equivalents": 1.0,
        },
        {
            "name": "Acetic anhydride",
            "amount": 3.0,
            "unit": "mL",
            "molar_mass_g_mol": 102.09,
            "equivalents": 2.2,
        },
    ],
    "notes": "Yield 2.1 g (80%). mp 135 C.",
}

CHECK_CODE = """```python
def check(rows):
    issues = []
    moles = []
    for r in rows:
        name = r.get("name") or "?"
        amount, unit = r.get("amount"), (r.get("unit") or "").lower()
        mm, eq = r.get("molar_mass_g_mol"), r.get("equivalents")
        if amount is not None and amount < 0:
            issues.append(name + ": negative amount")
        if mm is not None and mm <= 0:
            issues.append(name + ": molar mass must be positive")
        n = None
        if amount is not None:
            if unit == "g" and mm:
                n = amount / mm
            elif unit == "mg" and mm:
                n = amount / mm / 1000.0
            elif unit == "mmol":
                n = amount / 1000.0
            elif unit == "mol":
                n = amount
        moles.append((name, n, eq))
    base = next((n for _, n, _ in moles if n), None)
    if base:
        for name, n, eq in moles:
            if n is not None and eq is not None:
                expected = n / base
                if abs(expected - eq) > 0.05 * max(abs(eq), 1e-9):
                    msg = "%s: equivalents %.2f but amounts imply %.2f"
                    issues.append(msg % (name, eq, expected))
    return issues
```"""


class Control(BaseModel):
    latency_ms: float | None = None
    fail_next: int | None = None  # answer this many requests with HTTP 500
    invalid_next: int | None = None  # answer this many page requests with broken JSON
    error_rate: float | None = None  # share of fields to get wrong when an answer key is used
    check_code: str | None = None  # replace the code returned for check requests
    reset: bool = False


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latency_ms = float(os.environ.get("FAKE_LATENCY_MS", "0"))
        self.fail_next = 0
        self.invalid_next = 0
        self.error_rate = float(os.environ.get("FAKE_ERROR_RATE", "0"))
        self.check_code = CHECK_CODE
        self.requests = 0
        self.page_requests = 0
        self.check_requests = 0
        self.oracle: dict[str, dict[str, Any]] = {}
        self._load_oracle(os.environ.get("FAKE_ORACLE_DIR", ""))

    def _load_oracle(self, directory: str) -> None:
        if not directory:
            return
        root = Path(directory)
        facts = {p["id"]: p for p in json.loads((root / "facts.json").read_text())["pages"]}
        index = json.loads((root / "pages" / "index.json").read_text())
        for digest, page_id in index.items():
            self.oracle[digest] = facts[page_id]


def _perturb(page: dict[str, Any], rate: float, seed: str) -> dict[str, Any]:
    """Make roughly `rate` of the fields wrong, always the same ones for the same page."""
    rng = random.Random(seed)
    out = json.loads(json.dumps({k: page[k] for k in CANNED_PAGE}))

    def hit() -> bool:
        return rng.random() < rate

    if hit():
        out["title"] = out["title"] + " (copy)"
    if hit() and out["date"]:
        y, m, d = out["date"].split("-")
        out["date"] = f"{y}-{m}-{int(d) % 28 + 1:02d}"
    if hit():
        out["narrative"] = " ".join(out["narrative"].split()[:-3])
    for r in out["reagents"]:
        if hit():
            r["name"] = r["name"].lower() + "x"
        for key in ("amount", "molar_mass_g_mol", "equivalents"):
            if r[key] is not None and hit():
                r[key] = round(r[key] * 1.5 + 0.1, 3)
        if r["unit"] and hit():
            r["unit"] = "mg" if r["unit"] != "mg" else "g"
    if hit():
        out["notes"] = ""
    return out


def _image_digest(messages: list[dict[str, Any]], want_hash: bool) -> str | None:
    """None if there is no image, a sha256 if hashing is wanted, else the word "unknown"."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if want_hash and url.startswith("data:") and ";base64," in url:
                        raw = base64.b64decode(url.split(";base64,", 1)[1])
                        return hashlib.sha256(raw).hexdigest()
                    return "unknown"
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="Fake model server")
    state = State()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/_stats")
    def stats() -> dict[str, Any]:
        with state.lock:
            return {
                "requests": state.requests,
                "page_requests": state.page_requests,
                "check_requests": state.check_requests,
                "oracle_pages": len(state.oracle),
            }

    @app.post("/_control")
    def control(c: Control) -> dict[str, str]:
        with state.lock:
            if c.reset:
                state.fail_next = state.invalid_next = 0
                state.latency_ms = 0.0
                state.error_rate = 0.0
                state.check_code = CHECK_CODE
                state.requests = state.page_requests = state.check_requests = 0
            if c.latency_ms is not None:
                state.latency_ms = c.latency_ms
            if c.fail_next is not None:
                state.fail_next = c.fail_next
            if c.invalid_next is not None:
                state.invalid_next = c.invalid_next
            if c.error_rate is not None:
                state.error_rate = c.error_rate
            if c.check_code is not None:
                state.check_code = c.check_code
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        messages = body.get("messages", [])
        digest = _image_digest(messages, bool(state.oracle))
        with state.lock:
            state.requests += 1
            latency = state.latency_ms
            if state.fail_next > 0:
                state.fail_next -= 1
                raise HTTPException(500, "injected failure")
            is_page = digest is not None
            if is_page:
                state.page_requests += 1
            else:
                state.check_requests += 1
            broken = is_page and state.invalid_next > 0
            if broken:
                state.invalid_next -= 1
            rate = state.error_rate
            check_code = state.check_code
        if latency:
            await asyncio.sleep(latency / 1000.0)

        if is_page:
            # A repeat after an invalid answer includes an assistant turn; answer it properly.
            if broken:
                text = '{"title": "Synthesis of aspirin", "date": "14 March"'
            else:
                truth = state.oracle.get(digest or "")
                if truth is not None:
                    page = _perturb(truth, rate, digest or "") if rate else truth
                    page = {k: page[k] for k in CANNED_PAGE}
                else:
                    page = CANNED_PAGE
                text = json.dumps(page)
            prompt_tokens = 1100
        else:
            text = check_code
            prompt_tokens = 350
        completion_tokens = max(1, len(text) // 4)
        return {
            "id": "fake-1",
            "object": "chat.completion",
            "model": body.get("model", "fake"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8081")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
