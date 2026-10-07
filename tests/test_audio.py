import asyncio
import sqlite3
from pathlib import Path

import pytest

from voqelis.audio import is_audio_document
from voqelis.bot import run_worker
from voqelis.domain import AudioJob, TranscriptionResult
from voqelis.planner.config import PlannerConfig

def test_audio_mime_is_accepted():
    assert is_audio_document(mime_type="audio/mpeg", file_name="unknown.bin")


def test_known_audio_extension_is_accepted():
    assert is_audio_document(mime_type=None, file_name="recording.m4a")


def test_non_audio_document_is_rejected():
    assert not is_audio_document(mime_type="application/pdf", file_name="file.pdf")



@pytest.mark.asyncio
async def test_run_worker_debug_logging_accepts_sqlite_row(monkeypatch, tmp_path):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    session = connection.execute(
        "SELECT 'planning' AS state, '2026-10-09' AS target_day"
    ).fetchone()

    class FakeStore:
        def session(self, user_id):
            assert user_id == 1
            return session

    class FakePlanner:
        config = PlannerConfig()

        def __init__(self):
            self.store = FakeStore()

        async def handle_text(self, user_id, text, today):
            assert user_id == 1
            assert text == "завтра тест"
            return ["ok"]

    class FakeBot:
        async def send_chat_action(self, chat_id, action):
            assert chat_id == 10

        async def send_message(self, chat_id, text, **kwargs):
            assert chat_id == 10
            assert text == "ok"

    class FakeTranscriber:
        async def transcribe(self, audio_path):
            assert audio_path
            return TranscriptionResult(
                text="завтра тест",
                language="ru",
                language_probability=1.0,
                duration_seconds=1.0,
                duration_after_vad_seconds=1.0,
                processing_seconds=0.1,
                pause_aware_text="завтра тест",
            )

    class FakeQueue:
        def __init__(self, job):
            self.job = job
            self.done = False

        async def get(self):
            if self.done:
                raise asyncio.CancelledError
            self.done = True
            return self.job

        async def release(self, user_id):
            assert user_id == 1

        def task_done(self):
            pass

    audio_path = Path(tmp_path) / "input.ogg"
    audio_path.write_bytes(b"test")
    job = AudioJob(chat_id=10, reply_to_message_id=20, user_id=1, file_path=audio_path)

    monkeypatch.setattr("voqelis.bot.probe_duration_seconds", lambda path: 1.0)

    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            bot=FakeBot(),
            queue=FakeQueue(job),
            transcriber=FakeTranscriber(),
            settings=None,
            planner=FakePlanner(),
            log_content=True,
        )

    connection.close()
