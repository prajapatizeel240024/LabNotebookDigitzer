"""Settings read from environment variables.

Everything has a safe local default except the database URL password, which
comes from the environment (compose gets it from a generated, git-ignored .env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    data_dir: Path
    # model server (OpenAI-compatible, llama.cpp's llama-server)
    model_base_url: str
    model_name: str
    model_timeout_s: float
    extract_max_tokens: int
    check_max_tokens: int
    # sandbox manager
    sandbox_url: str
    sandbox_timeout_s: float
    # queue
    max_attempts: int
    backoff_base_s: float
    backoff_cap_s: float
    lease_s: float
    # documents
    pdf_dpi: int
    max_image_px: int
    max_upload_mb: int
    # misc
    default_token_budget: int | None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    budget = os.environ.get("DEFAULT_TOKEN_BUDGET", "")
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "postgresql://lnd@127.0.0.1:55432/lnd_test"),
        redis_url=os.environ.get("REDIS_URL", "redis://127.0.0.1:56379/0"),
        data_dir=Path(os.environ.get("DATA_DIR", "./data")),
        model_base_url=os.environ.get("MODEL_BASE_URL", "http://127.0.0.1:8081").rstrip("/"),
        model_name=os.environ.get("MODEL_NAME", "qwen2.5-vl-7b-instruct-q4_k_m"),
        model_timeout_s=_float("MODEL_TIMEOUT_S", 300),
        extract_max_tokens=_int("EXTRACT_MAX_TOKENS", 2048),
        check_max_tokens=_int("CHECK_MAX_TOKENS", 600),
        sandbox_url=os.environ.get("SANDBOX_URL", "http://127.0.0.1:8090").rstrip("/"),
        sandbox_timeout_s=_float("SANDBOX_CLIENT_TIMEOUT_S", 20),
        max_attempts=_int("MAX_ATTEMPTS", 3),
        backoff_base_s=_float("BACKOFF_BASE_S", 2.0),
        backoff_cap_s=_float("BACKOFF_CAP_S", 60.0),
        lease_s=_float("LEASE_S", 600),
        pdf_dpi=_int("PDF_DPI", 150),
        max_image_px=_int("MAX_IMAGE_PX", 2000),
        max_upload_mb=_int("MAX_UPLOAD_MB", 500),
        default_token_budget=int(budget) if budget else None,
    )


def reset_settings_cache() -> None:
    get_settings.cache_clear()
