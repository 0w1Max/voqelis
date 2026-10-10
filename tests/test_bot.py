import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis import bot as bot_module
from voqelis.domain import AudioJob, TranscriptionResult
from voqelis.queue import JobQueue


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.message_sent = asyncio.Event()

    async def send_chat_action(self, chat_id: int, action) -> None:
        return None

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text))
        self.message_sent.set()


class FakeTranscriber:
    def __init__(self, result: TranscriptionResult) -> None:
        self.result = result

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        return self.result


async def run_one_job(tmp_path: Path, monkeypatch, result: TranscriptionResult) -> list[tuple[int, str]]:
    audio_path = tmp_path / "input.ogg"
    audio_path.write_bytes(b"test audio")
    monkeypatch.setattr(bot_module, "probe_duration_seconds", lambda _path: 1.0)

    queue = JobQueue(max_pending_jobs=1, max_pending_per_user=1)
    assert await queue.reserve(123)
    await queue.put(
        AudioJob(
            chat_id=70,
            reply_to_message_id=80,
            user_id=123,
            file_path=audio_path,
        )
    )
    bot = FakeBot()
    worker = asyncio.create_task(
        bot_module.run_worker(
            bot=bot,
            queue=queue,
            transcriber=FakeTranscriber(result),
            settings=SimpleNamespace(max_audio_seconds=1200),
        )
    )
    try:
        await asyncio.wait_for(bot.message_sent.wait(), timeout=2.0)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    return bot.messages


def make_result(*, text: str, pause_aware_text: str | None) -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        language="ru",
        language_probability=0.0,
        duration_seconds=1.0,
        duration_after_vad_seconds=1.0,
        processing_seconds=0.1,
        pause_aware_text=pause_aware_text,
    )


@pytest.mark.asyncio
async def test_plain_transcription_returns_pause_aware_text(tmp_path: Path, monkeypatch) -> None:
    messages = await run_one_job(
        tmp_path,
        monkeypatch,
        make_result(
            text="для здоровья для настроения",
            pause_aware_text="для здоровья, для настроения",
        ),
    )

    assert messages == [(70, "для здоровья, для настроения")]


@pytest.mark.asyncio
async def test_plain_transcription_falls_back_to_original_text(tmp_path: Path, monkeypatch) -> None:
    messages = await run_one_job(
        tmp_path,
        monkeypatch,
        make_result(
            text="исходный текст",
            pause_aware_text=None,
        ),
    )

    assert messages == [(70, "исходный текст")]
