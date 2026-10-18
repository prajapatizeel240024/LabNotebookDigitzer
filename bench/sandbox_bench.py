"""Sandbox start-up benchmark: warm pool versus cold start.

Talks to the sandbox manager over HTTP, the same way a worker does. If --url is not given it
starts a manager of its own (needs a Docker daemon and the sandbox image).

Definitions:
  acquire  time until a ready sandbox is in hand. Warm: take one from the pool. Cold: create
           and start a fresh container.
  total    acquire plus running the check inside it (a `docker exec` of the runner).
Warm runs wait for the pool to refill between requests, so they show a pool hit every time.
The burst test sends more requests at once than the pool holds, to show the spill to cold starts.

    SANDBOX_IMAGE=lnd-sandbox:latest python -m bench.sandbox_bench --runs 40
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

from .load_test import free_port, wait_http

CHECK = "def check(rows):\n    return [r['name'] for r in rows if (r.get('amount') or 0) < 0]\n"
ROWS = [{"name": "A", "amount": 1.0}, {"name": "B", "amount": -2.0}]


def percentile(values: list[float], p: float) -> float:
    xs = sorted(values)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def run_one(client: httpx.Client, url: str, mode: str) -> dict[str, Any]:
    t0 = time.perf_counter()
    res = client.post(f"{url}/run", json={"code": CHECK, "rows": ROWS, "mode": mode}, timeout=60)
    res.raise_for_status()
    body = res.json()
    assert body["status"] == "ok", body
    assert body["result"]["issues"] == ["B"], body
    body["round_trip_ms"] = (time.perf_counter() - t0) * 1000
    return body


def wait_pool_full(client: httpx.Client, url: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pool = client.get(f"{url}/pool", timeout=10).json()
        if pool["ready"] >= pool["pool_size"]:
            return pool
        time.sleep(0.05)
    raise SystemExit("the warm pool never filled")


def summarise(runs: list[dict[str, Any]]) -> dict[str, float]:
    acquire = [r["timings_ms"]["acquire"] for r in runs]
    total = [r["timings_ms"]["total"] for r in runs]
    return {
        "n": len(runs),
        "acquire_p50": percentile(acquire, 50),
        "acquire_p95": percentile(acquire, 95),
        "total_p50": percentile(total, 50),
        "total_p95": percentile(total, 95),
    }


def bench(url: str, runs: int, burst: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with httpx.Client() as client:
        pool = wait_pool_full(client, url)
        run_one(client, url, "warm")  # first call pays one-off costs (image layers, caches)
        wait_pool_full(client, url)
        warm, cold = [], []
        for _ in range(runs):
            wait_pool_full(client, url)
            r = run_one(client, url, "warm")
            assert r["start"] == "warm", "expected a pool hit"
            warm.append(r)
        for _ in range(runs):
            r = run_one(client, url, "cold")
            assert r["start"] == "cold"
            cold.append(r)
        out["pool_size"] = pool["pool_size"]
        out["runtime"] = pool["runtime"]
        out["image"] = pool["image"]
        out["warm"] = summarise(warm)
        out["cold"] = summarise(cold)

        wait_pool_full(client, url)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(burst) as ex:
            burst_runs = list(ex.map(lambda _: run_one(client, url, "auto"), range(burst)))
        wall = time.perf_counter() - t0
        out["burst"] = {
            "requests": burst,
            "warm": sum(1 for r in burst_runs if r["start"] == "warm"),
            "cold": sum(1 for r in burst_runs if r["start"] == "cold"),
            "wall_s": round(wall, 2),
            "round_trip_p50": percentile([r["round_trip_ms"] for r in burst_runs], 50),
            "round_trip_p95": percentile([r["round_trip_ms"] for r in burst_runs], 95),
        }
    return out


def markdown(res: dict[str, Any]) -> str:
    def row(name: str, s: dict[str, float]) -> str:
        return (
            f"| {name} | {s['n']:.0f} | {s['acquire_p50']:.2f} | {s['acquire_p95']:.2f} | "
            f"{s['total_p50']:.0f} | {s['total_p95']:.0f} |"
        )

    b = res["burst"]
    return "\n".join(
        [
            "| Start | Runs | Acquire p50 (ms) | Acquire p95 (ms) "
            "| Total p50 (ms) | Total p95 (ms) |",
            "|---|---:|---:|---:|---:|---:|",
            row("Warm (pool hit)", res["warm"]),
            row("Cold (create + start)", res["cold"]),
            "",
            f"Burst: {b['requests']} requests at once against a pool of {res['pool_size']}: "
            f"{b['warm']} served warm, {b['cold']} cold, all finished in {b['wall_s']} s "
            f"(round trip p50 {b['round_trip_p50']:.0f} ms, p95 {b['round_trip_p95']:.0f} ms).",
        ]
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", help="use a manager that is already running")
    ap.add_argument("--runs", type=int, default=40)
    ap.add_argument("--burst", type=int, default=12)
    ap.add_argument("--pool-size", type=int, default=4)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    proc = None
    url = args.url
    if not url:
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "PORT": str(port),
            "SANDBOX_POOL_SIZE": str(args.pool_size),
            "SANDBOX_MAX_CONCURRENT": str(max(args.burst, 4)),
        }
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "lnd.sandbox.manager"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        wait_http(url + "/health", timeout=60)
        res = bench(url, args.runs, args.burst)
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=30)
    res["env"] = {"cpus": os.cpu_count(), "platform": platform.platform()}
    print(markdown(res))
    print(f"\nimage: {res['image']}  runtime: {res['runtime']}  cpus: {res['env']['cpus']}")
    if args.json:
        args.json.write_text(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
