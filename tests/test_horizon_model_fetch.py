"""Safety checks for optional Horizon model downloader."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.horizon_model_fetch import existing_digest, safe_destination


def test_download_destination_confined_to_voqelis_data() -> None:
    with pytest.raises(ValueError, match="inside /var/lib/voqelis"):
        safe_destination(Path("/tmp/horizon"))
    with pytest.raises(ValueError, match="inside /var/lib/voqelis"):
        safe_destination(Path("/var/lib/voqelis"))
    assert safe_destination(Path("/var/lib/voqelis/horizon-onnx-pilot")) == Path(
        "/var/lib/voqelis/horizon-onnx-pilot"
    )


def test_existing_asset_size_hash_and_symlink_checks(tmp_path: Path) -> None:
    file = tmp_path / "config.json"
    file.write_bytes(b"test")
    assert len(existing_digest(file, 1, 100)) == 64
    with pytest.raises(ValueError, match="Unexpected local size"):
        existing_digest(file, 5, 100)
    symlink = tmp_path / "link"
    symlink.symlink_to(file)
    with pytest.raises(ValueError, match="Unsafe local"):
        existing_digest(symlink, 1, 100)
