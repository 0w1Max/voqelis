import asyncio
import json
import re
import wave
from pathlib import Path

import pytest

from voqelis.domain import TranscriptSegment
from voqelis.transcription import (
    Transcriber,
    _group_punctuation_segments,
    _has_significant_vad_pause,
    _write_wav_window,
)

_WORD_RE = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)


def _words(text: str) -> list[str]:
    return [
        match.group().casefold().replace("ё", "е")
        for match in _WORD_RE.finditer(text)
    ]


def _make_transcriber(tmp_path: Path, *, backend: str) -> Transcriber:
    transcriber = object.__new__(Transcriber)
    transcriber.settings = type("TestSettings", (), {"temp_dir": tmp_path})()
    transcriber._repo_root = tmp_path
    transcriber._punctuation_backend = backend
    transcriber._punctuation_model_path = tmp_path / "model.gguf"
    transcriber._punctuation_worker_python = tmp_path / "python"
    transcriber._punctuation_worker_path = (
        tmp_path / "tools" / "asr_gguf_transcribe.py"
    )

    transcriber._punctuation_model_path.touch()
    transcriber._punctuation_worker_python.touch()
    transcriber._punctuation_worker_path.parent.mkdir()
    transcriber._punctuation_worker_path.touch()
    return transcriber


class _FakeProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_punctuation_stage_disabled_does_not_launch_worker(
    tmp_path: Path, monkeypatch
) -> None:
    transcriber = _make_transcriber(tmp_path, backend="none")

    async def unexpected_process(*args, **kwargs):
        raise AssertionError("Disabled punctuation stage must not launch GGUF")

    monkeypatch.setattr(
        "voqelis.transcription.asyncio.create_subprocess_exec",
        unexpected_process,
    )

    rnnt_text = "это тест списка для здоровья для настроения"
    result = asyncio.run(
        transcriber._apply_gigaam_punctuation(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == rnnt_text


def test_projects_punctuation_without_changing_rnnt_words(
    tmp_path: Path, monkeypatch
) -> None:
    transcriber = _make_transcriber(
        tmp_path, backend="gigaam-v3-e2e-ctc-gguf"
    )
    e2e_text = (
        "Это тест списка. Для здоровья, для настроения, "
        "для духовного опыта."
    )
    rnnt_text = (
        "это тест списка для здоровья для настроения "
        "для духовного опыта"
    )

    async def fake_create_subprocess_exec(*command, **kwargs):
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(
            json.dumps({"text": e2e_text}, ensure_ascii=False),
            encoding="utf-8",
        )
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(
        "voqelis.transcription.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = asyncio.run(
        transcriber._apply_gigaam_punctuation(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == (
        "Это тест списка. Для здоровья, для настроения, "
        "для духовного опыта."
    )
    assert _words(result) == _words(rnnt_text)


def test_missing_model_falls_back_without_starting_worker(
    tmp_path: Path, monkeypatch
) -> None:
    transcriber = _make_transcriber(
        tmp_path, backend="gigaam-v3-e2e-ctc-gguf"
    )
    transcriber._punctuation_model_path.unlink()

    async def unexpected_process(*args, **kwargs):
        raise AssertionError("Missing model must not launch GGUF")

    monkeypatch.setattr(
        "voqelis.transcription.asyncio.create_subprocess_exec",
        unexpected_process,
    )

    rnnt_text = "с двадцати трех вечера до часа ночи подготовиться ко сну"
    result = asyncio.run(
        transcriber._apply_gigaam_punctuation(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == rnnt_text


def test_failed_worker_preserves_original_rnnt_text(
    tmp_path: Path, monkeypatch
) -> None:
    transcriber = _make_transcriber(
        tmp_path, backend="gigaam-v3-e2e-ctc-gguf"
    )

    async def failed_process(*command, **kwargs):
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(
        "voqelis.transcription.asyncio.create_subprocess_exec",
        failed_process,
    )

    rnnt_text = "завтра с девяти до одиннадцати часов написать отчет"
    result = asyncio.run(
        transcriber._apply_gigaam_punctuation(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == rnnt_text


def test_weak_alignment_preserves_original_rnnt_text(
    tmp_path: Path, monkeypatch
) -> None:
    transcriber = _make_transcriber(
        tmp_path, backend="gigaam-v3-e2e-ctc-gguf"
    )

    async def fake_create_subprocess_exec(*command, **kwargs):
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text(
            json.dumps(
                {"text": "совершенно другие слова неизвестной фразы"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _FakeProcess(returncode=0)

    monkeypatch.setattr(
        "voqelis.transcription.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    rnnt_text = "завтра с девяти до одиннадцати часов написать отчет"
    result = asyncio.run(
        transcriber._apply_gigaam_punctuation(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == rnnt_text


def test_pause_gate_is_false_without_two_segments() -> None:
    assert not _has_significant_vad_pause(())
    assert not _has_significant_vad_pause(
        (TranscriptSegment('привет', 0.0, 0.5),)
    )


def test_pause_gate_rejects_gap_below_threshold() -> None:
    segments = (
        TranscriptSegment('первая', 0.0, 1.0),
        TranscriptSegment('вторая', 1.599, 2.0),
    )
    assert not _has_significant_vad_pause(segments)


def test_pause_gate_accepts_gap_at_threshold() -> None:
    segments = (
        TranscriptSegment('первая', 0.0, 1.0),
        TranscriptSegment('вторая', 1.6, 2.0),
    )
    assert _has_significant_vad_pause(segments)



def test_group_punctuation_segments_keeps_short_sequence_together() -> None:
    segments = (
        TranscriptSegment("первая", 0.0, 8.0),
        TranscriptSegment("вторая", 8.5, 15.0),
        TranscriptSegment("третья", 16.0, 21.9),
    )

    groups = _group_punctuation_segments(segments)

    assert groups == (segments,)


def test_group_punctuation_segments_splits_at_window_boundary() -> None:
    segments = (
        TranscriptSegment("первая", 0.0, 10.0),
        TranscriptSegment("вторая", 10.5, 20.0),
        TranscriptSegment("третья", 20.5, 22.5),
    )

    groups = _group_punctuation_segments(segments)

    assert groups == ((segments[0], segments[1]), (segments[2],))
    assert [segment for group in groups for segment in group] == list(segments)


def test_group_punctuation_segments_rejects_empty_input() -> None:
    assert _group_punctuation_segments(()) is None


def test_group_punctuation_segments_rejects_oversized_single_segment() -> None:
    segments = (TranscriptSegment("длинный", 0.0, 22.1),)

    assert _group_punctuation_segments(segments) is None


def test_group_punctuation_segments_rejects_overlapping_segments() -> None:
    segments = (
        TranscriptSegment("первая", 0.0, 2.0),
        TranscriptSegment("вторая", 1.9, 3.0),
    )

    assert _group_punctuation_segments(segments) is None



def test_write_wav_window_preserves_exact_pcm_frames(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "window.wav"
    pcm = bytes(range(256)) * 125

    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm)

    duration = _write_wav_window(source, destination, 0.25, 0.75)

    with wave.open(str(destination), "rb") as wav:
        result = wav.readframes(wav.getnframes())

    assert duration == 0.5
    assert len(result) == 16000
    assert result == pcm[8000:24000]


def test_write_wav_window_rejects_more_than_22_seconds(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    destination = tmp_path / "window.wav"

    with wave.open(str(source), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(16000 * 2))

    with pytest.raises(ValueError, match="Invalid or oversized"):
        _write_wav_window(source, destination, 0.0, 22.01)

    assert not destination.exists()
