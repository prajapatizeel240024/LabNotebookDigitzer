"""JSON and CSV export of a document's current records."""

from __future__ import annotations

import csv
import io
from typing import Any

CSV_COLUMNS = [
    "page",
    "page_status",
    "title",
    "date",
    "reagent_name",
    "amount",
    "unit",
    "molar_mass_g_mol",
    "equivalents",
    "narrative",
    "notes",
    "version",
    "source",
]


def _safe(value: Any) -> Any:
    """Stop spreadsheet apps from running text as a formula (CSV injection).

    Only strings are touched. Numbers stay numbers, so a real negative amount is untouched.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def to_json(doc: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "document": {
            "id": str(doc["id"]),
            "filename": doc["filename"],
            "page_count": doc["page_count"],
            "status": doc["status"],
        },
        "pages": [
            {
                "page": r["page_no"],
                "status": r["status"],
                "version": r["version"],
                "source": r["source"],
                "data": r["data"],
            }
            for r in rows
        ],
    }


def to_csv(rows: list[dict[str, Any]]) -> str:
    """One line per reagent. A page with no reagents (or no data) still gets one line."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for r in rows:
        data = r["data"] or {}
        base = {
            "page": r["page_no"],
            "page_status": r["status"],
            "title": _safe(data.get("title", "")),
            "date": data.get("date") or "",
            "narrative": _safe(data.get("narrative", "")),
            "notes": _safe(data.get("notes", "")),
            "version": r["version"] if r["version"] is not None else "",
            "source": r["source"] or "",
        }
        reagents = data.get("reagents") or [None]
        for reagent in reagents:
            line = dict(base)
            if reagent:
                line.update(
                    reagent_name=_safe(reagent["name"]),
                    amount="" if reagent["amount"] is None else reagent["amount"],
                    unit=_safe(reagent["unit"] or ""),
                    molar_mass_g_mol=(
                        "" if reagent["molar_mass_g_mol"] is None else reagent["molar_mass_g_mol"]
                    ),
                    equivalents="" if reagent["equivalents"] is None else reagent["equivalents"],
                )
            writer.writerow(line)
    return out.getvalue()
