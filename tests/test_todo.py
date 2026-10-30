"""ToDo V1: independent checklist, immutable audio intent and SQLite isolation."""

from __future__ import annotations

import asyncio
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis import bot as audio_module
from voqelis.domain import AudioJob, TranscriptionResult
from voqelis.menu import TODO_ACTIVE, TODO_NEW, TODO_START
from voqelis.navigation import NavigationStore, create_navigation_router
from voqelis.queue import JobQueue
from voqelis.todo.bot import create_todo_router, item_markup
from voqelis.todo.service import TodoService
from voqelis.todo.store import TodoStore


@pytest.fixture
def store(tmp_path):
    db = TodoStore(tmp_path / "todo.sqlite3")
    yield db
    db.close()


def handler(router, name):
    return next(h.callback for h in router.message.handlers
                if h.callback.__name__ == name)


class MessageStub:
    def __init__(self, user_id=1, text=""):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.message_id = 7
        self.text = text
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))


class Click:
    def __init__(self, user_id, data, message):
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.data = data
        self.acks = []

    async def answer(self, text=None, **kwargs):
        self.acks.append((text, kwargs))


def test_no_dates_data_scoped_and_persistent(store, tmp_path):
    first = store.begin_capture(1, mode="add")
    item = store.capture_text(1, first.token, "Купить продукты")
    assert item.text == "Купить продукты"
    assert store.capture_text(1, first.token, "Дублирование") is None
    assert store.get(2, item.id) is None
    assert store.list_items(2, completed=False)[0] == []
    assert store.list_items(1, completed=False)[0] == [item]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert "deadline" not in str(store.db.execute(
        "SELECT sql FROM sqlite_master WHERE name='todo_items'"
    ).fetchone()[0]).lower()
    store.close()
    again = TodoStore(tmp_path / "todo.sqlite3")
    try:
        assert again.get(1, item.id) == item
    finally:
        again.close()


def test_capture_text_lifecycle_validation_and_expiry(store):
    token = store.begin_capture(1, now=100).token
    assert store.pending(1, now=100).token == token
    assert store.pending(1, now=1000) is None
    assert store.capture_text(1, "stale", "Дело") is None
    with pytest.raises(ValueError):
        store.capture_text(1, token, "  ")
    with pytest.raises(ValueError):
        store.capture_text(1, token, "x" * 1001)
    item = store.capture_text(1, token, "Подготовить проект")
    # Time was mocked into the past. This must not commit a stale token.
    assert item is None
    assert store.list_items(1, completed=False)[0] == []


def test_voice_capture_is_one_shot_and_stale_cannot_duplicate(store):
    token = store.begin_capture(1).token
    assert store.claim_voice(1) == token
    assert store.claim_voice(1) is None
    assert store.capture_text(1, token, "Текст после голосового") is None
    item = store.capture_voice(1, token, "Записать задачу")
    assert item.source == "voice"
    assert store.capture_voice(1, token, "Ещё раз") is None
    assert len(store.list_items(1, completed=False)[0]) == 1
    abandoned = store.begin_capture(1).token
    store.cancel_capture(1)
    assert store.claim_voice(1) is None
    assert store.capture_voice(1, abandoned, "Старое голосовое") is None
    assert len(store.list_items(1, completed=False)[0]) == 1


def test_guest_separation_complete_reopen_edit_and_confirmed_delete(store):
    a = store.begin_capture(1).token
    item = store.capture_text(1, a, "Заполнить чек-лист")
    assert store.set_completed(2, item.id, completed=True) is None
    assert store.ask_delete(2, item.id) is None
    with pytest.raises(ValueError):
        store.begin_capture(2, mode="edit", item_id=item.id)

    done = store.set_completed(1, item.id, completed=True)
    assert done.completed_at
    assert store.list_items(1, completed=True)[0] == [done]
    assert store.list_items(1, completed=False)[0] == []
    edited = store.begin_capture(1, mode="edit", item_id=item.id)
    updated = store.capture_text(1, edited.token, "Исправленная задача")
    assert updated.text == "Исправленная задача"
    assert updated.completed_at == done.completed_at
    restored = store.set_completed(1, item.id, completed=False)
    assert restored.completed_at is None

    deletion = store.ask_delete(1, item.id, now=100)
    assert not store.confirm_delete(1, item.id, "wrong", now=100)
    assert not store.confirm_delete(2, item.id, deletion, now=100)
    assert not store.confirm_delete(1, item.id, deletion, now=221)
    assert store.get(1, item.id) is not None
    again = store.ask_delete(1, item.id)
    assert store.confirm_delete(1, item.id, again)
    assert not store.confirm_delete(1, item.id, again)
    assert store.get(1, item.id) is None
    assert store.get(2, item.id) is None


