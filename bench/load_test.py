"""Worker-fleet load test.

Uploads one synthetic PDF (300 pages by default) and measures how long the fleet takes to turn
every page into a record, for 1, 2, 4 and 8 workers. The model is a fake server that answers
instantly, so the number is the speed of everything around the model: PDF page rendering,
the Redis queue, Postgres writes and the gateway. It says nothing about model speed.

Two ways to run:

  local     (default) starts the API, fake model, sandbox stub and N worker processes on this
            machine from the current Python environment. Needs Postgres and Redis reachable
            through DATABASE_URL and REDIS_URL. Uses its own tenant and never deletes data.
  external  talks to a stack that is already running (for example docker compose with
            `--scale worker=N`) through --api, and just reports the numbers. `make loadtest`
            uses this to scale the real compose service.

    python -m bench.load_test --workers 1 2 4 8
    python -m bench.load_test --launch external --api http://127.0.0.1:8000 --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pymupdf


def make_pdf(path: Path, pages: int) -> None:
    """A deterministic PDF with text and ruled lines on every page, so rendering costs something."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((50, 60), f"Experiment {i + 1}: coupling reaction", fontsize=16)
        page.insert_text((450, 60), f"2025-03-{(i % 28) + 1:02d}", fontsize=11)
        y = 100
        for line in range(18):
            page.insert_text(
                (50, y),
                f"Step {line + 1}: stirred the mixture for {10 + line} min at room temperature.",
                fontsize=10,
            )
            y += 16
        top = 420
        for row in range(6):
            page.draw_line((50, top + row * 28), (545, top + row * 28))
            for col, label in enumerate(["Reagent", "Amount", "Unit", "MW", "Equiv"]):
                text = label if row == 0 else f"R{row}-{col}"
                page.insert_text((56 + col * 100, top + row * 28 + 18), text, fontsize=10)
        page.draw_line((50, top + 6 * 28), (545, top + 6 * 28))
    doc.save(path)
    doc.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise SystemExit(f"{url} did not come up")


