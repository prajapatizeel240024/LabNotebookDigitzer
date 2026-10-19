"""The network and privilege layout is part of the design, so it is tested like code.

These read the compose files as plain YAML. They need no Docker.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


class _Loader(yaml.SafeLoader):
    """Understands Compose's !reset / !override tags (the value is kept as is)."""


for _tag in ("!reset", "!override"):
    _Loader.add_constructor(_tag, lambda loader, node: loader.construct_sequence(node))


def load(name: str = "docker-compose.yml") -> dict:
    return yaml.load((ROOT / name).read_text(), Loader=_Loader)  # noqa: S506 - SafeLoader subclass


@pytest.fixture(scope="module")
def compose() -> dict:
    return load()


def nets(svc: dict) -> set[str]:
    n = svc.get("networks", [])
    return set(n if isinstance(n, list) else n.keys())


def test_only_the_api_publishes_a_port_and_only_on_loopback(compose):
    published = {
        name: svc["ports"] for name, svc in compose["services"].items() if svc.get("ports")
    }
    assert published == {"api": ["127.0.0.1:${API_PORT:-8000}:8000"]}


def test_everything_but_api_and_fetcher_is_internal_only(compose):
    for name, svc in compose["services"].items():
        if name in ("api", "models-fetch", "sandbox-image"):
            continue
        assert nets(svc) == {"internal"}, name


def test_api_is_the_only_service_on_edge(compose):
    on_edge = [n for n, s in compose["services"].items() if "edge" in nets(s)]
    assert on_edge == ["api"]


def test_internal_network_has_no_route_out(compose):
    assert compose["networks"]["internal"]["internal"] is True
    edge = compose["networks"]["edge"]["driver_opts"]
    assert edge["com.docker.network.bridge.enable_ip_masquerade"] == "false"


def test_only_the_model_fetcher_can_reach_the_internet(compose):
    s = compose["services"]["models-fetch"]
    assert nets(s) == {"download"}
    assert s["profiles"] == ["models"]
    on_download = [n for n, v in compose["services"].items() if "download" in nets(v)]
    assert on_download == ["models-fetch"]


def test_sandbox_image_builder_has_no_network(compose):
    assert compose["services"]["sandbox-image"]["network_mode"] == "none"


def test_docker_socket_only_in_the_sandbox_manager(compose):
    holders = [
        n
        for n, s in compose["services"].items()
        if any("docker.sock" in str(v) for v in s.get("volumes", []))
    ]
    assert holders == ["sandbox-manager"]


def test_sandbox_manager_has_no_database_or_queue_credentials(compose):
    env = compose["services"]["sandbox-manager"]["environment"]
    assert not {"DATABASE_URL", "REDIS_URL", "POSTGRES_PASSWORD"} & set(env)
    assert "data:/data" not in compose["services"]["sandbox-manager"].get("volumes", [])


def test_no_privileged_host_network_or_added_capabilities(compose):
    for name, svc in compose["services"].items():
        assert not svc.get("privileged"), name
        assert svc.get("network_mode") in (None, "none"), name
        assert not svc.get("cap_add"), name
        assert not svc.get("pid") and not svc.get("ipc"), name


def test_app_containers_are_hardened(compose):
    for name in ("api", "worker", "sandbox-manager", "models-fetch"):
        svc = compose["services"][name]
        assert svc["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in svc["security_opt"], name
    for name in ("api", "worker", "sandbox-manager"):
        assert compose["services"][name]["read_only"] is True, name


def test_model_server_is_pinned_and_keeps_no_prompt_cache(compose):
    svc = compose["services"]["model-server"]
    assert "@sha256:" in svc["image"]
    cmd = svc["command"]
    assert cmd[cmd.index("--cache-ram") + 1] == "0"


def test_workers_wait_for_the_model_and_leases_outlast_three_model_calls(compose):
    worker = compose["services"]["worker"]
    assert worker["depends_on"]["model-server"] == {"condition": "service_healthy"}
    env = worker["environment"]

    def default(value: str) -> int:  # "${LEASE_S:-3600}" -> 3600
        return int(value.split(":-")[1].rstrip("}"))

    assert default(env["LEASE_S"]) > 3 * default(env["MODEL_TIMEOUT_S"])


def test_no_password_is_written_into_the_compose_files():
    for name in ("docker-compose.yml", "docker-compose.fake.yml", "docker-compose.test.yml"):
        text = (ROOT / name).read_text()
        assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in text or name != "docker-compose.yml"
        for line in text.splitlines():
            if "PASSWORD" in line and not line.lstrip().startswith("#"):
                assert "${" in line, f"{name}: {line.strip()}"


def test_dotenv_is_ignored_and_example_has_no_real_secret():
    assert ".env" in (ROOT / ".gitignore").read_text().splitlines()
    example = (ROOT / ".env.example").read_text()
    assert "generated-by-make-env" in example


def test_fake_override_swaps_only_the_model_server():
    fake = load("docker-compose.fake.yml")
    assert list(fake["services"]) == ["model-server"]
    svc = fake["services"]["model-server"]
    assert "networks" not in svc and "ports" not in svc
    assert svc["cap_drop"] == ["ALL"] and svc["read_only"] is True


def test_test_stack_binds_loopback_only():
    t = load("docker-compose.test.yml")
    for name, svc in t["services"].items():
        for p in svc["ports"]:
            assert p.startswith("127.0.0.1:"), name


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
@pytest.mark.parametrize("extra", [[], ["-f", "docker-compose.fake.yml"]])
def test_docker_compose_accepts_the_files(extra):
    env = {**os.environ, "POSTGRES_PASSWORD": "x", "DOCKER_GID": "999"}
    res = subprocess.run(  # noqa: S603
        ["docker", "compose", "-f", "docker-compose.yml", *extra, "config", "-q"],  # noqa: S607
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if "unknown shorthand flag" in res.stderr or "is not a docker command" in res.stderr:
        pytest.skip("docker compose plugin not installed")
    assert res.returncode == 0, res.stderr
