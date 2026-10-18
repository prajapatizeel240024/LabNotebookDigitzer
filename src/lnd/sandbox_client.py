"""HTTP client for the sandbox manager. Workers and the API use this; they never touch Docker."""

from __future__ import annotations

from typing import Any

import httpx

from .config import get_settings


class SandboxUnavailable(Exception):
    pass


class SandboxClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None) -> None:
        s = get_settings()
        self.base_url = (base_url or s.sandbox_url).rstrip("/")
        self._client = client or httpx.Client(timeout=s.sandbox_timeout_s)

    def run(self, code: str, rows: list[dict[str, Any]], mode: str = "auto") -> dict[str, Any]:
        try:
            resp = self._client.post(
                f"{self.base_url}/run", json={"code": code, "rows": rows, "mode": mode}
            )
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxUnavailable(str(exc)) from exc

    def pool(self) -> dict[str, Any]:
        try:
            resp = self._client.get(f"{self.base_url}/pool")
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxUnavailable(str(exc)) from exc
