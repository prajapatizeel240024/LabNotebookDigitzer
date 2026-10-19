"""Pydantic models for what the vision model must return, and for API payloads."""

from __future__ import annotations

import copy
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Reagent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    amount: float | None = None
    unit: str | None = Field(default=None, max_length=30)
    molar_mass_g_mol: float | None = Field(default=None, ge=0)
    equivalents: float | None = Field(default=None, ge=0)


class PageExtraction(BaseModel):
    """Strict shape of one notebook page. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=300)
    date: str | None = None
    narrative: str = Field(max_length=20000)
    reagents: list[Reagent] = Field(default_factory=list, max_length=60)
    notes: str = Field(default="", max_length=5000)

    @field_validator("date")
    @classmethod
    def _iso_date(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not ISO_DATE.match(v):
            raise ValueError("date must be YYYY-MM-DD or null")
        return v


class RecordUpdate(BaseModel):
    """Body of a user edit."""

    model_config = ConfigDict(extra="forbid")

    data: PageExtraction
    editor: str = Field(default="reviewer", max_length=80)
    note: str = Field(default="", max_length=500)


class BudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_budget: int | None = Field(default=None, ge=0)


# Cosmetic keywords, plus size and range limits. Pydantic enforces the limits afterwards, and
# large maxLength values make llama.cpp's grammar converter build very large grammars.
_DROP_KEYS = {
    "$defs",
    "title",
    "default",
    "pattern",
    "description",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
}


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Inline $refs and drop keywords the grammar does not need.

    The keys of a "properties" map are field names, not schema keywords, so a field that
    happens to be called "title" must survive. Those maps are walked separately.
    """
    if isinstance(node, dict):
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return _inline_refs(copy.deepcopy(defs[name]), defs)
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                out[key] = {name: _inline_refs(sub, defs) for name, sub in value.items()}
            else:
                out[key] = _inline_refs(value, defs)
        return out
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def llm_json_schema() -> dict[str, Any]:
    """JSON schema in the plain form llama.cpp's grammar converter handles well.

    $refs are inlined and cosmetic keys are dropped. Pydantic still does the real
    validation afterwards, so anything the grammar cannot express (like the date
    pattern) is still enforced.

    Every key is required (a value can still be null), so the grammar makes the model write
    all of them in page order: title, date, narrative, reagents, notes. With only title and
    narrative required, the real model wrote title, narrative, reagents, notes and left the
    date out on every page, even one dated 2025-02-03.
    """
    raw = PageExtraction.model_json_schema()
    schema = _inline_refs(raw, raw.get("$defs", {}))
    schema["required"] = list(schema["properties"])
    item = schema["properties"]["reagents"]["items"]
    item["required"] = list(item["properties"])
    return schema
