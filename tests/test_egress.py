"""A container on an internal Docker network cannot reach the internet.

This is the property the compose file relies on for the model server, the workers and the
database. The test builds the same kind of network (`internal: true`) and proves two things:
  1. the control: a container on it can still talk to another container on it, so a failure
     below means "no route out" and not "the network is broken";
  2. a container on it cannot open a connection to a public address, by IP or by name.
Needs a Docker daemon and an image with python3 (SANDBOX_IMAGE, same as the escape tests).
Skipped locally without them, a failure in CI.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import docker
import pytest

pytestmark = pytest.mark.docker

IMAGE = os.environ.get("SANDBOX_IMAGE", "lnd-sandbox:latest")

CONNECT = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    s = socket.create_connection((host, port), timeout=3)
except OSError as e:
    print("BLOCKED", type(e).__name__)
else:
    print("CONNECTED")
"""

SERVE = """
import http.server, socketserver
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"hello")
    def log_message(self, *a): pass
socketserver.TCPServer(("0.0.0.0", 8080), H).serve_forever()
"""


def _unavailable(msg: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(msg)
    pytest.skip(msg)


@pytest.fixture(scope="module")
def client() -> docker.DockerClient:
    try:
        c = docker.from_env()
        c.ping()
        c.images.get(IMAGE)
    except Exception as exc:  # noqa: BLE001
        _unavailable(f"docker or image {IMAGE} not available: {exc}")
    return c


def _run(client, network: str, argv: list[str], name: str | None = None, detach: bool = False):
    container = client.containers.run(
        IMAGE,
        argv,
        name=name,
        network=network,
        detach=True,
        user="10001",
        read_only=True,
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        entrypoint=[],
    )
    if detach:
        return container
    # Read the output ourselves: containers.run() without detach returns None instead of the
    # output when the daemon's log driver is not json-file or journald (Docker Desktop's
    # default is "local").
    try:
        container.wait(timeout=60)
        return container.logs(stdout=True, stderr=False)
    finally:
        container.remove(force=True)


@pytest.fixture
def internal_net(client) -> Iterator[str]:
    name = f"lnd-egress-{uuid.uuid4().hex[:8]}"
    net = client.networks.create(name, driver="bridge", internal=True)
    try:
        yield name
    finally:
        for c in client.containers.list(all=True, filters={"network": net.id}):
            c.remove(force=True)
        net.remove()


def test_internal_containers_can_talk_to_each_other(client, internal_net):
    server = _run(
        client, internal_net, ["python3", "-c", SERVE], name=f"{internal_net}-srv", detach=True
    )
    try:
        time.sleep(1.0)
        out = _run(client, internal_net, ["python3", "-c", CONNECT, f"{internal_net}-srv", "8080"])
        assert out.decode().startswith("CONNECTED"), out
    finally:
        server.remove(force=True)


@pytest.mark.parametrize("target", [("1.1.1.1", "443"), ("8.8.8.8", "53"), ("example.com", "443")])
def test_internal_containers_cannot_reach_the_internet(client, internal_net, target):
    out = _run(client, internal_net, ["python3", "-c", CONNECT, *target])
    assert out.decode().startswith("BLOCKED"), out


def test_internal_network_is_flagged_internal(client, internal_net):
    assert client.networks.get(internal_net).attrs["Internal"] is True


def test_no_masquerade_bridge_cannot_reach_the_internet(client):
    """The `edge` network from docker-compose.yml: a bridge with NAT switched off."""
    name = f"lnd-edge-{uuid.uuid4().hex[:8]}"
    ordinary = client.networks.create(f"{name}-plain", driver="bridge")
    edge = client.networks.create(
        name,
        driver="bridge",
        options={"com.docker.network.bridge.enable_ip_masquerade": "false"},
    )
    try:
        control = _run(client, ordinary.name, ["python3", "-c", CONNECT, "1.1.1.1", "443"])
        if not control.decode().startswith("CONNECTED"):
            pytest.skip("this host has no direct internet from containers, so there is no control")
        out = _run(client, edge.name, ["python3", "-c", CONNECT, "1.1.1.1", "443"])
        if out.decode().startswith("CONNECTED") and client.info()["OperatingSystem"] == (
            "Docker Desktop"
        ):
            # Measured on Docker Desktop 4.95 for Mac (Engine 29.8): its VM's network still
            # carries traffic from a bridge with masquerading off, so `edge` does not seal the
            # API there.
            pytest.xfail("Docker Desktop: a no-masquerade bridge still reaches the internet")
        assert out.decode().startswith("BLOCKED"), out
    finally:
        ordinary.remove()
        edge.remove()
