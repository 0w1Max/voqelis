import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import voqelis.asr_test_bot as asr_test_bot
from voqelis.asr_test_bot import ASR_TEST_CASES, AsrBenchmarkManager


@pytest.mark.asyncio
async def test_telegram_session_collects_recordings_in_order_with_each_recording_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_convert(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"WAV:" + source.read_bytes())

    monkeypatch.setattr(asr_test_bot, "convert_audio_to_wav", fake_convert)
    manager = AsrBenchmarkManager(
        data_dir=tmp_path / "runs",
        timezone="Europe/Moscow",
        repo_root=tmp_path,
        benchmark_python=tmp_path / "python",
    )
    start_day = date(2026, 10, 9)
    session, created = manager.begin(user_id=7, chat_id=70, today=start_day)

    assert created
    assert manager.has_session(7)
    assert session.next_case == 0
    assert "1/12" in manager.prompt_text(0)

    for index in range(len(ASR_TEST_CASES)):
        source = tmp_path / f"input-{index:02d}.ogg"
        source.write_bytes(f"sample-{index}".encode())
        recording_day = start_day + timedelta(days=index)
        updated_session, accepted_index, complete = await manager.accept_recording(
            user_id=7,
            source_path=source,
            today=recording_day,
        )
        assert updated_session is session
        assert accepted_index == index
        assert complete is (index == len(ASR_TEST_CASES) - 1)
        assert not source.exists()

    assert session.state == "running"
    manifest = json.loads(session.manifest_path.read_text(encoding="utf-8"))
    assert len(manifest["cases"]) == len(ASR_TEST_CASES)
    assert manifest["today"] == start_day.isoformat()
    assert manifest["cases"][0]["today"] == start_day.isoformat()
    assert manifest["cases"][1]["today"] == (start_day + timedelta(days=1)).isoformat()
    assert manifest["cases"][0]["audio"] == "audio/case01.wav"
    assert manifest["cases"][-1]["reference"] == ASR_TEST_CASES[-1][1]


def test_cancel_removes_incomplete_telegram_test_session(tmp_path: Path) -> None:
    manager = AsrBenchmarkManager(
        data_dir=tmp_path / "runs",
        timezone="Europe/Moscow",
        repo_root=tmp_path,
        benchmark_python=tmp_path / "python",
    )
    session, _ = manager.begin(
        user_id=3,
        chat_id=30,
        today=date(2026, 10, 9),
    )

    assert manager.cancel(3) == "cancelled"
    assert not session.directory.exists()
    assert not manager.has_session(3)
    assert manager.cancel(3) == "missing"
