"""Sandbox manager: runs untrusted check code in single-use, locked-down containers.

This is the only service that gets the Docker socket. It exposes exactly one action over HTTP,
"run this code on these rows". Callers cannot choose the image, mounts, network, limits or
timeout, so a compromised worker cannot use the manager to start arbitrary containers.

Every sandbox is created with:
    no network (network_mode none)      read-only root filesystem
    small noexec tmpfs on /tmp          non-root user
    all capabilities dropped            no-new-privileges
    pids, memory, swap and CPU limits   a wall-clock timeout enforced from outside

A container runs one job and is then deleted. A warm pool keeps a few started containers
idle (waiting on a pid-1 that does nothing) so a job only pays for an exec, not a container
create and start. The code goes in through `docker exec` arguments; no volumes are mounted.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

import docker
from docker.errors import APIError, DockerException, NotFound
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .runner import RESULT_MARKER, RUNNER_SOURCE

log = logging.getLogger("lnd.sandbox")

LABEL = "lnd.sandbox"
IDLE_COMMAND = ["python3", "-I", "-c", "import signal; signal.pause()"]
EXEC_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "HOME": "/tmp"}  # noqa: S108
MAX_CODE_CHARS = 16_000
MAX_ROWS_JSON_BYTES = 64_000


@dataclass(frozen=True)
class SandboxConfig:
    image: str = "lnd-sandbox:latest"
    pool_size: int = 4
    timeout_s: float = 5.0
    memory: str = "128m"
    cpus: float = 0.5
    pids: int = 64
    tmpfs_size: str = "16m"
    output_limit: int = 64 * 1024
    max_concurrent: int = 4
    queue_wait_s: float = 15.0
    runtime: str = ""
    user: str = "10001:10001"

    @staticmethod
    def from_env() -> SandboxConfig:
        e = os.environ.get
        return SandboxConfig(
            image=e("SANDBOX_IMAGE", "lnd-sandbox:latest"),
            pool_size=int(e("SANDBOX_POOL_SIZE", "4")),
            timeout_s=float(e("SANDBOX_TIMEOUT_S", "5")),
            memory=e("SANDBOX_MEMORY", "128m"),
            cpus=float(e("SANDBOX_CPUS", "0.5")),
            pids=int(e("SANDBOX_PIDS", "64")),
            tmpfs_size=e("SANDBOX_TMPFS_SIZE", "16m"),
            output_limit=int(e("SANDBOX_OUTPUT_LIMIT", str(64 * 1024))),
            max_concurrent=int(e("SANDBOX_MAX_CONCURRENT", "4")),
            runtime=e("SANDBOX_RUNTIME", ""),
            user=e("SANDBOX_USER", "10001:10001"),
        )


@dataclass
class PoolStats:
    created: int = 0
    runs: int = 0
    warm_hits: int = 0
    cold_starts: int = 0
    create_errors: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class SandboxPool:
    def __init__(self, client: docker.DockerClient, cfg: SandboxConfig) -> None:
        self.client = client
        self.cfg = cfg
        self._ready: deque[Any] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(cfg.max_concurrent)
        self._cleanup = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sandbox-rm")
        self.stats = PoolStats()
        self._thread: threading.Thread | None = None

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        self.remove_stale()
        self._thread = threading.Thread(target=self._replenish_loop, daemon=True, name="pool")
        self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            leftovers = list(self._ready)
            self._ready.clear()
        for c in leftovers:
            self._remove(c)
        self._cleanup.shutdown(wait=True)

    def remove_stale(self) -> None:
        for c in self.client.containers.list(all=True, filters={"label": LABEL}):
            self._remove(c)

    # ---- containers ------------------------------------------------------------------------
    def _create(self) -> Any:
        cfg = self.cfg
        kwargs: dict[str, Any] = {
            "command": IDLE_COMMAND,
            "network_mode": "none",
            "read_only": True,
            "tmpfs": {"/tmp": f"rw,noexec,nosuid,nodev,size={cfg.tmpfs_size},mode=1777"},  # noqa: S108
            "user": cfg.user,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges"],
            "pids_limit": cfg.pids,
            "mem_limit": cfg.memory,
            "memswap_limit": cfg.memory,
            "nano_cpus": int(cfg.cpus * 1_000_000_000),
            "shm_size": "1m",
            "labels": {LABEL: "1"},
            "environment": {},
            "working_dir": "/tmp",  # noqa: S108
            "ulimits": [
                docker.types.Ulimit(name="nofile", soft=256, hard=256),
                docker.types.Ulimit(name="core", soft=0, hard=0),
            ],
        }
        if cfg.runtime:
            kwargs["runtime"] = cfg.runtime
        c = self.client.containers.create(cfg.image, **kwargs)
        try:
            c.start()
        except Exception:
            self._remove(c)
            raise
        with self.stats.lock:
            self.stats.created += 1
        return c

    def _remove(self, c: Any) -> None:
        try:
            c.remove(force=True, v=True)
        except NotFound:
            pass
        except (APIError, DockerException) as exc:
            log.warning("could not remove sandbox %s: %s", getattr(c, "short_id", "?"), exc)

    def _dispose(self, c: Any) -> None:
        """Used containers are always deleted, never reused."""
        self._cleanup.submit(self._remove, c)
        self._wake.set()

    def _replenish_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=2.0)
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    missing = self.cfg.pool_size - len(self._ready)
                if missing <= 0:
                    break
                try:
                    c = self._create()
                except Exception as exc:
                    with self.stats.lock:
                        self.stats.create_errors += 1
                    log.warning("could not create warm sandbox: %s", exc)
                    time.sleep(2.0)
                    break
                with self._lock:
                    self._ready.append(c)

    def _take(self, mode: str) -> tuple[Any, str]:
        if mode != "cold":
            with self._lock:
                c = self._ready.popleft() if self._ready else None
            if c is not None:
                self._wake.set()
                with self.stats.lock:
                    self.stats.warm_hits += 1
                return c, "warm"
        c = self._create()
        with self.stats.lock:
            self.stats.cold_starts += 1
        return c, "cold"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ready = len(self._ready)
        with self.stats.lock:
            s = self.stats
            return {
                "image": self.cfg.image,
                "pool_size": self.cfg.pool_size,
                "ready": ready,
                "created_total": s.created,
                "runs_total": s.runs,
                "warm_hits": s.warm_hits,
                "cold_starts": s.cold_starts,
                "create_errors": s.create_errors,
                "runtime": self.cfg.runtime or "runc (default)",
            }

    # ---- running code ----------------------------------------------------------------------
    def run(self, code: str, rows: list[dict[str, Any]], mode: str = "auto") -> dict[str, Any]:
        if not self._slots.acquire(timeout=self.cfg.queue_wait_s):
            raise TimeoutError("too many sandboxes running")
        t0 = time.perf_counter()
        container = None
        try:
            container, start = self._take(mode)
            t_acquired = time.perf_counter()
            try:
                out = self._exec(container, code, rows)
            except (NotFound, APIError):
                # A warm container that died while idle: fall back to a fresh one.
                self._dispose(container)
                container, start = self._create(), "cold"
                t_acquired = time.perf_counter()
                out = self._exec(container, code, rows)
            t_done = time.perf_counter()
            with self.stats.lock:
                self.stats.runs += 1
            out["start"] = start
            out["timings_ms"] = {
                "acquire": round((t_acquired - t0) * 1000, 2),
                "exec": round((t_done - t_acquired) * 1000, 2),
                "total": round((t_done - t0) * 1000, 2),
            }
            return out
        finally:
            if container is not None:
                self._dispose(container)
            self._slots.release()

    def _exec(self, container: Any, code: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        cfg = self.cfg
        api = self.client.api
        b64_code = base64.b64encode(code.encode()).decode()
        b64_rows = base64.b64encode(json.dumps(rows).encode()).decode()
        exec_id = api.exec_create(
            container.id,
            ["python3", "-I", "-c", RUNNER_SOURCE, b64_code, b64_rows],
            user=cfg.user,
            workdir="/tmp",  # noqa: S108
            environment=EXEC_ENV,
        )["Id"]

        flags = {"timeout": False, "output_limit": False}

        def kill(reason: str) -> None:
            flags[reason] = True
            with contextlib.suppress(APIError, NotFound):
                container.kill()

        timer = threading.Timer(cfg.timeout_s, kill, args=("timeout",))
        timer.start()
        stdout = bytearray()
        stderr = bytearray()
        try:
            for out_chunk, err_chunk in api.exec_start(exec_id, stream=True, demux=True):
                if flags["output_limit"] or flags["timeout"]:
                    # A kill is under way. Keep reading and throw the data away: if we stopped
                    # reading, the daemon would block writing to us and the kill would stall.
                    continue
                if out_chunk:
                    stdout += out_chunk
                if err_chunk:
                    stderr += err_chunk
                if len(stdout) + len(stderr) > cfg.output_limit:
                    flags["output_limit"] = True
                    threading.Thread(target=kill, args=("output_limit",), daemon=True).start()
        except (OSError, APIError) as exc:
            # Killing the container mid-stream can surface as a read error; that is expected
            # when we did it on purpose.
            if not (flags["timeout"] or flags["output_limit"]):
                raise NotFound(str(exc)) from exc
        finally:
            timer.cancel()

        exit_code: int | None = None
        if not (flags["timeout"] or flags["output_limit"]):
            try:
                exit_code = api.exec_inspect(exec_id).get("ExitCode")
            except (APIError, NotFound):
                exit_code = None

        text_out = bytes(stdout[: cfg.output_limit]).decode("utf-8", "replace")
        text_err = bytes(stderr[: cfg.output_limit]).decode("utf-8", "replace")
        result = _find_result(text_out)
        base = {
            "result": result,
            "stdout": _clip(_strip_result(text_out), 2000),
            "stderr": _clip(text_err, 2000),
            "exit_code": exit_code,
        }
        if flags["output_limit"]:
            return {**base, "status": "output_limit", "error": "output limit exceeded"}
        if flags["timeout"]:
            return {**base, "status": "timeout", "error": f"killed after {cfg.timeout_s:g}s"}
        if result is not None:
            if result.get("ok"):
                return {**base, "status": "ok"}
            return {**base, "status": "error", "error": result.get("error", "check failed")}
        return {
            **base,
            "status": "error",
            "error": f"no result (exit code {exit_code}); the process was killed or crashed",
        }


def _find_result(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        if line.startswith(RESULT_MARKER):
            try:
                value = json.loads(line[len(RESULT_MARKER) :])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _strip_result(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith(RESULT_MARKER)).strip()


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "...[clipped]"


# ---- HTTP -----------------------------------------------------------------------------------
class RunRequest(BaseModel):
    """The only thing a caller can ask for. Extra fields (an image, a network...) are errors."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    mode: Literal["auto", "warm", "cold"] = "auto"


