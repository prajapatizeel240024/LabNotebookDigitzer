import hashlib
import re

import httpx
import pytest

from lnd.fetch_models import (
    download_file,
    expected_sha256,
    load_manifest,
    remote_facts,
    sha256_of,
)

PAYLOAD = bytes(range(256)) * 400  # 102,400 bytes


def server(calls: list[str], payload: bytes = PAYLOAD):
    def handler(request: httpx.Request) -> httpx.Response:
        rng = request.headers.get("range")
        calls.append(rng or "full")
        if rng:
            start = int(re.match(r"bytes=(\d+)-", rng).group(1))
            return httpx.Response(206, content=payload[start:])
        return httpx.Response(200, content=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_downloads_and_verifies(tmp_path):
    calls: list[str] = []
    dest = tmp_path / "m.gguf"
    download_file(
        server(calls),
        "http://x/m",
        dest,
        sha256=sha(PAYLOAD),
        size=len(PAYLOAD),
        log=lambda _: None,
    )
    assert dest.read_bytes() == PAYLOAD and not (tmp_path / "m.gguf.part").exists()
    assert calls == ["full"]


def test_existing_good_file_is_not_downloaded_again(tmp_path):
    dest = tmp_path / "m.gguf"
    dest.write_bytes(PAYLOAD)
    calls: list[str] = []
    download_file(
        server(calls),
        "http://x/m",
        dest,
        sha256=sha(PAYLOAD),
        size=len(PAYLOAD),
        log=lambda _: None,
    )
    assert calls == []


def test_interrupted_download_resumes(tmp_path):
    dest = tmp_path / "m.gguf"
    (tmp_path / "m.gguf.part").write_bytes(PAYLOAD[:50_000])
    calls: list[str] = []
    download_file(
        server(calls),
        "http://x/m",
        dest,
        sha256=sha(PAYLOAD),
        size=len(PAYLOAD),
        log=lambda _: None,
    )
    assert calls == ["bytes=50000-"] and dest.read_bytes() == PAYLOAD


def test_wrong_hash_is_rejected_and_deleted(tmp_path):
    dest = tmp_path / "m.gguf"
    with pytest.raises(RuntimeError, match="sha256"):
        download_file(
            server([]), "http://x/m", dest, sha256="0" * 64, size=len(PAYLOAD), log=lambda _: None
        )
    assert not dest.exists() and not (tmp_path / "m.gguf.part").exists()


def test_manifest_names_the_exact_files_and_license():
    m = load_manifest()
    assert m["license"] == "Apache-2.0"
    names = [f["name"] for f in m["files"]]
    assert names == [
        "Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf",
        "mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf",
    ]


def test_manifest_pins_revision_size_and_sha256():
    m = load_manifest()
    assert re.fullmatch(r"[0-9a-f]{40}", m["revision"])
    for f in m["files"]:
        assert re.fullmatch(r"[0-9a-f]{64}", f["sha256"]), f["name"]
        assert isinstance(f["size"], int) and f["size"] > 0, f["name"]


def test_pinned_size_or_sha256_that_differs_from_the_remote_is_refused():
    good = "ab" * 32
    f = {"name": "m.gguf", "size": 1234, "sha256": good}
    assert expected_sha256(f, 1234, good) == good
    assert expected_sha256(f, 1234, None) == good  # pin is enough when the remote says nothing
    with pytest.raises(RuntimeError, match="1235 bytes"):
        expected_sha256(f, 1235, good)
    with pytest.raises(RuntimeError, match="differs"):
        expected_sha256(f, 1234, "cd" * 32)


def test_unpinned_file_uses_the_reported_sha256_and_needs_one():
    f = {"name": "m.gguf", "size": None, "sha256": None}
    assert expected_sha256(f, 10, "ab" * 32) == "ab" * 32
    with pytest.raises(RuntimeError, match="no sha256"):
        expected_sha256(f, 10, None)


def test_sha256_helper(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    assert sha256_of(p) == sha(b"abc")


def test_remote_facts_reads_the_linked_headers():
    good = "ab" * 32

    def handler(request):
        return httpx.Response(
            302, headers={"x-linked-size": "1234", "x-linked-etag": f'"{good}"', "location": "/x"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert remote_facts(client, "http://x/m") == (1234, good)
