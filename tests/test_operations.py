"""Operations V1 regression: permissions, privacy, report tokens, and Telegram UI."""

from __future__ import annotations

import asyncio
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis import bot as audio_module
from voqelis.access import AccessStore
from voqelis.audio import AudioProcessingError
from voqelis.domain import AudioJob
from voqelis.menu import REPORT_BUTTON, main_keyboard
from voqelis.navigation import NavigationStore
from voqelis.operations import store as ops_store_module
from voqelis.operations.bot import create_operations_router
from voqelis.operations.store import OperationsStore
from voqelis.operations.telemetry import OperationsTelemetry
from voqelis.queue import JobQueue


@pytest.fixture
def data(tmp_path: Path):
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    ops = OperationsStore(tmp_path / "operations.sqlite3", owner_id=access.admin_id)
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    try:
        yield access, ops, nav
    finally:
        nav.close()
        ops.close()
        access.close()


def approve(access: AccessStore, user_id: int) -> None:
    assert access.request(user_id, display_name="Друг", username="guest") == "created"
    assert access.decide(1, user_id, approve=True)


def callback_handler(router):
    return router.callback_query.handlers[0].callback


def message_handler(router, name):
    return next(h.callback for h in router.message.handlers
                if h.callback.__name__ == name)


class Message:
    def __init__(self, user_id: int = 1, text: str = "") -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.text = text
        self.messages = []

    async def answer(self, text, **kwargs):
        self.messages.append((text, kwargs))


class Callback:
    def __init__(self, user_id: int, data: str, message: Message):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class Bot:
    def __init__(self):
        self.notifications = []

    async def send_message(self, user_id, text):
        self.notifications.append((user_id, text))


def test_initially_only_owner_allowed_and_approval_is_explicit(data):
    access, store, _ = data
    assert store.owner_id == 1
    assert store.eligible(1, approved=access.is_allowed(1))
    assert not store.eligible(2, approved=False)
    assert store.reporter_id() is None
    assert not store.set_reporter(2, 3, target_approved=True)
    assert not store.set_reporter(1, 3, target_approved=False)
    approve(access, 2)
    approve(access, 3)
    assert store.set_reporter(1, 2, target_approved=access.is_allowed(2))
    assert store.eligible(2, approved=access.is_allowed(2))
    assert not store.eligible(3, approved=access.is_allowed(3))
    assert store.set_reporter(1, 3, target_approved=access.is_allowed(3))
    assert not store.eligible(2, approved=access.is_allowed(2))
    assert store.eligible(3, approved=access.is_allowed(3))
    assert access.revoke(1, 3)
    assert not store.eligible(3, approved=access.is_allowed(3))
    assert store.set_reporter(1, None, target_approved=True)
    assert store.reporter_id() is None


def test_store_is_private_persistent_and_rejects_symlink(tmp_path):
    p = tmp_path / "operations.sqlite3"
    first = OperationsStore(p, owner_id=1)
    try:
        first.record(2, "todo_capture", "message", "handled")
        first.set_reporter(1, 2, target_approved=True)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    finally:
        first.close()
    second = OperationsStore(p, owner_id=1)
    try:
        assert second.reporter_id() == 2
        assert second.summary(2, 10)["handled"] == 1
        assert second.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        second.close()
    (tmp_path / "symlink.sqlite3").symlink_to(p)
    with pytest.raises(OSError):
        OperationsStore(tmp_path / "symlink.sqlite3", owner_id=1)


def test_events_never_include_message_payloads_and_aggregate_by_user(data):
    _, store, _ = data
    now = int(time.time())
    store.record(1, "facts", "message", "handled", 120, now=now - 100)
    store.record(1, "planner", "audio", "failed", 1000, now=now - 150)
    store.record(2, "todo", "callback", "handled", 30, now=now - 200)
    store.record(1, "secrets", "message", "handled", now=now - 4000)
    assert store.summary(1, 10, now=now) == {
        "handled": 1, "failed": 1, "completed": 0,
    }
    assert store.summary(None, 10, now=now)["handled"] == 2
    row = store.db.execute("SELECT * FROM ops_events WHERE user_id=1 "
                           "AND occurred_at=?", (now - 4000,)).fetchone()
    assert row["section"] == "unknown"
    keys = tuple(row.keys())
    assert "text" not in keys
    assert "token" not in keys
    with pytest.raises(ValueError):
        store.record(1, "todo", "voice_text", "handled")
    with pytest.raises(ValueError):
        store.summary(1, 15)


