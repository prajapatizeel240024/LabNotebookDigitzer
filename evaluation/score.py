"""Field-by-field scoring of extracted pages against facts.json.

Every field of every page is one yes/no comparison, so a page with 4 reagents has
title + date + narrative + notes + row count + 4 rows x 5 cells = 26 comparisons.
Rules (kept simple on purpose):
  title, date, reagent name, unit   normalised text must match (case, spacing, punctuation ignored)
  narrative, notes                  text similarity >= 0.90 (character level)
  amount, molar mass, equivalents   numbers within 1 percent
  reagent count                     must match exactly
Reagent rows are compared in the order written on the page. A row the model dropped scores
wrong in all five cells; an extra row only fails the count.
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from typing import Any

TEXT_SIMILARITY = 0.90
NUMBER_TOLERANCE = 0.01
REAGENT_FIELDS = ("name", "amount", "unit", "molar_mass_g_mol", "equivalents")
TOP_FIELDS = ("title", "date", "narrative", "notes", "reagent_count")


def norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").casefold()).strip()


def same_text(a: Any, b: Any) -> bool:
    return norm(a) == norm(b)


def similar(a: Any, b: Any) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def same_number(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= NUMBER_TOLERANCE * max(abs(a), abs(b), 1e-12)


def score_page(truth: dict[str, Any], pred: dict[str, Any] | None) -> dict[str, bool]:
    """Returns {field_name: correct}. A missing prediction gets every field wrong."""
    if pred is None:  # a failed page earns nothing, even where the truth is empty (page13's date)
        names = [*TOP_FIELDS]
        names += [f"reagent.{f}#{i}" for i in range(len(truth["reagents"])) for f in REAGENT_FIELDS]
        return dict.fromkeys(names, False)
    out: dict[str, bool] = {
        "title": same_text(truth["title"], pred.get("title")),
        "date": (truth["date"] or None) == (pred.get("date") or None),
        "narrative": similar(truth["narrative"], pred.get("narrative")) >= TEXT_SIMILARITY,
        "notes": similar(truth["notes"], pred.get("notes")) >= TEXT_SIMILARITY,
        "reagent_count": len(truth["reagents"]) == len(pred.get("reagents") or []),
    }
    got = pred.get("reagents") or []
    for i, want in enumerate(truth["reagents"]):
        row = got[i] if i < len(got) else {}
        for f in REAGENT_FIELDS:
            if f in ("name", "unit"):
                ok = same_text(want[f], row.get(f)) if row else False
            else:
                ok = same_number(want[f], row.get(f)) if row else False
            out[f"reagent.{f}#{i}"] = ok
    return out


def summarise(per_page: dict[str, dict[str, bool]]) -> dict[str, Any]:
    """Overall and per-field accuracy over many pages."""
    by_field: dict[str, list[bool]] = defaultdict(list)
    for fields in per_page.values():
        for name, ok in fields.items():
            key = (
                "reagent." + name.split(".")[1].split("#")[0]
                if name.startswith("reagent.")
                else name
            )
            by_field[key].append(ok)
    total = sum(len(v) for v in by_field.values())
    right = sum(sum(v) for v in by_field.values())
    perfect = sum(1 for f in per_page.values() if all(f.values()))
    return {
        "pages": len(per_page),
        "fields": total,
        "correct": right,
        "accuracy": right / total if total else 0.0,
        "pages_perfect": perfect,
        "by_field": {
            k: {"n": len(v), "accuracy": sum(v) / len(v)}
            for k, v in sorted(by_field.items(), key=lambda kv: kv[0])
        },
    }


def check_report(
    truth_pages: list[dict[str, Any]], checks: dict[str, dict[str, Any] | None]
) -> dict[str, Any]:
    """Did the model-written check flag the pages with planted problems, and only those?"""
    planted = [p["id"] for p in truth_pages if p["expect_check_issue"]]
    clean = [p["id"] for p in truth_pages if not p["expect_check_issue"]]

    def flagged(pid: str) -> bool | None:
        res = checks.get(pid)
        if not res or res.get("status") != "ok":
            return None  # check did not run
        return bool(res.get("issues"))

    caught = [p for p in planted if flagged(p) is True]
    false_alarms = [p for p in clean if flagged(p) is True]
    not_run = [p for p in planted + clean if flagged(p) is None]
    return {
        "planted": len(planted),
        "caught": len(caught),
        "clean_pages": len(clean),
        "false_alarms": len(false_alarms),
        "not_run": len(not_run),
    }
