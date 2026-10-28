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
    def __init__(self, user_id=1):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type="private", id=user_id)
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
@pytest.mark.parametrize("user_id", [1, 2])
@pytest.mark.parametrize("kind", ["voice", "audio", "document"])
async def test_home_accepts_audio_for_owner_and_guest(nav, tmp_path, user_id, kind):
    settings = SimpleNamespace(
        allowed_user_ids=frozenset({1, 2}),
        max_file_size_bytes=20_000,
        max_audio_seconds=1200,
        temp_dir=tmp_path,
        download_timeout_seconds=10,
    )
    queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
    router = bot_module.create_router(
        settings=settings, queue=queue, navigation=nav, owner_user_id=1,
    )
    audio = FakeAudioMessage(user_id=user_id)
    if kind == "audio":
        audio.audio, audio.voice = audio.voice, None
    elif kind == "document":
        audio.voice = None
        audio.document = SimpleNamespace(
            file_id="audio123", file_size=4,
            mime_type="audio/ogg", file_name="record.ogg",
        )
    await _handler(router, "on_audio")(audio, FakeAudioTelegramBot())
    job = await queue.get()
    try:
        assert "Принял" in audio.replies[-1]
        assert job.user_id == user_id
        assert job.navigation_section == "home"
        assert job.navigation_revision == 0
        assert job.day_facts_token is None
        assert job.capture_token is None
    finally:
        job.file_path.unlink(missing_ok=True)
        await queue.release(user_id)
        queue.task_done()


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


def test_legacy_transcription_state_migrates_to_home_without_resetting_others(tmp_path):
    path = tmp_path / "navigation.sqlite3"
    original = NavigationStore(path)
    try:
        original.activate(1, "facts_capture")
        original.activate(2, "planner")
        with original.db:
            original.db.execute(
                "UPDATE navigation_state SET section='transcribe' WHERE user_id=2"
            )
        assert original.current(2) == ("transcribe", 1)
    finally:
        original.close()

    migrated = NavigationStore(path)
    try:
        assert migrated.current(1) == ("facts_capture", 1)
        assert migrated.current(2) == ("home", 2)
        assert migrated.activate(2, "home") == ("home", 2)
        with pytest.raises(ValueError):
            migrated.activate(2, "transcribe")
        assert NOTES_START in _labels(main_keyboard(admin=True))
        assert NOTES_START not in _labels(main_keyboard())
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_facts_menu_does_not_arm_existing_parked_draft(nav, tmp_path):
    store = DayFactsStore(tmp_path / "facts.sqlite3")
    try:
        from datetime import date

        draft = store.begin(1, date(2026, 10, 28))
        nav.activate(1, "facts")
        settings = SimpleNamespace(
            allowed_user_ids=frozenset({1}), temp_dir=tmp_path,
            max_file_size_bytes=20_000, max_audio_seconds=1200,
            download_timeout_seconds=10,
        )
        queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
        router = bot_module.create_router(
            settings=settings, queue=queue, navigation=nav,
            day_facts=DayFactsService(store),
        )
        audio = FakeAudioMessage()
        await _handler(router, "on_audio")(audio, FakeAudioTelegramBot())
        assert "Продолжить черновик" in audio.replies[-1]
        assert queue.snapshot().reserved == 0
        assert store.draft(1) == draft
    finally:
        store.close()


@pytest.mark.asyncio
async def test_facts_dictation_uses_only_explicit_capture_mode(nav, tmp_path):
    from datetime import date

    store = DayFactsStore(tmp_path / "facts.sqlite3")
    try:
        draft = store.begin(1, date(2026, 10, 28))
        nav.activate(1, "facts_capture")
        settings = SimpleNamespace(
            allowed_user_ids=frozenset({1}), temp_dir=tmp_path,
            max_file_size_bytes=20_000, max_audio_seconds=1200,
            download_timeout_seconds=10,
        )
        queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
        router = bot_module.create_router(
            settings=settings, queue=queue, navigation=nav,
            day_facts=DayFactsService(store),
        )
        audio = FakeAudioMessage()
        await _handler(router, "on_audio")(audio, FakeAudioTelegramBot())
        job = await queue.get()
        try:
            assert job.day_facts_token == draft.token
            assert job.capture_token is None
            assert job.navigation_section == "facts_capture"
            assert job.navigation_revision == nav.current(1)[1]
        finally:
            job.file_path.unlink(missing_ok=True)
            await queue.release(1)
            queue.task_done()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_note_recording_does_not_intercept_parked_facts_draft(nav, tmp_path):
    from datetime import date

    store = DayFactsStore(tmp_path / "facts.sqlite3")
    try:
        draft = store.begin(1, date(2026, 10, 28))
        nav.activate(1, "notes")
        notes = SimpleNamespace(
            pending_voice=lambda _user_id: True,
            claim_voice=lambda _user_id: "note-token",
        )
        settings = SimpleNamespace(
            allowed_user_ids=frozenset({1}), temp_dir=tmp_path,
            max_file_size_bytes=20_000, max_audio_seconds=1200,
            download_timeout_seconds=10,
        )
        queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
        router = bot_module.create_router(
            settings=settings, queue=queue, navigation=nav,
            notes=notes, day_facts=DayFactsService(store),
            owner_user_id=1,
        )
        audio = FakeAudioMessage()
        await _handler(router, "on_audio")(audio, FakeAudioTelegramBot())
        job = await queue.get()
        try:
            assert job.capture_token == "note-token"
            assert job.day_facts_token is None
            assert store.draft(1) == draft
        finally:
            job.file_path.unlink(missing_ok=True)
            await queue.release(1)
            queue.task_done()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_admin_panel_parks_active_dictation(nav, tmp_path):
    from voqelis.access import AccessStore, create_access_router

    access = AccessStore(
        tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1})
    )
    try:
        nav.activate(1, "facts_capture")
        router = create_access_router(access, navigation=nav)
        owner = FakeMessage(1, "/admin")
        await _handler(router, "on_admin")(owner)
        assert nav.current(1)[0] == "home"
        assert "Админ-панель" in owner.responses[-1][0]

        nav.activate(2, "facts_capture")
        guest = FakeMessage(2, "/admin")
        await _handler(router, "on_admin")(guest)
        assert nav.current(2)[0] == "facts_capture"
        assert guest.responses == []
    finally:
        access.close()