def create_app(pool: SandboxPool | None = None, start_pool: bool = True) -> FastAPI:
    """Build the HTTP app. Tests pass an already running pool with start_pool=False."""
    holder: dict[str, SandboxPool] = {}

    def lifespan_start() -> None:
        p = pool or SandboxPool(docker.from_env(), SandboxConfig.from_env())
        holder["pool"] = p
        if start_pool:
            p.start()

    def lifespan_stop() -> None:
        if start_pool:
            holder["pool"].stop()

    app = FastAPI(title="Sandbox manager", on_startup=[lifespan_start], on_shutdown=[lifespan_stop])

    @app.get("/health")
    def health() -> dict[str, Any]:
        holder["pool"].client.ping()
        return {"status": "ok"}

    @app.get("/pool")
    def pool_info() -> dict[str, Any]:
        return holder["pool"].snapshot()

    @app.post("/run")
    def run(req: RunRequest) -> dict[str, Any]:
        if len(json.dumps(req.rows)) > MAX_ROWS_JSON_BYTES:
            raise HTTPException(413, "rows are too large")
        try:
            return holder["pool"].run(req.code, req.rows, req.mode)
        except TimeoutError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (APIError, DockerException) as exc:
            raise HTTPException(502, f"docker error: {exc}") from exc

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        create_app(),
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8090")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
