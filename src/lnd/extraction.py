"""Read one page image into a PageExtraction, with one retry on invalid output."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from pydantic import ValidationError

from .config import get_settings
from .gateway import LLMGateway
from .schemas import PageExtraction, llm_json_schema

EXTRACT_PROMPT = """You are transcribing one page of a handwritten chemistry lab notebook.
Return a single JSON object and nothing else, with these fields:
- title: the page title or experiment name (empty string if there is none)
- date: the page date as YYYY-MM-DD, or null if it is missing or not clear enough
- narrative: the handwritten prose (procedure and observations), as written
- reagents: one entry per row of the reagent table, with name, amount (a number),
  unit (for example g, mg, mL, mmol), molar_mass_g_mol (a number) and equivalents (a number).
  Use null for a cell that is blank or unreadable. Never guess a value.
- notes: margin notes and footnotes (empty string if there are none)
"""


class ExtractionError(Exception):
    """The model did not return a valid page, even after the retry."""


def image_data_url(image: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"


def _json_from_text(text: str) -> Any:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    return json.loads(text)


def parse_extraction(text: str) -> PageExtraction:
    return PageExtraction.model_validate(_json_from_text(text))


def extract_page(
    gateway: LLMGateway,
    *,
    image: bytes,
    mime: str,
    tenant: str,
    job_id: str,
) -> PageExtraction:
    """Ask the model once; if the answer is not valid, show it the error and ask once more."""
    schema = llm_json_schema()
    max_tokens = get_settings().extract_max_tokens
    user_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "image_url", "image_url": {"url": image_data_url(image, mime)}},
        ],
    }
    messages: list[dict[str, Any]] = [user_msg]

    first = gateway.chat(
        tenant=tenant,
        job_id=job_id,
        purpose="extract",
        messages=messages,
        max_tokens=max_tokens,
        json_schema=schema,
    )
    try:
        return parse_extraction(first.text)
    except (ValidationError, ValueError) as exc:
        problem = _short_problem(exc)

    messages = [
        user_msg,
        {"role": "assistant", "content": first.text[:4000]},
        {
            "role": "user",
            "content": "That answer was not valid: "
            f"{problem}. Answer again with only a JSON object that follows the schema.",
        },
    ]
    second = gateway.chat(
        tenant=tenant,
        job_id=job_id,
        purpose="extract_retry",
        messages=messages,
        max_tokens=max_tokens,
        json_schema=schema,
    )
    try:
        return parse_extraction(second.text)
    except (ValidationError, ValueError) as exc:
        raise ExtractionError(f"invalid output twice: {_short_problem(exc)}") from exc


def _short_problem(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for err in exc.errors()[:5]:
            where = ".".join(str(p) for p in err["loc"]) or "(root)"
            parts.append(f"{where}: {err['msg']}")
        return "; ".join(parts)
    return str(exc)[:300]
