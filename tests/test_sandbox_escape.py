"""Attacks on the sandbox. Every one of them has to fail.

Each test runs hostile code through the real SandboxPool against a real Docker daemon, in the
same containers the stack uses. Set SANDBOX_IMAGE to the sandbox image (CI builds
docker/sandbox.Dockerfile as lnd-sandbox:ci). Skipped locally if Docker or the image is
missing, and a failure in CI.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest
from fastapi.testclient import TestClient

from lnd.sandbox.manager import LABEL, SandboxConfig, SandboxPool, create_app

pytestmark = pytest.mark.docker

IMAGE = os.environ.get("SANDBOX_IMAGE", "lnd-sandbox:latest")
TIMEOUT_S = 2.0
POOL_SIZE = 2


def _unavailable(msg: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="module")
def pool() -> Iterator[SandboxPool]:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
    except Exception as exc:
        _unavailable(f"Docker or sandbox image {IMAGE!r} not available: {exc}")
    cfg = SandboxConfig(image=IMAGE, pool_size=POOL_SIZE, timeout_s=TIMEOUT_S, max_concurrent=6)
    p = SandboxPool(client, cfg)
    p.start()
    wait_ready(p)
    yield p
    p.stop()


def wait_ready(p: SandboxPool, want: int = POOL_SIZE, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.snapshot()["ready"] >= want:
            return
        time.sleep(0.1)
    raise AssertionError(f"pool never reached {want} ready: {p.snapshot()}")


def sandbox_containers() -> list[str]:
    """Ids of every sandbox container on the daemon (through the SDK, so no docker CLI needed)."""
    client = docker.from_env()
    return [c.id for c in client.containers.list(all=True, filters={"label": LABEL})]


def run_ok(p: SandboxPool, code: str, rows=None) -> dict:
    out = p.run(code, rows or [])
    assert out["status"] == "ok", out
    return out


# ---- the attacks that must all fail ----------------------------------------------------------
def test_network_calls_all_fail(pool):
    code = """
import socket
def check(rows):
    worked = []
    targets = [("1.1.1.1", 53), ("8.8.8.8", 443), ("172.17.0.1", 22), ("127.0.0.1", 8090)]
    for host, port in targets:
        try:
            socket.create_connection((host, port), timeout=1).close()
            worked.append("connected to %s:%s" % (host, port))
        except OSError as exc:
            print("blocked", host, port, type(exc).__name__)
    try:
        socket.getaddrinfo("example.com", 80)
        worked.append("resolved example.com")
    except OSError as exc:
        print("blocked dns", type(exc).__name__)
    return worked
"""
    out = run_ok(pool, code)
    assert out["result"]["issues"] == [], out["result"]["issues"]
    assert out["stdout"].count("blocked") == 5  # the code really ran, and every attempt failed


def test_writes_outside_tmp_fail_and_tmp_is_small(pool):
    code = """
import os
def check(rows):
    worked = []
    for path in ["/pwned", "/etc/pwned", "/usr/pwned", "/root/pwned", "/var/pwned", "/bin/pwned"]:
        try:
            with open(path, "w") as f:
                f.write("x")
            worked.append("wrote " + path)
        except OSError as exc:
            print("blocked", path, type(exc).__name__)
    try:
        os.remove("/etc/passwd")
        worked.append("deleted /etc/passwd")
    except OSError:
        print("blocked delete")
    try:
        os.mkdir("/newdir")
        worked.append("made /newdir")
    except OSError:
        print("blocked mkdir")
    with open("/tmp/ok.txt", "w") as f:  # the one place that must work
        f.write("fine")
    try:
        with open("/tmp/big.bin", "wb") as f:
            for _ in range(64):
                f.write(b"x" * 1024 * 1024)
        worked.append("wrote 64 MB to /tmp")
    except OSError as exc:
        print("tmp full", type(exc).__name__)
    return worked
"""
    out = run_ok(pool, code)
    assert out["result"]["issues"] == [], out["result"]["issues"]
    assert out["stdout"].count("blocked") == 8
    assert "tmp full" in out["stdout"]


def test_cannot_read_environment_secrets_or_host_files(pool, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LND_CANARY_SECRET", "canary-env-8f3a91")
    host_file = tmp_path / "host_secret.txt"
    host_file.write_text("canary-file-5d07c2")
    code = f"""
