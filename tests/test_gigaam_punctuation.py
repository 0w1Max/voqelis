import asyncio
import json
import re
from pathlib import Path

import pytest

from voqelis.asr_punctuation import GgufPunctuationProcessor
from voqelis.domain import TranscriptSegment
from voqelis.transcription import _has_significant_vad_pause

_WORD_RE = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)


def _words(text: str) -> list[str]:
    return [
        match.group().casefold().replace("ё", "е")
        for match in _WORD_RE.finditer(text)
    ]


def _make_processor(tmp_path: Path) -> GgufPunctuationProcessor:
    model_path = tmp_path / "model.gguf"
    worker_python = tmp_path / "python"
    worker_path = tmp_path / "tools" / "asr_gguf_transcribe.py"

    model_path.touch()
    worker_python.touch()
    worker_path.parent.mkdir()
    worker_path.touch()
    return GgufPunctuationProcessor(
        model_path=model_path,
        worker_python=worker_python,
        worker_path=worker_path,
        repo_root=tmp_path,
        temp_dir=tmp_path,
    )

class _FakeProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_punctuation_stage_disabled_does_not_construct_worker() -> None:
    settings = type("TestSettings", (), {"asr_punctuation_backend": "none"})()
    assert GgufPunctuationProcessor.from_settings(
        settings, repo_root=Path("/unused")
    ) is None

def test_projects_punctuation_without_changing_rnnt_words(
    tmp_path: Path, monkeypatch
) -> None:
    processor = _make_processor(tmp_path)
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
        "voqelis.asr_punctuation.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = asyncio.run(
        processor.apply(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == (
        "Это тест списка. Для здоровья, для настроения, "
        "для духовного опыта."
    )
    assert _words(result) == _words(rnnt_text)


def test_missing_model_falls_back_without_starting_worker(
    tmp_path: Path, monkeypatch
) -> None:
    processor = _make_processor(tmp_path)
    processor.model_path.unlink()

    async def unexpected_process(*args, **kwargs):
        raise AssertionError("Missing model must not launch GGUF")

    monkeypatch.setattr(
        "voqelis.asr_punctuation.asyncio.create_subprocess_exec",
        unexpected_process,
    )

    rnnt_text = "с двадцати трех вечера до часа ночи подготовиться ко сну"
    result = asyncio.run(
        processor.apply(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == rnnt_text


def test_failed_worker_preserves_original_rnnt_text(
    tmp_path: Path, monkeypatch
) -> None:
    processor = _make_processor(tmp_path)

    async def failed_process(*command, **kwargs):
        return _FakeProcess(returncode=1)

    monkeypatch.setattr(
        "voqelis.asr_punctuation.asyncio.create_subprocess_exec",
        failed_process,
    )

    rnnt_text = "завтра с девяти до одиннадцати часов написать отчет"
    result = asyncio.run(
        processor.apply(tmp_path / "audio.wav", rnnt_text)
    )

    assert result == rnnt_text


def test_weak_alignment_preserves_original_rnnt_text(
    tmp_path: Path, monkeypatch
) -> None:
    processor = _make_processor(tmp_path)

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
        "voqelis.asr_punctuation.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    rnnt_text = "завтра с девяти до одиннадцати часов написать отчет"
    result = asyncio.run(
        processor.apply(tmp_path / "audio.wav", rnnt_text)
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