def test_capture_cancel_and_rearm_does_not_apply_old_jobs(store):
    old = store.begin_capture(1).token
    new = store.begin_capture(1).token
    assert old != new
    assert store.capture_text(1, old, "Старое") is None
    assert store.claim_voice(1) == new
    store.cancel_capture(1)
    assert store.capture_voice(1, new, "Устаревшее") is None
    assert store.list_items(1, completed=False)[0] == []


def test_list_pagination_and_item_buttons_are_bounded(store):
    for i in range(11):
        token = store.begin_capture(1).token
        store.capture_text(1, token, f"Дело #{i}")
    first, total = store.list_items(1, completed=False)
    second, _ = store.list_items(1, completed=False, page=2)
    assert total == 11
    assert len(first) == 10 and len(second) == 1
    assert {x.id for x in first}.isdisjoint(x.id for x in second)
    with pytest.raises(ValueError):
        store.list_items(1, completed=False, page=0)
    with pytest.raises(ValueError):
        store.list_items(1, completed=False, page=51)
    markup = item_markup(first[0])
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callbacks == [
        f"td:done:{first[0].id}",
        f"td:rem:list:{first[0].id}",
        f"td:edit:{first[0].id}",
        f"td:ask:{first[0].id}",
    ]
    assert all(len(c) <= 64 for c in callbacks)


def test_todo_file_rejects_symlinks(tmp_path):
    link = tmp_path / "todo.sqlite3"
    link.symlink_to(tmp_path / "target.sqlite3")
    with pytest.raises(OSError):
        TodoStore(link)


def test_legacy_navigation_migrates_existing_rows_and_revision(tmp_path):
    dbpath = tmp_path / "old-nav.sqlite3"
    with sqlite3.connect(dbpath) as db:
        db.executescript(
            "CREATE TABLE navigation_state("
            "user_id INTEGER PRIMARY KEY, "
            "section TEXT NOT NULL CHECK(section IN "
            "('home','facts','facts_capture','notes','planner','transcribe')), "
            "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0));"
        )
        db.executemany(
            "INSERT INTO navigation_state(user_id,section,revision) "
            "VALUES(?,?,?)",
            [(1, "facts_capture", 7), (2, "planner", 11), (3, "home", 2)],
        )
    nav = NavigationStore(dbpath)
    try:
        assert nav.current(1) == ("facts_capture", 7)
        assert nav.current(2) == ("planner", 11)
        assert nav.current(3) == ("home", 2)
        assert nav.activate(4, "todo") == ("todo", 1)
        assert nav.activate(4, "todo_capture") == ("todo_capture", 2)
    finally:
        nav.close()
    nav = NavigationStore(dbpath)
    try:
        assert nav.current(1) == ("facts_capture", 7)
        assert nav.current(4) == ("todo_capture", 2)
    finally:
        nav.close()


@pytest.mark.asyncio
async def test_keyboard_menu_and_old_button_are_user_scoped(store, tmp_path):
    nav = NavigationStore(tmp_path / "nav.sqlite3")
    try:
        navigation_router = create_navigation_router(
            navigation=nav, allowed_user_ids=frozenset({1, 2}), admin_user_id=1,
        )
        for user in (1, 2):
            opened = MessageStub(user, TODO_START)
            await handler(navigation_router, "on_todo")(opened)
            assert nav.current(user)[0] == "todo"
            assert TODO_NEW in [
                b.text for row in opened.responses[-1][1]["reply_markup"].keyboard
                for b in row
            ]
            assert store.list_items(user, completed=False)[0] == []
        guest = MessageStub(3, TODO_START)
        await handler(navigation_router, "on_todo")(guest)
        assert nav.current(3)[0] == "home"
        assert not guest.responses
    finally:
        nav.close()


