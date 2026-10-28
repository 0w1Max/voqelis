"""Navigation: modular keyboards, durable per-user state and fail-closed routing."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis import bot as bot_module
from voqelis.day_facts.bot import create_day_facts_router
from voqelis.day_facts.service import DayFactsService
from voqelis.day_facts.store import DayFactsStore
from voqelis.domain import AudioJob, TranscriptionResult
from voqelis.menu import (
    ADMIN_PANEL,
    FACTS_NEW,
    FACTS_RESUME,
    FACTS_START,
    HOME,
    NOTES_START,
    TODO_START,
    main_keyboard,
)
from voqelis.navigation import NavigationStore, create_navigation_router
from voqelis.queue import JobQueue


@pytest.fixture
def nav(tmp_path):
    navigation = NavigationStore(tmp_path / "navigation.sqlite3")
    yield navigation
    navigation.close()


def _labels(keyboard):
    return [
        button.text for row in keyboard.keyboard for button in row
    ]


def test_private_navigation_is_per_user_versioned_and_persistent(nav):
    assert nav.current(1) == ("home", 0)
    assert nav.current(2) == ("home", 0)
    assert nav.activate(1, "facts") == ("facts", 1)
    assert nav.activate(1, "facts") == ("facts", 1)
    assert nav.current(2) == ("home", 0)
    assert nav.activate(1, "home") == ("home", 2)
    assert nav.activate(2, "notes") == ("notes", 1)
    assert stat.S_IMODE(nav.path.stat().st_mode) == 0o600
    assert nav.current(1) == ("home", 2)


def test_navigation_survives_restart_and_denies_invalid_values(tmp_path):
    path = tmp_path / "navigation.sqlite3"
    nav = NavigationStore(path)
    nav.activate(10, "planner")
    nav.close()
    reopened = NavigationStore(path)
    try:
        assert reopened.current(10) == ("planner", 1)
        with pytest.raises(ValueError):
            reopened.activate(10, "admin")
        with pytest.raises(ValueError):
            reopened.activate(-1, "facts")
    finally:
        reopened.close()


def test_navigation_rejects_symlink(tmp_path):
    target = tmp_path / "target.db"
    target.write_bytes(b"not a database")
    (tmp_path / "navigation.sqlite3").symlink_to(target)
    with pytest.raises(OSError):
        NavigationStore(tmp_path / "navigation.sqlite3")


class FakeMessage:
    def __init__(self, user_id=1, text=""):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type="private", id=user_id)
        self.text = text
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))


def _handler(router, name):
    return next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == name
    )


@pytest.mark.asyncio
async def test_navigation_only_opens_modules_without_creating_drafts(nav, tmp_path):
    store = DayFactsStore(tmp_path / "facts.sqlite3")
    try:
        router = create_navigation_router(
            navigation=nav, allowed_user_ids=frozenset({1}),
            admin_user_id=1,
        )
        facts = FakeMessage(1, FACTS_START)
        await _handler(router, "on_facts")(facts)
        assert nav.current(1) == ("facts", 1)
        assert store.draft(1) is None
        assert FACTS_NEW in _labels(facts.responses[-1][1]["reply_markup"])
        assert FACTS_RESUME in _labels(facts.responses[-1][1]["reply_markup"])

        home = FakeMessage(1, HOME)
        await _handler(router, "on_home")(home)
        assert nav.current(1) == ("home", 2)
        assert ADMIN_PANEL in _labels(home.responses[-1][1]["reply_markup"])
        assert FACTS_NEW not in _labels(home.responses[-1][1]["reply_markup"])

        guest = FakeMessage(3, FACTS_START)
        await _handler(router, "on_facts")(guest)
        assert not guest.responses
        assert nav.current(3) == ("home", 0)

        placeholder = FakeMessage(1, TODO_START)
        await _handler(router, "on_todo")(placeholder)
        assert "не принимает" in placeholder.responses[-1][0]
        assert nav.current(1)[0] == "home"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_draft_is_parked_not_modified_when_leaving_facts(nav, tmp_path):
    store = DayFactsStore(tmp_path / "facts.sqlite3")
    try:
        service = DayFactsService(store)
        planner = SimpleNamespace(store=SimpleNamespace(session=lambda _user: None))
        notes = SimpleNamespace(pending_voice=lambda _user: False)
        router = create_day_facts_router(
            service=service, allowed_user_ids=frozenset({1}),
            planner=planner, notes=notes, export_dir=tmp_path,
            navigation=nav,
        )
        nav.activate(1, "facts")
        await _handler(router, "on_start")(FakeMessage(1, FACTS_NEW))
        draft = store.draft(1)
        assert draft is not None

        nav.activate(1, "notes")
        stale = FakeMessage(1, "Нельзя добавлять в факты")
        await _handler(router, "on_field_text")(stale)
        assert store.draft(1) == draft
        assert not stale.responses

        nav.activate(1, "facts")
        resumed = FakeMessage(1, FACTS_RESUME)
        await _handler(router, "on_start")(resumed)
        assert store.draft(1) == draft
        assert "Расскажи" in resumed.responses[-1][0]
    finally:
        store.close()


class FakeAudioMessage:
    def __init__(self):
        self.from_user = SimpleNamespace(id=1)
        self.chat = SimpleNamespace(type="private", id=1)
        self.voice = SimpleNamespace(file_id="voice123", duration=2, file_size=4)
        self.audio = None
        self.document = None
        self.message_id = 3
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class FakeAudioTelegramBot:
    def __init__(self):
        self.messages = []
        self.sent = asyncio.Event()

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="record.ogg", file_size=4)

    async def download_file(self, remote_path, *, destination, timeout):
        Path(destination).write_bytes(b"ABCD")

    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        self.sent.set()


@pytest.mark.asyncio
async def test_home_rejects_audio_before_downloading(nav, tmp_path):
    settings = SimpleNamespace(
        allowed_user_ids=frozenset({1}),
        max_file_size_bytes=20_000,
        max_audio_seconds=1200,
        temp_dir=tmp_path,
        download_timeout_seconds=10,
    )
    queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
    router = bot_module.create_router(
        settings=settings, queue=queue, navigation=nav
    )
    message = FakeAudioMessage()
    await _handler(router, "on_audio")(message, FakeAudioTelegramBot())
    assert "Сначала выбери модуль" in message.replies[-1]
    assert queue.snapshot().reserved == 0


@pytest.mark.asyncio
async def test_switch_after_queued_audio_does_not_write_to_other_module(nav, tmp_path):
    nav.activate(1, "facts")
    admitted_section, admitted_revision = nav.current(1)
    audio_file = tmp_path / "audio.ogg"
    audio_file.write_bytes(b"record")
    queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
    assert await queue.reserve(1)
    await queue.put(AudioJob(
        chat_id=1, reply_to_message_id=5, user_id=1, file_path=audio_file,
        navigation_section=admitted_section,
        navigation_revision=admitted_revision,
    ))
    nav.activate(1, "notes")
    bot = FakeAudioTelegramBot()
    calls = []
    transcriber = SimpleNamespace(
        transcribe=lambda _path: calls.append(_path)
    )
    worker = asyncio.create_task(bot_module.run_worker(
        bot=bot, queue=queue, transcriber=transcriber,
        settings=SimpleNamespace(max_audio_seconds=1200, allowed_user_ids=frozenset({1})),
        navigation=nav,
    ))
    try:
        await asyncio.wait_for(bot.sent.wait(), timeout=2)
        assert "Режим изменился" in bot.messages[0]
        assert calls == []
        assert not audio_file.exists()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_existing_transcription_contract_still_available_in_transcribe_module(nav, tmp_path):
    nav.activate(1, "transcribe")
    assert nav.current(1) == ("transcribe", 1)
    result = TranscriptionResult(
        text="исходный текст",
        language="ru",
        language_probability=1.0,
        duration_seconds=1.0,
        duration_after_vad_seconds=1.0,
        processing_seconds=0.1,
    )
    assert result.text == "исходный текст"
    assert NOTES_START in _labels(main_keyboard())