import os
def check(rows):
    leaks = []
    for key, value in os.environ.items():
        if "canary" in value.lower() or "CANARY" in key:
            leaks.append("env " + key)
        for bad in ("DATABASE", "REDIS", "DOCKER", "PASSWORD", "SECRET", "TOKEN"):
            if bad in key.upper():
                leaks.append("env " + key)
    for path in [{str(host_file)!r}, "/var/run/docker.sock", "/run/docker.sock"]:
        if os.path.exists(path):
            leaks.append("sees " + path)
    for path in ["/host", "/mnt"]:
        # Debian images ship an empty /mnt; only a mount, or something inside, is a leak.
        if os.path.exists(path) and (os.path.ismount(path) or os.listdir(path)):
            leaks.append("sees " + path)
    for root in ("/home", "/root", "/proc/1", "/etc/shadow"):
        pass
    try:
        data = open("/proc/1/environ", "rb").read()
        if b"canary" in data.lower():
            leaks.append("pid 1 environ has canary")
    except OSError:
        print("blocked /proc/1/environ")
    try:
        open("/etc/shadow").read()
        leaks.append("read /etc/shadow")
    except OSError:
        print("blocked /etc/shadow")
    needle = b"canary-"
    for top in ("/etc", "/usr/lib", "/var", "/opt", "/srv", "/home", "/root", "/tmp"):
        for base, _dirs, files in os.walk(top):
            for name in files:
                try:
                    with open(os.path.join(base, name), "rb") as f:
                        if needle in f.read(200000):
                            leaks.append("canary in " + os.path.join(base, name))
                except OSError:
                    pass
    print("env keys:", sorted(os.environ))
    return leaks
"""
    out = run_ok(pool, code)
    assert out["result"]["issues"] == [], out["result"]["issues"]
    assert "env keys:" in out["stdout"]  # the code really ran


def test_fork_bomb_is_stopped_by_the_pids_limit(pool):
    code = """
import os, time
def check(rows):
    kids = 0
    try:
        while True:
            pid = os.fork()
            if pid == 0:
                time.sleep(30)
                os._exit(0)
            kids += 1
            if kids > 5000:
                break
    except OSError:
        pass
    return ["forked %d children" % kids]
"""
    out = pool.run(code, [])
    assert out["status"] in {"ok", "timeout", "error"}
    if out["status"] == "ok":
        forked = int(out["result"]["issues"][0].split()[1])
        assert forked < pool.cfg.pids, f"pids limit did not hold: {forked} children"
    # the host is fine and nothing is left running
    assert pool.snapshot()["create_errors"] == 0
    wait_ready(pool)
    time.sleep(1.0)
    assert len(sandbox_containers()) <= POOL_SIZE + 1


def test_infinite_loop_is_killed_at_the_timeout(pool):
    started = time.time()
    out = pool.run("def check(rows):\n    while True:\n        pass\n", [])
    took = time.time() - started
    assert out["status"] == "timeout"
    assert TIMEOUT_S <= took < TIMEOUT_S + 4, took


def test_endless_output_is_cut_off(pool):
    code = """
import sys
def check(rows):
    while True:
        sys.stdout.write("x" * 65536)
        sys.stdout.flush()
"""
    started = time.time()
    out = pool.run(code, [])
    assert out["status"] == "output_limit", out["status"]
    assert time.time() - started < TIMEOUT_S + 4
    assert len(out["stdout"]) < 3000  # what we hand back is clipped too


def test_one_giant_print_does_not_get_through(pool):
    out = pool.run("def check(rows):\n    print('x' * 10**9)\n    return []\n", [])
    assert out["status"] in {"error", "output_limit", "timeout"}
    assert len(out["stdout"]) < 3000


def test_memory_bomb_is_contained(pool):
    out = pool.run("def check(rows):\n    blob = bytearray(2 * 1024**3)\n    return []\n", [])
    assert out["status"] != "ok"


def test_syntax_errors_and_bad_return_values_are_just_errors(pool):
    assert pool.run("def check(rows) return", [])["status"] == "error"
    assert pool.run("x = 1", [])["status"] == "error"  # no check function
    bad = pool.run("def check(rows):\n    return 42\n", [])
    assert bad["status"] == "error" and "list of strings" in bad["error"]


# ---- how the container is built --------------------------------------------------------------
def test_container_is_configured_as_locked_down(pool):
    c = pool._create()
    try:
        c.reload()
        host = c.attrs["HostConfig"]
        cfg = c.attrs["Config"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"] and not host.get("CapAdd")
        assert "no-new-privileges" in " ".join(host["SecurityOpt"])
        assert host["PidsLimit"] == pool.cfg.pids
        assert host["Memory"] == 128 * 1024 * 1024
        assert host["MemorySwap"] == host["Memory"]  # no swap
        assert host["NanoCpus"] == int(pool.cfg.cpus * 1e9)
        assert host["Privileged"] is False
        assert not host.get("Binds") and not c.attrs["Mounts"]  # nothing from the host
        assert set(host["Tmpfs"]) == {"/tmp"} and "size=16m" in host["Tmpfs"]["/tmp"]
        assert "noexec" in host["Tmpfs"]["/tmp"]
        assert cfg["User"] == pool.cfg.user and not cfg["User"].startswith("0")
        assert not cfg.get("Env") or all("=" in e for e in cfg["Env"])
    finally:
        pool._remove(c)


def test_code_runs_as_nobody_with_no_capabilities(pool):
    code = """
