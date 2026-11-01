"""ToDo daily digest: defaults, recurrence, owner isolation, idempotency and UI."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.access import AccessStore
from voqelis.navigation import NavigationStore
from voqelis.todo.digest import (
    DEFAULT_MINUTE,
    TodoDigestRepository,
    clock,
    parse_clock,
)
from voqelis.todo.digest_delivery import send_due_digests
from voqelis.todo.digest_ui import create_todo_digest_router
from voqelis.todo.settings import TodoSettingsRepository
from voqelis.todo.settings_ui import create_todo_settings_router
from voqelis.todo.store import TodoStore


@pytest.fixture
def data(tmp_path: Path):
    todo = TodoStore(tmp_path / "todo.sqlite3")
    zones = TodoSettingsRepository(todo)
    digests = TodoDigestRepository(todo, zones)
    yield todo, zones, digests
    todo.close()


def add_item(todo: TodoStore, uid: int, text: str = "Купить продукты"):
    capture = todo.begin_capture(uid)
    item = todo.capture_text(uid, capture.token, text)
    assert item is not None
    return item


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def test_default_daily_16_00_moscow_and_persistent_editing(data, tmp_path):
    todo, _, digests = data
    default = digests.get(1)
    assert default.enabled and default.times == (DEFAULT_MINUTE,) and default.revision == 0
    assert clock(DEFAULT_MINUTE) == "16:00"
    assert parse_clock("09:05") == 545
    changed = digests.add(1, 0, 545)
    assert changed is not None and changed.times == (545, 960)
    assert digests.get(2).times == (960,)
    todo.close()
    reopened = TodoStore(tmp_path / "todo.sqlite3")
    try:
        saved = TodoDigestRepository(reopened, TodoSettingsRepository(reopened)).get(1)
        assert saved.times == (545, 960) and saved.enabled
    finally:
        reopened.close()


def test_disable_reenable_and_remove_all_times(data):
    _, _, digests = data
    disabled = digests.toggle(1, 0)
    assert disabled and not disabled.enabled and disabled.times == (960,)
    assert digests.toggle(1, 0) is None
    enabled = digests.toggle(1, disabled.revision)
    assert enabled and enabled.enabled
    empty = digests.remove(1, enabled.revision, 960)
    assert empty and not empty.enabled and empty.times == ()
    restored = digests.toggle(1, empty.revision)
    assert restored and restored.enabled and restored.times == (960,)


def test_max_five_times_and_bad_input_are_rejected(data):
    _, _, digests = data
    p = digests.get(1)
    for minute in (450, 600, 720, 1080):
        p = digests.add(1, p.revision, minute)
        assert p is not None
    with pytest.raises(ValueError, match="пяти"):
        digests.add(1, p.revision, 1200)
    with pytest.raises(ValueError):
        digests.add(1, p.revision, 960)
    for value in ("9:00", "25:01", "12:60", "завтра", "", "12.45"):
        with pytest.raises(ValueError):
            parse_clock(value)


def test_no_delivery_for_disabled_empty_or_unapproved_users(data):
    todo, _, digests = data
    add_item(todo, 1)
    add_item(todo, 2)
    timestamp = utc(2026, 11, 2, 13)
    due = digests.claim_due(frozenset({2}), now=timestamp)
    assert len(due) == 1 and due[0].user_id == 2
    # Due claim must not include any ID outside the approved set.
    rows = todo.db.execute(
        "SELECT user_id FROM todo_digest_delivery",
    ).fetchall()
    assert {row["user_id"] for row in rows} == {2}
    assert digests.claim_due(frozenset({2}), now=timestamp) == []
    disabled = digests.toggle(1, 0)
    assert disabled and not disabled.enabled
    assert digests.claim_due(frozenset({1}), now=timestamp) == []
    assert {row["user_id"] for row in todo.db.execute(
        "SELECT user_id FROM todo_digest_delivery",
    ).fetchall()} == {2}


def test_clock_timezone_and_window(data):
    todo, zones, digests = data
    add_item(todo, 1)
    add_item(todo, 2)
    zones.set_timezone(2, "Europe/Berlin", expected_revision=0)
    morning = utc(2026, 11, 2, 13)
    due = digests.claim_due(frozenset({1, 2}), now=morning, limit=5)
    assert [(x.user_id, x.day, x.minute) for x in due] == [(1, "2026-11-02", 960)]
    assert digests.claim_due(frozenset({1, 2}), now=morning + 45) == []
    later = digests.claim_due(frozenset({1, 2}), now=utc(2026, 11, 2, 15))
    assert len(later) == 1 and later[0].user_id == 2
    assert later[0].timezone == "Europe/Berlin"
    assert digests.claim_due(frozenset({1, 2}), now=utc(2026, 11, 3, 13, 11)) == []


def test_crash_recovery_never_duplicates_and_audit(data):
    todo, _, digests = data
    add_item(todo, 1)
    now = utc(2026, 11, 2, 13)
    [claimed] = digests.claim_due(frozenset({1}), now=now)
    assert digests.recover_inflight() == 1
    assert digests.claim_due(frozenset({1}), now=now + 20) == []
    assert not digests.finish(claimed, "sent", now=now + 40)
    state = todo.db.execute(
        "SELECT state FROM todo_digest_delivery WHERE user_id=1",
    ).fetchone()[0]
    assert state == "uncertain"


def test_no_active_tasks_records_skip_preventing_later_duplicates(data):
    todo, _, digests = data
    timestamp = utc(2026, 11, 2, 13)
    assert digests.claim_due(frozenset({1}), now=timestamp) == []
    row = todo.db.execute(
        "SELECT state FROM todo_digest_delivery WHERE user_id=1",
    ).fetchone()
    assert row["state"] == "skipped"
    add_item(todo, 1)
    assert digests.claim_due(frozenset({1}), now=timestamp + 5) == []


def test_custom_input_consumed_once_and_invalid_input_kept(data):
    _, _, digests = data
    digests.begin_input(1, nav_revision=5, digest_revision=0)
    draft = digests.pending_input(1)
    assert draft is not None
    with pytest.raises(ValueError):
        digests.submit_input(1, draft.token, 5, "wrong")
    assert digests.pending_input(1) is not None
    assert digests.submit_input(1, "wrong-token", 5, "09:15") is None
    prefs = digests.submit_input(1, draft.token, 5, "09:15")
    assert prefs and prefs.times == (555, 960)
    assert digests.pending_input(1) is None
    assert digests.submit_input(1, draft.token, 5, "09:15") is None


class FakeMessage:
    def __init__(self, uid=1, text=""):
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")
        self.text = text
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))


class FakeClick:
    def __init__(self, uid: int, data: str, message: FakeMessage):
        self.from_user = SimpleNamespace(id=uid)
        self.message = message
        self.data = data
        self.acks = []

    async def answer(self, text=None, **kwargs):
        self.acks.append((text, kwargs))


def handler(router, name, *, callback=False):
    handlers = router.callback_query.handlers if callback else router.message.handlers
    return next(h.callback for h in handlers if h.callback.__name__ == name)


@pytest.mark.asyncio
async def test_digest_ui_controls_and_open_from_any_section(data, tmp_path):
    todo, zones, digests = data
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1, 2}))
    try:
        nav.activate(1, "todo")
        settings_router = create_todo_settings_router(
            settings=zones, navigation=nav, access=access, digests=digests,
        )
        router = create_todo_digest_router(
            digests=digests, timezones=zones, navigation=nav, access=access,
        )
        msg = FakeMessage(1)
        await handler(settings_router, "on_settings")(msg)
        assert "16:00" in msg.responses[-1][0]
        buttons = msg.responses[-1][1]["reply_markup"].inline_keyboard
        menu_data = buttons[1][0].callback_data
        await handler(router, "on_digest_callback", callback=True)(
            FakeClick(1, menu_data, msg)
        )
        assert "Включена" in msg.responses[-1][0]
        panel = msg.responses[-1][1]["reply_markup"].inline_keyboard
        toggle = panel[0][0].callback_data
        changed = FakeClick(1, toggle, msg)
        await handler(router, "on_digest_callback", callback=True)(changed)
        assert "Выключена" in msg.responses[-1][0]
        replay = FakeClick(1, toggle, msg)
        await handler(router, "on_digest_callback", callback=True)(replay)
        assert "устарела" in replay.acks[-1][0]
        foreign = FakeClick(3, menu_data, msg)
        await handler(router, "on_digest_callback", callback=True)(foreign)
        assert digests.get(3).revision == 0

        add_item(todo, 1)
        nav.activate(1, "home")
        open_click = FakeClick(1, "td:digest:open", msg)
        await handler(router, "on_digest_callback", callback=True)(open_click)
        assert nav.current(1)[0] == "todo"
        assert "Купить продукты" in msg.responses[-1][0]
    finally:
        access.close()
        nav.close()


@pytest.mark.asyncio
async def test_digest_delivery_only_approved_and_once(data, tmp_path, monkeypatch):
    todo, _, digests = data
    add_item(todo, 1, "Купить продукты")
    add_item(todo, 2, "Секретное дело")
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    class Bot:
        def __init__(self):
            self.calls = []

        async def send_message(self, uid, message, **kwargs):
            self.calls.append((uid, message, kwargs))

    async def quick_sleep(_):
        return None

    from voqelis.todo import digest_delivery

    monkeypatch.setattr(digest_delivery.asyncio, "sleep", quick_sleep)
    bot = Bot()
    try:
        now = utc(2026, 11, 2, 13)
        assert await send_due_digests(bot, digests, access, now=now) == 1
        assert len(bot.calls) == 1 and bot.calls[0][0] == 1
        assert "Купить продукты" in bot.calls[0][1]
        assert "Секретное дело" not in bot.calls[0][1]
        assert "Europe/Moscow" not in bot.calls[0][1]
        assert bot.calls[0][2]["reply_markup"].inline_keyboard[0][0].callback_data == "td:digest:open"
        assert await send_due_digests(bot, digests, access, now=now + 30) == 0
        assert len(bot.calls) == 1
        assert todo.db.execute(
            "SELECT state FROM todo_digest_delivery WHERE user_id=1"
        ).fetchone()[0] == "sent"
    finally:
        access.close()


def test_legacy_sqlite_additive_digest_tables_preserve_tasks(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE todo_items(id INTEGER PRIMARY KEY, user_id INTEGER, "
            "text TEXT, source TEXT, created_at TEXT, updated_at TEXT, "
            "completed_at TEXT, deleted_at TEXT)"
        )
        db.execute(
            "INSERT INTO todo_items VALUES(1,1,'Старое дело','text','x','x',NULL,NULL)"
        )
    todo = TodoStore(path)
    try:
        assert todo.get(1, 1).text == "Старое дело"
        assert TodoDigestRepository(todo, TodoSettingsRepository(todo)).get(1).times == (960,)
        assert todo.db.execute(
            "SELECT name FROM sqlite_master WHERE name='todo_digest_delivery'"
        ).fetchone() is not None
    finally:
        todo.close()