class Stack:
    """Processes started for a local run. Stopped on exit."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[bytes]] = []
        self.data_dir = Path(tempfile.mkdtemp(prefix="lnd-load-"))
        self.env = {**os.environ, "DATA_DIR": str(self.data_dir)}

    def spawn(
        self, args: list[str], extra: dict[str, str] | None = None
    ) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, *args],
            env={**self.env, **(extra or {})},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        return proc

    def stop(self, procs: list[subprocess.Popen[bytes]] | None = None) -> None:
        for p in procs if procs is not None else list(self.procs):
            if p.poll() is None:
                p.terminate()
        for p in procs if procs is not None else list(self.procs):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
            if p in self.procs:
                self.procs.remove(p)

    def close(self) -> None:
        self.stop()
        shutil.rmtree(self.data_dir, ignore_errors=True)


def run_document(api: str, tenant: str, pdf: Path, timeout: float) -> tuple[float, dict[str, Any]]:
    """Upload and wait until every page is finished. Returns (seconds, final document)."""
    headers = {"X-Tenant": tenant}
    t0 = time.perf_counter()
    with pdf.open("rb") as f:
        res = httpx.post(
            f"{api}/api/documents", files={"file": (pdf.name, f)}, headers=headers, timeout=120
        )
    res.raise_for_status()
    doc_id = res.json()["id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        doc = httpx.get(f"{api}/api/documents/{doc_id}", headers=headers, timeout=30).json()
        if doc["status"] == "completed":
            return time.perf_counter() - t0, doc
        time.sleep(0.1)
    raise SystemExit(f"document {doc_id} did not finish within {timeout:.0f}s")


def queue_is_idle(api: str) -> bool:
    q = httpx.get(f"{api}/api/queue", timeout=10).json()
    return q["ready"] == 0 and q["inflight"] == 0 and q["delayed"] == 0


def measure(api: str, tenant: str, pdf: Path, warmup_pdf: Path, timeout: float) -> dict[str, Any]:
    run_document(api, tenant, warmup_pdf, timeout)  # workers and caches are warm now
    while not queue_is_idle(api):
        time.sleep(0.1)
    seconds, doc = run_document(api, tenant, pdf, timeout)
    pages = doc["page_count"]
    by = doc["pages_by_status"]
    return {
        "pages": pages,
        "seconds": round(seconds, 2),
        "pages_per_min": round(pages / seconds * 60, 1),
        "done": by["done"],
        "failed": by["failed"],
    }


def local_run(args: argparse.Namespace) -> list[dict[str, Any]]:
    stack = Stack()
    tmp = Path(tempfile.mkdtemp(prefix="lnd-load-pdf-"))
    results: list[dict[str, Any]] = []
    try:
        pdf, warm = tmp / "synthetic-300.pdf", tmp / "warmup.pdf"
        make_pdf(pdf, args.pages)
        make_pdf(warm, 10)
        model_port, sandbox_port, api_port = free_port(), free_port(), free_port()
        stack.spawn(
            ["-m", "lnd.fakes.model_server"],
            {"PORT": str(model_port), "FAKE_LATENCY_MS": str(args.model_latency_ms)},
        )
        stack.spawn(["-m", "lnd.fakes.sandbox_stub"], {"PORT": str(sandbox_port)})
        api_url = f"http://127.0.0.1:{api_port}"
        stack.spawn(
            [
                "-m",
                "uvicorn",
                "lnd.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--log-level",
                "warning",
            ],
            {"MAX_ATTEMPTS": "3"},
        )
        wait_http(f"http://127.0.0.1:{model_port}/health")
        wait_http(f"http://127.0.0.1:{sandbox_port}/health")
        wait_http(api_url + "/healthz")
        if not queue_is_idle(api_url):
            raise SystemExit("the Redis queue is not empty; use a clean Redis for the load test")
        worker_env = {
            "MODEL_BASE_URL": f"http://127.0.0.1:{model_port}",
            "SANDBOX_URL": f"http://127.0.0.1:{sandbox_port}",
        }
        tenant = f"loadtest-{uuid.uuid4().hex[:8]}"
        for n in args.workers:
            workers = [stack.spawn(["-m", "lnd.worker"], worker_env) for _ in range(n)]
            time.sleep(3.0)  # let the processes finish starting
            r = measure(api_url, tenant, pdf, warm, args.timeout)
            r["workers"] = n
            results.append(r)
            print(f"workers={n}: {r['pages_per_min']} pages/min ({r['seconds']}s)", flush=True)
            stack.stop(workers)
    finally:
        stack.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def external_run(args: argparse.Namespace) -> list[dict[str, Any]]:
    tmp = Path(tempfile.mkdtemp(prefix="lnd-load-pdf-"))
    try:
        pdf, warm = tmp / "synthetic-300.pdf", tmp / "warmup.pdf"
        make_pdf(pdf, args.pages)
        make_pdf(warm, 10)
        if not queue_is_idle(args.api):
            raise SystemExit("the queue is not empty; wait for it to drain first")
        tenant = f"loadtest-{uuid.uuid4().hex[:8]}"
        r = measure(args.api, tenant, pdf, warm, args.timeout)
        r["workers"] = args.workers[0]
        print(f"workers={r['workers']}: {r['pages_per_min']} pages/min ({r['seconds']}s)")
        return [r]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def markdown(results: list[dict[str, Any]], args: argparse.Namespace) -> str:
    base = results[0]["pages_per_min"]
    lines = [f"| Workers | Pages/min | Seconds for {args.pages} pages | Speed-up vs first row |"]
    lines.append("|---:|---:|---:|---:|")
    for r in results:
        lines.append(
            f"| {r['workers']} | {r['pages_per_min']:.0f} | {r['seconds']} | "
            f"{r['pages_per_min'] / base:.2f}x |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--pages", type=int, default=300)
    ap.add_argument("--launch", choices=["local", "external"], default="local")
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="for --launch external")
    ap.add_argument("--model-latency-ms", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--json", type=Path, help="also write the results to this file")
    args = ap.parse_args()

    results = local_run(args) if args.launch == "local" else external_run(args)
    env = {
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "model_latency_ms": args.model_latency_ms,
        "launch": args.launch,
    }
    print()
    print(markdown(results, args))
    print(
        f"\nrun on {env['cpus']} CPUs, Python {env['python']}, fake model latency "
        f"{env['model_latency_ms']} ms"
    )
    if args.json:
        args.json.write_text(json.dumps({"env": env, "results": results}, indent=2))


if __name__ == "__main__":
    main()