@pytest.mark.asyncio
async def test_todo_text_flow_and_confirmed_delete_callbacks(store, tmp_path):
    nav = NavigationStore(tmp_path / "nav.sqlite3")
    service = TodoService(store)
    router = create_todo_router(
        service=service, navigation=nav, allowed_user_ids=frozenset({1, 2}),
    )
    try:
        nav.activate(1, "todo")
        start = MessageStub(1, TODO_NEW)
        await handler(router, "on_add")(start)
        assert nav.current(1)[0] == "todo_capture"
        assert "ОДНО" in start.responses[-1][0]
        text = MessageStub(1, "Купить продукты")
        await handler(router, "on_capture_text")(text)
        item = store.list_items(1, completed=False)[0][0]
        assert item.text == "Купить продукты"
        assert nav.current(1)[0] == "todo"
        assert service.saved_message(item, edited=False) in text.responses[0][0]
        assert store.list_items(2, completed=False)[0] == []

        message = MessageStub(1)
        cb = router.callback_query.handlers[0].callback
        await cb(Click(1, f"td:done:{item.id}", message))
        assert store.get(1, item.id).completed_at is not None
        await cb(Click(2, f"td:reopen:{item.id}", message))
        assert store.get(1, item.id).completed_at is not None
        await cb(Click(1, f"td:ask:{item.id}", message))
        confirm = message.responses[-1][1]["reply_markup"]
        token_data = confirm.inline_keyboard[0][0].callback_data
        assert token_data.startswith("td:del:")
        assert store.get(1, item.id) is not None
        await cb(Click(1, token_data, message))
        assert store.get(1, item.id) is None
    finally:
        nav.close()


@pytest.mark.asyncio
async def test_todo_capture_filter_does_not_swallow_other_modules(store, tmp_path):
    nav = NavigationStore(tmp_path / "nav.sqlite3")
    router = create_todo_router(
        service=TodoService(store), navigation=nav,
        allowed_user_ids=frozenset({1}),
    )
    h = next(h for h in router.message.handlers
             if h.callback.__name__ == "on_capture_text")
    try:
        nav.activate(1, "todo_capture")
        assert (await h.check(MessageStub(1, "Текст задачи")))[0]
        assert not (await h.check(MessageStub(1, "/start")))[0]
        assert not (await h.check(MessageStub(1, TODO_ACTIVE)))[0]
        assert not (await h.check(MessageStub(2, "Посторонний текст")))[0]
        nav.activate(1, "facts")
        assert not (await h.check(MessageStub(1, "В «Факты дня»")))[0]
    finally:
        nav.close()


class AudioMessage:
    def __init__(self, user_id=1):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.voice = SimpleNamespace(file_id="audio123", duration=2, file_size=4)
        self.audio = None
        self.document = None
        self.message_id = 19
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append(text)


class TelegramBot:
    def __init__(self):
        self.messages = []
        self.sent = asyncio.Event()

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="audio.ogg", file_size=4)

    async def download_file(self, remote_path, *, destination, timeout):
        Path(destination).write_bytes(b"ABCD")

    async def send_chat_action(self, chat_id, action):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        self.sent.set()