def test_report_atomic_single_use_window_limit_and_retention(data):
    _, store, _ = data
    now = int(time.time())
    store.record(2, "todo", "audio", "completed", 2200, now=now)
    for i in range(3):
        token = store.begin_report(2, "Плохое поведение")
        report = store.submit_report(2, token, 10, now=now + i)
        assert report is not None
        assert report.snapshot["completed"] == 1
        assert report.description == "Плохое поведение"
        assert store.submit_report(2, token, 10, now=now + i) is None
    extra = store.begin_report(2)
    assert store.submit_report(2, extra, 10, now=now + 4) is None
    assert len(store.reports()) == 3
    assert store.resolve(store.reports()[0].id)
    assert not store.resolve(store.reports()[0].id)
    assert store.reports()[0].status == "resolved"
    assert store.get_report(9999) is None
    with pytest.raises(ValueError):
        store.reports(0)


def test_report_without_description_and_expired_token(data):
    _, store, _ = data
    token = store.begin_report(1)
    assert store.submit_report(1, "wrong", 10) is None
    assert store.submit_report(1, token, 15) is None
    with store.db:
        store.db.execute("UPDATE ops_report_requests SET expires_at=0")
    assert store.submit_report(1, token, 10) is None
    next_token = store.begin_report(1, "Очень плохое\ngesture \x00" + "x" * 900)
    report = store.submit_report(1, next_token, 30)
    assert report is not None
    assert "\n" not in report.description
    assert "\x00" not in report.description
    assert len(report.description) <= 800


def test_owner_and_reporter_keyboard_opt_in():
    base = [b.text for row in main_keyboard().keyboard for b in row]
    owner = [b.text for row in main_keyboard(admin=True, reporter=True).keyboard for b in row]
    friend = [b.text for row in main_keyboard(reporter=True).keyboard for b in row]
    assert REPORT_BUTTON not in base
    assert REPORT_BUTTON in owner and REPORT_BUTTON in friend


@pytest.mark.asyncio
async def test_report_router_owner_and_friend_submit_and_admin_notification(data):
    access, store, nav = data
    approve(access, 2)
    assert store.set_reporter(1, 2, target_approved=True)
    router = create_operations_router(store=store, access=access, navigation=nav)
    report_handler = message_handler(router, "on_report")
    cb_handler = callback_handler(router)
    tg = Bot()

    denied = Message(3, REPORT_BUTTON)
    await report_handler(denied)
    assert not denied.messages

    friend = Message(2, REPORT_BUTTON)
    await report_handler(friend)
    assert friend.messages
    assert nav.current(2)[0] == "home"
    token = friend.messages[0][1]["reply_markup"].inline_keyboard[0][0].callback_data
    assert token.startswith("ops:submit:10:")

    unsafe = Callback(3, token, friend)
    await cb_handler(unsafe, tg)
    assert unsafe.answers[-1][1]["show_alert"] is True
    assert not store.reports()

    sent = Callback(2, token, friend)
    await cb_handler(sent, tg)
    assert len(store.reports()) == 1
    assert tg.notifications == [(
        1, "🐞 Новый отчёт VQ-1 от пользователя 2. Открой /ops для просмотра.",
    )]
    await cb_handler(sent, tg)
    assert len(store.reports()) == 1


@pytest.mark.asyncio
async def test_non_admin_cannot_read_reports_or_grant_self(data):
    access, store, nav = data
    approve(access, 2)
    router = create_operations_router(store=store, access=access, navigation=nav)
    callback = callback_handler(router)
    msg = Message(999)
    for data_value in ("ops:admin", "ops:reports:1", "ops:diag:10",
                       "ops:grant:2", "ops:reporters:0"):
        denied = Callback(2, data_value, msg)
        await callback(denied, Bot())
        assert denied.answers[-1][1]["show_alert"] is True
        assert not msg.messages
    assert store.reporter_id() is None