@pytest.mark.asyncio
async def test_navigation_change_during_punctuation_does_not_write_facts(
    nav, tmp_path, monkeypatch
):
    from datetime import date

    store = DayFactsStore(tmp_path / "facts.sqlite3")
    try:
        draft = store.begin(1, date(2026, 10, 28))
        draft = store.accept(1, draft.token, "Встреча с другом")
        service = DayFactsService(store)
        nav.activate(1, "facts_capture")
        section, revision = nav.current(1)

        queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
        assert await queue.reserve(1)
        path = tmp_path / "voice.ogg"
        path.write_bytes(b"hello")
        await queue.put(AudioJob(
            chat_id=1, reply_to_message_id=6, user_id=1, file_path=path,
            day_facts_token=draft.token,
            navigation_section=section, navigation_revision=revision,
        ))
        monkeypatch.setattr(bot_module, "probe_duration_seconds", lambda _p: 1.0)

        class Transcriber:
            async def transcribe(self, _path):
                return TranscriptionResult(
                    text="Новый факт", language="ru", language_probability=1.0,
                    duration_seconds=1.0, duration_after_vad_seconds=1.0,
                    processing_seconds=0.2,
                )

        class Punctuation:
            async def format(self, text, *, fallback_text):
                nav.activate(1, "home")
                return text

        bot = FakeAudioTelegramBot()
        worker = asyncio.create_task(bot_module.run_worker(
            bot=bot, queue=queue, transcriber=Transcriber(),
            settings=SimpleNamespace(
                max_audio_seconds=1200, allowed_user_ids=frozenset({1})
            ),
            navigation=nav, day_facts=service,
            punctuation_processor=Punctuation(),
        ))
        try:
            await asyncio.wait_for(bot.sent.wait(), timeout=2)
            assert "не обновлены" in bot.messages[-1]
            assert store.draft(1) == draft
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
    finally:
        store.close()



@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [1, 2])
async def test_home_transcription_keeps_parked_facts_and_note_unchanged(
    nav, tmp_path, monkeypatch, user_id
):
    from datetime import date

    from voqelis.notes.service import NotesService
    from voqelis.notes.store import NotesStore

    fact_store = DayFactsStore(tmp_path / "facts.sqlite3")
    note_store = NotesStore(tmp_path / "notes.sqlite3")
    fact_service = DayFactsService(fact_store)
    note_service = NotesService(note_store)
    try:
        parked = fact_store.begin(user_id, date(2026, 10, 28))
        note_store.arm_voice(user_id)
        nav.activate(user_id, "home")
        calls = []

        def parked_edit_guard(uid):
            calls.append(uid)
            return SimpleNamespace(edit_field="select")

        # Any parked edit must NOT be consulted while accepting home audio.
        monkeypatch.setattr(fact_store, "draft", parked_edit_guard)
        settings = SimpleNamespace(
            allowed_user_ids=frozenset({1, 2}),
            temp_dir=tmp_path, max_file_size_bytes=20_000,
            max_audio_seconds=1200, download_timeout_seconds=10,
        )
        queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
        intake = bot_module.create_router(
            settings=settings, queue=queue, navigation=nav,
            day_facts=fact_service, notes=note_service,
            owner_user_id=1,
        )
        tg_bot = FakeAudioTelegramBot()
        await _handler(intake, "on_audio")(
            FakeAudioMessage(user_id=user_id), tg_bot
        )
        assert not calls

        class FakeTranscriber:
            async def transcribe(self, path):
                return TranscriptionResult(
                    text="сегодня тестовое сообщение", language="ru",
                    language_probability=1.0, duration_seconds=2.0,
                    duration_after_vad_seconds=2.0, processing_seconds=0.1,
                )

        class FakePunctuator:
            async def format(self, text, *, fallback_text):
                assert text == "сегодня тестовое сообщение"
                return "Сегодня тестовое сообщение."

        monkeypatch.setattr(bot_module, "probe_duration_seconds", lambda _: 2.0)
        worker = asyncio.create_task(bot_module.run_worker(
            bot=tg_bot, queue=queue, transcriber=FakeTranscriber(),
            settings=settings, navigation=nav, owner_user_id=1,
            day_facts=fact_service, notes=note_service,
            punctuation_processor=FakePunctuator(),
        ))
        try:
            await asyncio.wait_for(tg_bot.sent.wait(), timeout=3)
            assert tg_bot.messages == ["Сегодня тестовое сообщение."]
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
        assert not calls
        # Inspect the actual store using its original method after unpatch.
        monkeypatch.undo()
        assert fact_store.draft(user_id) == parked
        assert note_store.pending_voice(user_id)
        assert note_store.recent(user_id) == []
    finally:
        fact_store.close()
        note_store.close()
