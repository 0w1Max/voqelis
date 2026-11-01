from pathlib import Path

import pytest

from voqelis.config import load_settings


def _prepare_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123")
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("MODEL_CACHE_DIR", str(tmp_path / "models"))
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    monkeypatch.delenv("ASR_WORKER_PYTHON", raising=False)


def test_asr_backend_defaults_to_whisper(monkeypatch, tmp_path) -> None:
    _prepare_env(monkeypatch, tmp_path)

    settings = load_settings(tmp_path / "missing.env")

    assert settings.asr_backend == "whisper"
    assert settings.asr_worker_python == Path(".venv-asr-benchmark/bin/python")


@pytest.mark.parametrize(
    "backend",
    ["gigaam-v3-rnnt-int8", "gigaam-v3-ctc-int8"],
)
def test_asr_backend_can_select_gigaam(monkeypatch, tmp_path, backend) -> None:
    _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ASR_BACKEND", backend)
    monkeypatch.setenv("ASR_WORKER_PYTHON", "/isolated/python")

    settings = load_settings(tmp_path / "missing.env")

    assert settings.asr_backend == backend
    assert settings.asr_worker_python == Path("/isolated/python")


def test_asr_backend_rejects_unknown_value(monkeypatch, tmp_path) -> None:
    _prepare_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ASR_BACKEND", "mystery-model")

    with pytest.raises(ValueError, match="ASR_BACKEND"):
        load_settings(tmp_path / "missing.env")
