"""Rules about which module may do what. They keep the security story honest as code changes."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "lnd"


def py_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py")]


def rel(p: Path) -> str:
    return str(p.relative_to(SRC))


def test_only_the_sandbox_manager_imports_the_docker_sdk():
    users = [
        rel(p)
        for p in py_files()
        if re.search(r"^\s*(import docker|from docker)\b", p.read_text(), re.M)
    ]
    assert users == ["sandbox/manager.py"]


def test_workers_and_api_never_touch_docker_or_subprocess():
    for name in ("worker.py", "pipeline.py", "api.py", "checks.py", "extraction.py"):
        text = (SRC / name).read_text()
        assert not re.search(r"\b(import|from) (docker|subprocess)\b", text), name
        assert "docker.sock" not in text, name


def test_model_generated_code_is_never_exec_ed_in_process():
    for p in py_files():
        if rel(p) in ("sandbox/runner.py", "sandbox/manager.py"):
            continue
        text = p.read_text()
        assert not re.search(r"(?<![\w.])(exec|eval)\(", text), rel(p)


def test_sandbox_manager_does_not_import_the_database_or_queue():
    text = (SRC / "sandbox" / "manager.py").read_text()
    for forbidden in ("lnd.db", "lnd.queue", "lnd.store", "redis", "psycopg"):
        assert not re.search(rf"^\s*(import|from) {re.escape(forbidden)}\b", text, re.M), forbidden


def test_no_secrets_are_hard_coded():
    for p in py_files():
        for line in p.read_text().splitlines():
            if re.search(r"(password|secret|api_key|token)\s*=\s*[\"'][^\"']{6,}[\"']", line, re.I):
                raise AssertionError(f"{rel(p)}: {line.strip()}")