@pytest.mark.asyncio
async def test_admin_can_assign_one_friend_and_view_anonymized_reports(data):
    access, store, nav = data
    approve(access, 2)
    router = create_operations_router(store=store, access=access, navigation=nav)
    cb = callback_handler(router)
    msg = Message(999)
    await cb(Callback(1, "ops:grant:2", msg), Bot())
    assert store.reporter_id() == 2
    store.record(2, "todo", "callback", "handled")
    token = store.begin_report(2)
    report = store.submit_report(2, token, 10)
    assert report is not None
    await cb(Callback(1, f"ops:view:{report.id}", msg), Bot())
    assert "Обработано" not in msg.messages[-1][0] or "Технические события" in msg.messages[-1][0]
    assert f"VQ-{report.id}" in msg.messages[-1][0]
    await cb(Callback(1, f"ops:resolve:{report.id}", msg), Bot())
    assert store.get_report(report.id).status == "resolved"
    await cb(Callback(1, "ops:clear", msg), Bot())
    assert store.reporter_id() is None


@pytest.mark.asyncio
async def test_telemetry_reports_handled_and_failed_without_swallowing(data):
    access, store, nav = data
    telemetry = OperationsTelemetry(store, nav, access.allowed)
    event = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        chat=SimpleNamespace(type="private"),
        data=None,
    )

    async def success(event, state):
        return 7

    assert await telemetry(success, event, {}) == 7

    async def fails(event, state):
        raise RuntimeError("secret user text must not be recorded")

    with pytest.raises(RuntimeError):
        await telemetry(fails, event, {})

    summary = store.summary(1, 10)
    assert summary["handled"] == 1
    assert summary["failed"] == 1
    assert "secret user text" not in str(store.db.execute(
        "SELECT sql FROM sqlite_master WHERE name='ops_events'"
    ).fetchone()[0])
    assert len(store.db.execute("SELECT * FROM ops_events").fetchall()) == 2

    async def outsider(event, state):
        return None

    event.from_user.id = 3
    await telemetry(outsider, event, {})
    assert len(store.db.execute("SELECT * FROM ops_events").fetchall()) == 2



def test_diagnostic_journal_is_bounded_even_when_active(data, monkeypatch):
    _, store, _ = data
    monkeypatch.setattr(ops_store_module, "MAX_EVENTS", 50)
    for _ in range(100):
        store.record(1, "home", "message", "handled")
    count = store.db.execute("SELECT COUNT(*) FROM ops_events").fetchone()[0]
    assert count == 50
    assert store.summary(1, 10)["handled"] == 50



@pytest.mark.asyncio
async def test_worker_audio_probe_failure_is_reported_without_transcript(
    data, tmp_path, monkeypatch,
):
    _, store, nav = data
    path = tmp_path / "invalid.ogg"
    path.write_bytes(b"bad")
    queue = JobQueue(max_pending_jobs=2, max_pending_per_user=2)
    assert await queue.reserve(1)
    await queue.put(AudioJob(
        chat_id=1, reply_to_message_id=11, user_id=1, file_path=path,
        navigation_section="home", navigation_revision=0,
    ))

    def bad_audio(_path):
        raise AudioProcessingError("unsupported audio")

    monkeypatch.setattr(audio_module, "probe_duration_seconds", bad_audio)

    class StubBot:
        def __init__(self):
            self.event = asyncio.Event()
            self.sent = []

        async def send_chat_action(self, chat_id, action):
            return None

        async def send_message(self, chat_id, text, **kwargs):
            self.sent.append(text)
            self.event.set()

    class StubTranscriber:
        async def transcribe(self, _path):
            raise AssertionError("Must not transcribe invalid audio")

    bot = StubBot()
    worker = asyncio.create_task(audio_module.run_worker(
        bot=bot, queue=queue, transcriber=StubTranscriber(),
        settings=SimpleNamespace(
            allowed_user_ids=frozenset({1}), max_audio_seconds=1200,
        ),
        navigation=nav, operations=store,
    ))
    try:
        await asyncio.wait_for(bot.event.wait(), timeout=4)
        assert "Не получилось прочитать аудиофайл" in bot.sent[0]
        assert store.summary(1, 10)["failed"] == 1
        assert store.summary(1, 10)["completed"] == 0
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert not path.exists()
