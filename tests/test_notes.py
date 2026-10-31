"""Notes V1: persistence, authorization, explicit capture and Planner isolation."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.bot import run_worker
from voqelis.domain import AudioJob, TranscriptionResult
from voqelis.notes.bot import _argument, create_notes_router
from voqelis.notes.service import NotesService
from voqelis.notes.store import (
    CAPTURE_TTL_SECONDS,
    MAX_NOTE_CHARS,
    NotesStore,
)


@pytest.fixture
def notes_store(tmp_path: Path):
    store = NotesStore(tmp_path / "notes.sqlite3")
    yield store
    store.close()


def test_notes_database_is_separate_from_planner(tmp_path: Path) -> None:
    planner_db = tmp_path / "planner.sqlite3"
    sqlite3.connect(planner_db).close()
    store = NotesStore(planner_db.with_name("notes.sqlite3"))
    try:
        store.create_text(1, "первая заметка")
        with sqlite3.connect(planner_db) as other:
            tables = {
                row[0] for row in other.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "notes" not in tables
        assert store.path.name == "notes.sqlite3"
    finally:
        store.close()


def test_text_note_persists_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite3"
    store = NotesStore(path)
    note = store.create_text(42, "  Купи молоко \n и хлеб  ")
    store.close()
    second = NotesStore(path)
    try:
        found = second.get(42, note.id)
        assert found is not None
        assert found.body == "Купи молоко \n и хлеб"
        assert found.original_text == found.body
        assert found.source == "text"
    finally:
        second.close()


def test_user_cannot_read_or_search_another_users_note(notes_store) -> None:
    note = notes_store.create_text(11, "Секретный проект Солнце")
    assert notes_store.get(12, note.id) is None
    assert notes_store.recent(12) == []
    assert notes_store.search(12, "Солнце") == []
    assert [r.id for r in notes_store.search(11, "проект")] == [note.id]


def test_search_casefold_unicode_and_literal_sql_characters(notes_store) -> None:
    one = notes_store.create_text(11, "Ёжик и ПРОЕКТ")
    two = notes_store.create_text(11, "Это 100% результат, пример_a")
    notes_store.create_text(12, "проект")
    assert [n.id for n in notes_store.search(11, "ёЖик")] == [one.id]
    assert [n.id for n in notes_store.search(11, "ежик")] == [one.id]
    assert [n.id for n in notes_store.search(11, "проект")] == [one.id]
    assert [n.id for n in notes_store.search(11, "100%")] == [two.id]
    assert [n.id for n in notes_store.search(11, "пример_")] == [two.id]
    assert notes_store.search(11, "' OR 1=1 --") == []


def test_recent_notes_sorted_and_paginated(notes_store) -> None:
    ids = [notes_store.create_text(1, f"заметка номер {i}").id for i in range(23)]
    assert [n.id for n in notes_store.recent(1)] == list(reversed(ids[-10:]))
    assert [n.id for n in notes_store.recent(1, page=2)] == list(
        reversed(ids[3:13])
    )
    assert len(notes_store.recent(1, page=3)) == 3
    with pytest.raises(ValueError):
        notes_store.recent(1, page=51)


def test_empty_oversized_and_invalid_queries_fail_without_insert(notes_store) -> None:
    for value in ("", "  ", "x" * (MAX_NOTE_CHARS + 1)):
        with pytest.raises(ValueError):
            notes_store.create_text(1, value)
    assert notes_store.recent(1) == []
    for query in ("", "а", "т" * 101):
        with pytest.raises(ValueError):
            notes_store.search(1, query)


def test_voice_capture_one_shot_keeps_original_transcript(notes_store) -> None:
    notes_store.arm_voice(17, now=100)
    assert notes_store.pending_voice(17, now=100)
    token = notes_store.claim_voice(17, now=101)
    assert token
    assert not notes_store.pending_voice(17, now=101)
    assert notes_store.claim_voice(17, now=102) is None
    saved = notes_store.capture_voice(
        17, "Сегодня хорошая погода.",
        original_text="сегодня хорошая погода", capture_token=token,
    )
    assert saved is not None
    assert saved.source == "voice"
    assert saved.original_text == "сегодня хорошая погода"
    assert saved.body == "Сегодня хорошая погода."
    assert not notes_store.pending_voice(17, now=102)
    assert notes_store.capture_voice(
        17, "ещё голос", original_text="ещё голос", capture_token=token,
    ) is None


def test_voice_capture_expires_and_cancel_prevents_insert(notes_store) -> None:
    notes_store.arm_voice(8, now=100)
    assert notes_store.pending_voice(8, now=100 + CAPTURE_TTL_SECONDS - 1)
    assert not notes_store.pending_voice(8, now=100 + CAPTURE_TTL_SECONDS)
    assert notes_store.claim_voice(8, now=100 + CAPTURE_TTL_SECONDS) is None
    notes_store.arm_voice(8, now=2000)
    token = notes_store.claim_voice(8, now=2001)
    assert token
    assert notes_store.cancel_voice(8) is True
    assert notes_store.cancel_voice(8) is False
    assert notes_store.capture_voice(
        8, "не сохранить", original_text="не сохранить", capture_token=token,
    ) is None


def test_failed_voice_capture_does_not_consume_arm(notes_store) -> None:
    notes_store.arm_voice(1, now=10)
    token = notes_store.claim_voice(1, now=11)
    assert token
    with pytest.raises(ValueError):
        notes_store.capture_voice(
            1, "x" * (MAX_NOTE_CHARS + 1), original_text="исходный текст",
            capture_token=token,
        )
    assert not notes_store.pending_voice(1, now=12)
    assert notes_store.recent(1) == []


def test_voice_capture_is_user_isolated(notes_store) -> None:
    notes_store.arm_voice(1, now=20)
    token = notes_store.claim_voice(1, now=21)
    assert token
    assert notes_store.capture_voice(
        2, "чужое", original_text="чужое", capture_token=token,
    ) is None
    assert notes_store.capture_voice(
        1, "своё", original_text="своё", capture_token=token,
    ) is not None


def test_cancel_then_rearm_does_not_save_stale_queued_voice(notes_store) -> None:
    notes_store.arm_voice(1, now=100)
    old_token = notes_store.claim_voice(1, now=101)
    assert old_token
    assert notes_store.cancel_voice(1)
    notes_store.arm_voice(1, now=102)
    new_token = notes_store.claim_voice(1, now=103)
    assert new_token and new_token != old_token
    assert notes_store.capture_voice(
        1, "старое аудио", original_text="старое аудио",
        capture_token=old_token,
    ) is None
    assert notes_store.capture_voice(
        1, "новое аудио", original_text="новое аудио",
        capture_token=new_token,
    ) is not None
    assert [n.body for n in notes_store.recent(1)] == ["новое аудио"]


def test_rearm_without_cancel_invalidates_earlier_claim(notes_store) -> None:
    notes_store.arm_voice(1, now=200)
    old_token = notes_store.claim_voice(1, now=201)
    assert old_token
    notes_store.arm_voice(1, now=202)
    new_token = notes_store.claim_voice(1, now=203)
    assert new_token and new_token != old_token
    assert notes_store.capture_voice(
        1, "не сохранять", original_text="не сохранять",
        capture_token=old_token,
    ) is None
    assert notes_store.capture_voice(
        1, "сохранить", original_text="сохранить",
        capture_token=new_token,
    ) is not None


def test_commands_and_note_service_output(notes_store) -> None:
    assert _argument("/note    Важная мысль о проекте") == "Важная мысль о проекте"
    assert _argument("/note") == ""
    notes = NotesService(notes_store)
    saved = notes.create_text(7, "Купить книгу о Python")
    assert f"#{saved.id}" in notes.saved_message(saved)
    assert "Купить книгу" in notes.list_message(7)
    assert "Купить книгу" in notes.search_message(7, "python")
    assert "Купить книгу" in notes.show_message(saved)
    assert "не найдены" in notes.search_message(8, "python")


def test_notes_router_has_explicit_command_handlers_only(notes_store) -> None:
    notes = NotesService(notes_store)
    router = create_notes_router(service=notes, allowed_user_ids=frozenset({7}))
    handler_names = {handler.callback.__name__ for handler in router.message.handlers}
    assert handler_names == {
        "on_note", "on_cancel", "on_list", "on_search", "on_show",
    }
    assert all(handler.filters for handler in router.message.handlers)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_chat_action(self, *args) -> None:
        return None

    async def send_message(self, _chat_id, text: str, **kwargs) -> None:
        self.sent.append(text)


class FakeQueue:
    def __init__(self, job: AudioJob) -> None:
        self.job = job
        self.calls = 0

    async def get(self):
        self.calls += 1
        if self.calls > 1:
            raise asyncio.CancelledError
        return self.job

    async def release(self, _user_id):
        pass

    def task_done(self):
        pass


class FakeTranscriber:
    async def transcribe(self, _path):
        return TranscriptionResult(
            text="сегодня хорошая погода",
            pause_aware_text="сегодня, хорошая погода",
            language="ru",
            language_probability=0,
            duration_seconds=2.0,
            duration_after_vad_seconds=2.0,
            processing_seconds=0.1,
        )


class FakePunctuator:
    async def format(self, text, *, fallback_text):
        assert text == "сегодня хорошая погода"
        return "Сегодня хорошая погода."


async def _one_voice(
    tmp_path: Path,
    monkeypatch,
    *,
    notes: NotesService,
    capture_note: bool,
    planner=None,
) -> list[str]:
    from voqelis import bot as bot_module

    path = tmp_path / "voice.ogg"
    path.write_bytes(b"test")
    monkeypatch.setattr(bot_module, "probe_duration_seconds", lambda _path: 2.0)
    bot = FakeBot()
    queue = FakeQueue(AudioJob(
        chat_id=10, reply_to_message_id=11, user_id=7, file_path=path,
        capture_token=notes.claim_voice(7) if capture_note else None,
    ))
    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            bot=bot,
            queue=queue,
            transcriber=FakeTranscriber(),
            settings=SimpleNamespace(max_audio_seconds=1200),
            notes=notes,
            planner=planner,
            punctuation_processor=FakePunctuator(),
        )
    return bot.sent


@pytest.mark.asyncio
async def test_explicit_voice_note_saved_and_punctuated(tmp_path, monkeypatch, notes_store):
    notes = NotesService(notes_store)
    notes.arm_voice(7)
    replies = await _one_voice(
        tmp_path, monkeypatch, notes=notes, capture_note=True
    )
    found = notes_store.recent(7)
    assert len(found) == 1
    assert found[0].body == "Сегодня хорошая погода."
    assert found[0].original_text == "сегодня хорошая погода"
    assert "Заметка #1 сохранена" in replies[0]
    assert not notes.pending_voice(7)


@pytest.mark.asyncio
async def test_ordinary_voice_not_saved_even_if_note_armed_later(
    tmp_path, monkeypatch, notes_store
):
    notes = NotesService(notes_store)
    notes.arm_voice(7)
    replies = await _one_voice(
        tmp_path, monkeypatch, notes=notes, capture_note=False
    )
    assert notes_store.recent(7) == []
    assert notes.pending_voice(7)
    assert replies == ["Сегодня хорошая погода."]


@pytest.mark.asyncio
async def test_voice_with_planner_session_never_creates_note(
    tmp_path, monkeypatch, notes_store
):
    notes = NotesService(notes_store)
    notes.arm_voice(7)

    class FakePlanner:
        config = SimpleNamespace(timezone="Europe/Moscow")

        class Store:
            def session(self, user_id):
                assert user_id == 7
                return {"state": "planning", "target_day": "2026-10-25"}

        store = Store()

        async def handle_text(self, user_id, text, today):
            raise AssertionError(
                "A voice captured as a Note must not enter Planner after mode switch"
            )

    monkeypatch.setattr(
        "voqelis.bot.planner_markup_for_state", lambda *_args: None
    )
    replies = await _one_voice(
        tmp_path, monkeypatch, notes=notes, capture_note=True,
        planner=FakePlanner(),
    )
    assert len(replies) == 1
    assert "не добавлено ни в Planner" in replies[0]
    assert notes_store.recent(7) == []



class FakeMessage:
    def __init__(self, text: str, *, user_id: int = 7) -> None:
        from aiogram.enums import ChatType

        self.text = text
        self.chat = SimpleNamespace(type=ChatType.PRIVATE)
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(text)


def _command_callback(router, name: str):
    return next(
        handler.callback
        for handler in router.message.handlers
        if handler.callback.__name__ == name
    )


@pytest.mark.asyncio
async def test_notes_telegram_commands_create_search_show_and_cancel(notes_store) -> None:
    service = NotesService(notes_store)
    router = create_notes_router(
        service=service, allowed_user_ids=frozenset({7})
    )
    create = _command_callback(router, "on_note")
    list_notes = _command_callback(router, "on_list")
    search = _command_callback(router, "on_search")
    show = _command_callback(router, "on_show")
    cancel = _command_callback(router, "on_cancel")

    text_command = FakeMessage("/note Новая полезная мысль")
    await create(text_command)
    assert "Заметка #1 сохранена" in text_command.answers[0]
    assert "Новая полезная мысль" in text_command.answers[0]

    list_command = FakeMessage("/notes")
    await list_notes(list_command)
    assert "Новая полезная мысль" in list_command.answers[0]

    search_command = FakeMessage("/note_search ПОЛЕЗНАЯ")
    await search(search_command)
    assert "Новая полезная мысль" in search_command.answers[0]

    show_command = FakeMessage("/note_show 1")
    await show(show_command)
    assert "Новая полезная мысль" in show_command.answers[0]

    arm_command = FakeMessage("/note")
    await create(arm_command)
    assert service.pending_voice(7)
    assert "Отправь одно голосовое" in arm_command.answers[0]

    cancel_command = FakeMessage("/note_cancel")
    await cancel(cancel_command)
    assert not service.pending_voice(7)

    invalid_show = FakeMessage("/note_show нечисло")
    await show(invalid_show)
    assert "Используй /note_show" in invalid_show.answers[0]


@pytest.mark.asyncio
async def test_notes_commands_reject_unauthorized_user(notes_store) -> None:
    service = NotesService(notes_store)
    router = create_notes_router(
        service=service, allowed_user_ids=frozenset({7})
    )
    create = _command_callback(router, "on_note")
    show = _command_callback(router, "on_show")
    unauthorized = FakeMessage("/note Не чужое дело", user_id=19)
    await create(unauthorized)
    assert unauthorized.answers == []
    assert notes_store.recent(19) == []
    assert not notes_store.pending_voice(19)
    invalid_show = FakeMessage("/note_show 1", user_id=19)
    await show(invalid_show)
    assert invalid_show.answers == []


@pytest.mark.asyncio
async def test_text_note_rejected_during_active_planner_session(notes_store) -> None:
    service = NotesService(notes_store)

    class PlannerStub:
        store = SimpleNamespace(session=lambda _user_id: {"state": "planning"})

    router = create_notes_router(
        service=service,
        allowed_user_ids=frozenset({7}),
        planner=PlannerStub(),
    )
    create = _command_callback(router, "on_note")
    text_note = FakeMessage("/note заметка во время Planner")
    await create(text_note)
    assert "открыт Planner" in text_note.answers[0]
    assert notes_store.recent(7) == []
    voice_note = FakeMessage("/note")
    await create(voice_note)
    assert "открыт Planner" in voice_note.answers[0]
    assert not notes_store.pending_voice(7)


@pytest.mark.asyncio
async def test_text_note_cancels_previous_pending_voice(notes_store) -> None:
    service = NotesService(notes_store)
    router = create_notes_router(service=service, allowed_user_ids=frozenset({7}))
    create = _command_callback(router, "on_note")
    await create(FakeMessage("/note"))
    assert service.pending_voice(7)
    await create(FakeMessage("/note Только текст"))
    assert not service.pending_voice(7)
    assert [n.body for n in notes_store.recent(7)] == ["Только текст"]
