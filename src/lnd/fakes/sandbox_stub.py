"""A stand-in for the sandbox manager that never starts a container.

Used by the worker-scaling load test (so it measures the queue and workers, not Docker) and by
tests that are not about the sandbox. It answers like the real manager does for a check that
finds nothing wrong.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI

app = FastAPI(title="Sandbox stub")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run")
def run(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "result": {"ok": True, "issues": []},
        "start": "stub",
        "timings_ms": {"acquire": 0.0, "exec": 0.0, "total": 0.0},
    }


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8090")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
