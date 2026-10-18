"""Model-written consistency checks for reagent tables.

The model writes a small Python function for each table. That code is untrusted: it is
never run here, only inside a sandbox container (see sandbox/ and DESIGN.md).
"""

from __future__ import annotations

import json
import re
from typing import Any

from .config import get_settings
from .gateway import LLMGateway
from .sandbox_client import SandboxClient, SandboxUnavailable

MAX_CODE_BYTES = 16_000

CHECK_PROMPT = """Below are the rows of a reagent table from a lab notebook page, as JSON.
Each row has name, amount, unit, molar_mass_g_mol and equivalents. Any value can be null.

{rows}

Write a Python function check(rows) that finds internal inconsistencies and returns a list of
short strings, one per problem. Return an empty list when everything is consistent.
Checks to do:
- When an amount is a mass (g or mg) and the molar mass is known, moles = mass / molar mass.
  An amount in mmol or mol is already moles. Compare moles across rows when a row also has
  equivalents: equivalents should be moles divided by the moles of the first reagent that has
  an amount, within 5 percent.
- Flag a row whose amount is negative or whose molar mass is zero.
Use only plain Python and the math module. No file or network access. No printing.
Reply with only the code in one ```python block.
"""


def build_prompt(rows: list[dict[str, Any]]) -> str:
    return CHECK_PROMPT.format(rows=json.dumps(rows, indent=1))


def extract_code(text: str) -> str | None:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    code = (m.group(1) if m else text).strip()
    if "def check" not in code:
        return None
    if len(code.encode()) > MAX_CODE_BYTES:
        return None
    return code


def run_checks(
    gateway: LLMGateway,
    sandbox: SandboxClient,
    *,
    rows: list[dict[str, Any]],
    tenant: str,
    job_id: str,
) -> tuple[str | None, dict[str, Any]]:
    """Ask the model for check code, run it in the sandbox. Returns (code, result).

    Never raises for problems in the generated code or the sandbox; those become a result
    with status 'error' so the extraction itself is still saved. A spent token budget does
    propagate (BudgetExceeded) so the caller can report it.
    """
    result = gateway.chat(
        tenant=tenant,
        job_id=job_id,
        purpose="check_codegen",
        messages=[{"role": "user", "content": build_prompt(rows)}],
        max_tokens=get_settings().check_max_tokens,
    )
    code = extract_code(result.text)
    if code is None:
        return None, {"status": "error", "error": "model did not return usable check code"}
    return code, run_code(sandbox, code, rows)


def run_code(sandbox: SandboxClient, code: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        out = sandbox.run(code, rows)
    except SandboxUnavailable as exc:
        return {"status": "error", "error": f"sandbox unavailable: {exc}"}
    status = out.get("status")
    if status == "ok" and isinstance(out.get("result"), dict) and out["result"].get("ok"):
        issues = out["result"].get("issues", [])
        return {
            "status": "ok",
            "issues": issues,
            "passed": len(issues) == 0,
            "start": out.get("start"),
            "total_ms": out.get("timings_ms", {}).get("total"),
        }
    error = out.get("error") or (out.get("result") or {}).get("error") or status
    return {"status": "error", "error": str(error)[:500], "sandbox_status": status}