@pytest.mark.asyncio
async def test_todo_voice_is_bound_once_and_uses_facts_safe_routing(
    store, tmp_path, monkeypatch
):
    nav = NavigationStore(tmp_path / "nav.sqlite3")
    service = TodoService(store)
    settings = SimpleNamespace(
        allowed_user_ids=frozenset({1, 2}), max_audio_seconds=1200,
        max_file_size_bytes=10_000, temp_dir=tmp_path,
        download_timeout_seconds=10,
    )
    queue = JobQueue(max_pending_jobs=3, max_pending_per_user=3)
    router = audio_module.create_router(
        settings=settings, queue=queue, navigation=nav, todo=service,
    )
    telegram = TelegramBot()
    try:
        nav.activate(2, "todo_capture")
        token = store.begin_capture(2).token
        message = AudioMessage(2)
        await handler(router, "on_audio")(message, telegram)
        assert "одно дело" in message.replies[-1]
        job = await queue.get()
        assert job.todo_token == token
        assert job.navigation_section == "todo_capture"
        assert job.capture_token is None and job.day_facts_token is None
        queue.task_done()
        # Requeue for worker; token was claimed once at admission.
        await queue.put(job)

        class FakeTranscriber:
            async def transcribe(self, _path):
                return TranscriptionResult(
                    text="купить свежие продукты", language="ru",
                    language_probability=1.0, duration_seconds=2.0,
                    duration_after_vad_seconds=2.0, processing_seconds=0.1,
                )

        class Punctuation:
            async def format(self, text, *, fallback_text):
                return "Купить свежие продукты."

        monkeypatch.setattr(audio_module, "probe_duration_seconds", lambda _: 2.0)
        worker = asyncio.create_task(audio_module.run_worker(
            bot=telegram, queue=queue, transcriber=FakeTranscriber(),
            settings=settings, navigation=nav, todo=service,
            punctuation_processor=Punctuation(),
        ))
        try:
            await asyncio.wait_for(telegram.sent.wait(), timeout=4)
            assert len(store.list_items(2, completed=False)[0]) == 1
            assert store.list_items(2, completed=False)[0][0].text == "Купить свежие продукты."
            assert nav.current(2)[0] == "todo"
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
    finally:
        nav.close()


@pytest.mark.asyncio
async def test_todo_queued_voice_cannot_write_after_navigation_switch(
    store, tmp_path, monkeypatch,
):
    nav = NavigationStore(tmp_path / "nav.sqlite3")
    queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
    try:
        nav.activate(1, "todo_capture")
        token = store.begin_capture(1).token
        section, revision = nav.current(1)
        voice = tmp_path / "voice.ogg"
        voice.write_bytes(b"ABCD")
        assert await queue.reserve(1)
        await queue.put(AudioJob(
            chat_id=1, reply_to_message_id=7, user_id=1, file_path=voice,
            todo_token=token, navigation_section=section,
            navigation_revision=revision,
        ))
        nav.activate(1, "facts_capture")
        bot = TelegramBot()
        transcribed = []

        class FakeTranscriber:
            async def transcribe(self, path):
                transcribed.append(path)
                raise AssertionError("Should not transcribe stale module audio")

        worker = asyncio.create_task(audio_module.run_worker(
            bot=bot, queue=queue, transcriber=FakeTranscriber(),
            settings=SimpleNamespace(
                max_audio_seconds=1200, allowed_user_ids=frozenset({1}),
            ),
            navigation=nav, todo=TodoService(store),
        ))
        try:
            await asyncio.wait_for(bot.sent.wait(), timeout=3)
            assert "Режим изменился" in bot.messages[0][1]
            assert not transcribed
            assert store.list_items(1, completed=False)[0] == []
            assert not voice.exists()
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
    finally:
        nav.close()



@pytest.mark.asyncio
async def test_list_callback_uses_clicking_user_not_bot_message_author(store, tmp_path):
    """Telegram callback.message.from_user is normally the bot, not the viewer."""
    nav = NavigationStore(tmp_path / "nav-list.sqlite3")
    try:
        router = create_todo_router(
            service=TodoService(store), navigation=nav,
            allowed_user_ids=frozenset({1, 2}),
        )
        for user_id in (1, 2):
            token = store.begin_capture(user_id).token
            store.capture_text(user_id, token, f"Личное дело пользователя {user_id}")
        nav.activate(1, "todo")
        nav.activate(2, "todo")

        bot_message = MessageStub(999, text="Список")
        callback = Click(1, "td:list:a:1", bot_message)
        await router.callback_query.handlers[0].callback(callback)
        assert callback.acks
        assert len(bot_message.responses) == 1
        body = bot_message.responses[0][0]
        assert "Личное дело пользователя 1" in body
        assert "Личное дело пользователя 2" not in body

        unauthorized = Click(3, "td:list:a:1", bot_message)
        await router.callback_query.handlers[0].callback(unauthorized)
        assert unauthorized.acks
        assert len(bot_message.responses) == 1
    finally:
        nav.close()