import os
def check(rows):
    lines = open("/proc/self/status").read().splitlines()
    status = dict(line.split(":\\t", 1) for line in lines if ":" in line)
    problems = []
    if os.getuid() == 0:
        problems.append("running as root")
    if status["CapEff"].strip("0") != "":
        problems.append("effective capabilities " + status["CapEff"])
    if status["CapBnd"].strip("0") != "":
        problems.append("bounding capabilities " + status["CapBnd"])
    if status["NoNewPrivs"].strip() != "1":
        problems.append("no_new_privs is off")
    try:
        os.setuid(0)
        problems.append("setuid(0) worked")
    except OSError:
        pass
    return problems
"""
    assert run_ok(pool, code)["result"]["issues"] == []


def test_every_sandbox_is_used_once_then_replaced(pool):
    code = """
import os, socket
def check(rows):
    marker = "/tmp/left-behind"
    seen = os.path.exists(marker)
    open(marker, "w").write("x")
    return [socket.gethostname(), "marker-seen" if seen else "fresh"]
"""
    first = run_ok(pool, code)["result"]["issues"]
    second = run_ok(pool, code)["result"]["issues"]
    assert first[1] == "fresh" and second[1] == "fresh"  # nothing carried over
    assert first[0] != second[0]  # a different container each time
    wait_ready(pool)
    time.sleep(0.5)
    assert len(sandbox_containers()) <= POOL_SIZE + 1  # used ones are gone


# ---- warm pool and the HTTP surface ----------------------------------------------------------
def test_warm_pool_serves_requests_and_refills(pool):
    wait_ready(pool)
    before = pool.snapshot()
    out = run_ok(pool, "def check(rows):\n    return []\n")
    assert out["start"] == "warm"
    wait_ready(pool)
    after = pool.snapshot()
    assert after["warm_hits"] == before["warm_hits"] + 1
    cold = pool.run("def check(rows):\n    return []\n", [], mode="cold")
    assert cold["start"] == "cold" and cold["status"] == "ok"


def test_parallel_requests_beyond_the_pool_still_work(pool):
    from concurrent.futures import ThreadPoolExecutor

    code = "import socket\ndef check(rows):\n    return [socket.gethostname()]\n"
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda _: pool.run(code, []), range(6)))
    assert all(r["status"] == "ok" for r in results)
    assert len({r["result"]["issues"][0] for r in results}) == 6  # six separate containers


def test_manager_only_accepts_code_and_rows(pool):
    """Callers cannot pick an image, network, mounts or timeout, so a hijacked worker cannot
    use the manager to start arbitrary containers."""
    with TestClient(create_app(pool, start_pool=False)) as client:
        ok = client.post("/run", json={"code": "def check(rows):\n    return []\n", "rows": []})
        assert ok.status_code == 200 and ok.json()["status"] == "ok"
        for extra in (
            {"image": "alpine"},
            {"network": "host"},
            {"timeout_s": 999},
            {"mounts": ["/"]},
        ):
            res = client.post("/run", json={"code": "def check(rows): return []", **extra})
            assert res.status_code == 422, extra
        assert client.post("/run", json={"code": "x" * 20000}).status_code == 422
        assert client.post("/run", json={"code": "x", "mode": "root"}).status_code == 422
        assert set(client.get("/pool").json()) >= {"ready", "warm_hits", "cold_starts"}
