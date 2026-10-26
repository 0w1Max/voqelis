"""Explicit, revision-pinned download of Horizon ONNX-only assets (no PyTorch).

This is an opt-in benchmark utility, not used by the live Telegram bot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

REVISION = "ee31fdf"  # v1.1 release, 2026-10-09
BASE = (
    "https://huggingface.co/Horizon-Labs/"
    f"punctuation-restoration-small/resolve/{REVISION}"
)
FILES = {
    "model_quantized.onnx": ("onnx/model_quantized.onnx", 200_000_000, 350_000_000),
    "tokenizer.json": ("tokenizer.json", 1_000_000, 60_000_000),
    "config.json": ("config.json", 300, 100_000),
}


def safe_destination(path: Path) -> Path:
    """Refuse writes outside the application data tree or through symlinks."""
    root = Path("/var/lib/voqelis")
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Destination must be inside /var/lib/voqelis")
    for parent in (path, *path.parents):
        if parent.exists() and parent.is_symlink():
            raise ValueError("Symlink in experiment destination")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("Destination is not a directory")
    return resolved


def fetch_one(url: str, path: Path, minimum: int, maximum: int) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Voqelis-ONNX-benchmark/1.0"}
    )
    temp_name: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            announced = response.headers.get("Content-Length")
            if announced and int(announced) > maximum:
                raise ValueError("Remote file exceeds allowed size")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".horizon-", dir=path.parent, delete=False
            ) as temp:
                temp_name = temp.name
                digest = hashlib.sha256()
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > maximum:
                        raise ValueError("Download exceeds size cap")
                    digest.update(block)
                    temp.write(block)
                temp.flush()
                os.fsync(temp.fileno())
            if not minimum <= total <= maximum:
                raise ValueError(f"Unexpected file size: {total}")
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite: {path}")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        temp_name = None
        print(f"{path.name}: {total} bytes; SHA256={digest.hexdigest()}", flush=True)
        return digest.hexdigest()
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def existing_digest(path: Path, minimum: int, maximum: int) -> str:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Unsafe local file: {path}")
    if not minimum <= path.stat().st_size <= maximum:
        raise ValueError(f"Unexpected local size: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--accept-download", action="store_true")
    args = parser.parse_args()
    if not args.accept_download:
        parser.error("Model download requires --accept-download")
    destination = safe_destination(args.dest)
    free = shutil.disk_usage("/var/lib/voqelis").free
    if free < 700_000_000:
        raise SystemExit(f"STOP: free disk {free} bytes; need >=700MB")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    manifest = destination / "download_manifest.json"
    if manifest.exists() and (manifest.is_symlink() or not manifest.is_file()):
        raise ValueError("Unsafe local manifest")
    old = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    if old and old.get("revision") != REVISION:
        raise ValueError("Model revision mismatch")
    digests = {}
    for name, (remote, minimum, maximum) in FILES.items():
        path = destination / name
        if path.exists():
            digests[name] = existing_digest(path, minimum, maximum)
            if old and old.get("sha256", {}).get(name) != digests[name]:
                raise ValueError(f"SHA256 mismatch: {name}")
            print(f"{name}: verified existing file", flush=True)
        else:
            digests[name] = fetch_one(
                f"{BASE}/{remote}?download=true", path, minimum, maximum
            )
    config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
    if set(config.get("id2label", {}).values()) != {"0", ".", ",", "?", ":", "-"}:
        raise ValueError("Unknown model labels")
    data = {
        "repository": "Horizon-Labs/punctuation-restoration-small",
        "revision": REVISION,
        "sha256": digests,
    }
    temp_manifest = manifest.with_name(".download_manifest.tmp")
    temp_manifest.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temp_manifest, 0o600)
    os.replace(temp_manifest, manifest)
    print("ONNX-only assets ready. Production unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
