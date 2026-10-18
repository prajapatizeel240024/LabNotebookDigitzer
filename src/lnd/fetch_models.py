"""Download the model files once into a volume (`make models`).

This is the only step that needs the internet. The stack itself runs with no route out: the
model server just reads the files from the volume. Files come from the repo revision pinned in
models.json and are checked against the size and sha256 pinned there (or, for a file that is
not pinned, the sha256 Hugging Face reports), so a truncated, corrupt or different file is
rejected, and an interrupted download resumes where it stopped.

    python -m lnd.fetch_models /models
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

CHUNK = 1024 * 1024


def load_manifest() -> dict[str, Any]:
    return json.loads(resources.files("lnd").joinpath("models.json").read_text())


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def remote_facts(client: httpx.Client, url: str) -> tuple[int, str | None]:
    """Size and sha256 as Hugging Face reports them (x-linked-* headers on the LFS redirect)."""
    res = client.head(url, follow_redirects=False)
    size = res.headers.get("x-linked-size") or res.headers.get("content-length")
    etag = (res.headers.get("x-linked-etag") or "").strip('"')
    if not size:
        raise RuntimeError(f"{url} did not report a file size (HTTP {res.status_code})")
    return int(size), etag if re.fullmatch(r"[0-9a-f]{64}", etag) else None


def expected_sha256(f: dict[str, Any], size: int, reported: str | None) -> str:
    """The sha256 to verify a file against, after comparing the pins in models.json with what
    Hugging Face reports. Raises if they disagree or there is nothing to verify against."""
    pinned_size = f.get("size")
    if pinned_size is not None and pinned_size != size:
        raise RuntimeError(
            f"{f['name']}: Hugging Face reports {size} bytes, models.json pins {pinned_size}"
        )
    pinned = f.get("sha256")
    if pinned and reported and pinned != reported:
        raise RuntimeError(f"{f['name']}: pinned sha256 differs from the one reported")
    want = pinned or reported
    if not want:
        raise RuntimeError(f"{f['name']}: no sha256 available to verify against")
    return want


def download_file(
    client: httpx.Client, url: str, dest: Path, *, sha256: str, size: int, log=print
) -> None:
    if dest.exists() and dest.stat().st_size == size and sha256_of(dest) == sha256:
        log(f"already have {dest.name}")
        return
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    log(f"downloading {dest.name} ({size / 1e9:.2f} GB){' (resuming)' if have else ''}")
    with client.stream("GET", url, headers=headers, follow_redirects=True) as res:
        if have and res.status_code == 200:  # server ignored the range: start over
            have = 0
        elif res.status_code not in (200, 206):
            raise RuntimeError(f"{url} answered HTTP {res.status_code}")
        done, next_report = have, 0.0
        with part.open("ab" if have else "wb") as out:
            for chunk in res.iter_bytes(CHUNK):
                out.write(chunk)
                done += len(chunk)
                if done / size >= next_report:
                    log(f"  {dest.name}: {100 * done / size:.0f}%")
                    next_report += 0.1
    if part.stat().st_size != size or sha256_of(part) != sha256:
        part.unlink()
        raise RuntimeError(f"{dest.name} failed its size or sha256 check; deleted, try again")
    part.replace(dest)
    log(f"saved {dest}")


def main(argv: list[str]) -> int:
    dest_dir = Path(argv[1] if len(argv) > 1 else "models")
    dest_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    revision = manifest.get("revision", "main")
    print(f"{manifest['name']}  license: {manifest['license']}  from {manifest['source_url']}")
    with httpx.Client(timeout=httpx.Timeout(60, read=120)) as client:
        for f in manifest["files"]:
            url = f"https://huggingface.co/{manifest['repo']}/resolve/{revision}/{f['name']}"
            size, reported = remote_facts(client, url)
            want = expected_sha256(f, size, reported)
            if not f.get("sha256"):
                print(f"  sha256 {want} (not pinned in models.json; add it to pin)")
            download_file(client, url, dest_dir / f["name"], sha256=want, size=size)
    print("done. The stack can now run without internet access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
