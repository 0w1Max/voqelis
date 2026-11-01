import json
from datetime import date
from pathlib import Path

from voqelis.asr_gguf_test_bot import GgufTestManager


def make_manager(tmp_path: Path) -> GgufTestManager:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    worker = tmp_path / "worker.py"
    worker.write_text("pass\n", encoding="utf-8")
    return GgufTestManager(
        data_dir=tmp_path / "samples",
        timezone="Europe/Moscow",
        model_path=model,
        python_path=python,
        worker_path=worker,
        repo_root=tmp_path,
    )


def test_manager_creates_persistent_session_and_reuses_active_one(tmp_path):
    manager = make_manager(tmp_path)
    today = date(2026, 10, 12)

    session, created = manager.begin(user_id=123, chat_id=456, today=today)
    assert created is True
    assert manager.has_session(123)
    assert manager.has_any_session()
    assert session.state == "awaiting_audio"

    document = json.loads(
        (session.directory / "session.json").read_text(encoding="utf-8")
    )
    assert document["session_id"] == session.session_id
    assert document["state"] == "awaiting_audio"

    same_session, created_again = manager.begin(
        user_id=123,
        chat_id=456,
        today=today,
    )
    assert created_again is False
    assert same_session is session


def test_manager_close_preserves_recording_artifacts(tmp_path):
    manager = make_manager(tmp_path)
    session, _ = manager.begin(
        user_id=123,
        chat_id=456,
        today=date(2026, 10, 12),
    )
    recording = session.directory / "audio.wav"
    recording.write_bytes(b"preserve me")

    status, closed = manager.close_session(123, "complete", reference_chars=12)

    assert status == "closed"
    assert closed is session
    assert not manager.has_session(123)
    assert not manager.has_any_session()
    assert recording.read_bytes() == b"preserve me"
    saved = json.loads(
        (session.directory / "session.json").read_text(encoding="utf-8")
    )
    assert saved["state"] == "complete"
    assert saved["reference_chars"] == 12


def test_manager_does_not_close_while_inference_is_running(tmp_path):
    manager = make_manager(tmp_path)
    session, _ = manager.begin(
        user_id=123,
        chat_id=456,
        today=date(2026, 10, 12),
    )
    manager.update(session, "processing")

    status, returned = manager.close_session(123, "cancelled")

    assert status == "running"
    assert returned is session
    assert manager.has_session(123)


def test_manager_available_checks_all_runtime_files(tmp_path):
    manager = make_manager(tmp_path)
    assert manager.available
    manager.worker_path.unlink()
    assert not manager.available
