"""Run the 20 synthetic pages through a running stack and score the answers.

    make eval          # stack must be up with the real model (make models && make up)

Each page is uploaded as its own one-page document under a fresh tenant, so nothing mixes with
other data. Results go to evaluation/results/latest.json and a table is printed. The model's
token use comes from the usage endpoint, so the numbers match what the gateway recorded.
Pages still unfinished at the timeout are scored as failed and listed, and the exit code is 1,
so a slow model costs those pages rather than the whole run.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from .score import check_report, score_page, summarise

HERE = Path(__file__).resolve().parent


def load_truth() -> list[dict[str, Any]]:
    return json.loads((HERE / "facts.json").read_text())["pages"]


def run(client: Any, tenant: str, pages_dir: Path, timeout: float) -> dict[str, Any]:
    """`client` is anything with httpx-style get/post (httpx.Client or a Starlette TestClient)."""
    truth = load_truth()
    headers = {"X-Tenant": tenant}
    docs: dict[str, str] = {}
    t0 = time.time()
    for page in truth:
        path = pages_dir / f"{page['id']}.jpg"
        res = client.post(
            "/api/documents",
            headers=headers,
            files={"file": (path.name, path.read_bytes(), "image/jpeg")},
        )
        res.raise_for_status()
        docs[page["id"]] = res.json()["id"]

    deadline = time.time() + timeout
    pending = dict(docs)
    page_ids: dict[str, str] = {}
    while pending and time.time() < deadline:
        for pid, doc_id in list(pending.items()):
            detail = client.get(f"/api/documents/{doc_id}", headers=headers).json()
            if detail["status"] in ("completed", "failed") or detail["pages_by_status"]["failed"]:
                page_ids[pid] = detail["pages"][0]["id"]
                del pending[pid]
        if pending:
            time.sleep(1.0)
    unfinished = sorted(pending)
    elapsed = time.time() - t0

    per_page: dict[str, dict[str, bool]] = {}
    checks: dict[str, dict[str, Any] | None] = {}
    failed: list[str] = []
    for page in truth:
        rec: dict[str, Any] = {"record": None, "checks": None}  # not finished in time
        if page["id"] in page_ids:
            rec = client.get(f"/api/pages/{page_ids[page['id']]}/record", headers=headers).json()
        data = rec["record"]["data"] if rec["record"] else None
        if data is None:
            failed.append(page["id"])
        per_page[page["id"]] = score_page(page, data)
        checks[page["id"]] = (rec["checks"] or {}).get("result")
    usage = client.get(f"/api/tenants/{tenant}/usage", headers=headers).json()
    return {
        "tenant": tenant,
        "seconds": round(elapsed, 1),
        "failed_pages": failed,
        "unfinished_pages": unfinished,
        "summary": summarise(per_page),
        "checks": check_report(truth, checks),
        "per_page": {k: {f: v for f, v in d.items() if not v} for k, d in per_page.items()},
        "usage": usage,
    }


def table(res: dict[str, Any]) -> str:
    s = res["summary"]
    lines = [
        f"Overall: {100 * s['accuracy']:.1f}% of {s['fields']} fields correct "
        f"({s['pages_perfect']}/{s['pages']} pages fully correct)",
        "",
        "| Field | Cells | Accuracy |",
        "|---|---:|---:|",
    ]
    lines += [f"| {k} | {v['n']} | {100 * v['accuracy']:.1f}% |" for k, v in s["by_field"].items()]
    c = res["checks"]
    lines += [
        "",
        f"Consistency check: caught {c['caught']} of {c['planted']} planted problems, "
        f"{c['false_alarms']} false alarms on {c['clean_pages']} clean pages, "
        f"{c['not_run']} pages where the check did not run.",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--pages", type=Path, default=HERE / "pages")
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--out", type=Path, default=HERE / "results" / "latest.json")
    args = ap.parse_args()
    if not (args.pages / "index.json").exists():
        sys.exit("no pages found; run `make eval-pages` first")
    tenant = f"eval-{uuid.uuid4().hex[:8]}"
    with httpx.Client(base_url=args.api, timeout=60) as client:
        res = run(client, tenant, args.pages, args.timeout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(res, indent=2))
    print(table(res))
    print(f"\n{res['seconds']} s for {res['summary']['pages']} pages; wrote {args.out}")
    if res["unfinished_pages"]:
        sys.exit(
            f"{len(res['unfinished_pages'])} pages did not finish in {args.timeout:.0f}s and were "
            f"scored as failed: {', '.join(res['unfinished_pages'])}"
        )


if __name__ == "__main__":
    main()
