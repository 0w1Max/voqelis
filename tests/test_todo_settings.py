"""ToDo V2.2 preferences: local timezone, scoped callbacks and legacy-safe storage."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.access import AccessStore
from voqelis.menu import TODO_SETTINGS, todo_keyboard
from voqelis.navigation import NavigationStore
from voqelis.todo.reminder_ui import _display, create_reminder_router
from voqelis.todo.reminders import ReminderRepository
from voqelis.todo.settings import (
    DEFAULT_TIMEZONE,
    TodoSettingsRepository,
    timezone_label,
)
from voqelis.todo.settings_ui import create_todo_settings_router
from voqelis.todo.store import TodoStore


def handler(router, name, *, callback=False):
    entries = router.callback_query.handlers if callback else router.message.handlers
    return next(item.callback for item in entries if item.callback.__name__ == name)


class FakeMessage:
    def __init__(self, uid: int, text: str = TODO_SETTINGS):
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(type="private", id=uid)
        self.text = text
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))


class FakeClick:
    def __init__(self, uid: int, data: str, message: FakeMessage):
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.message = message
        self.acks = []

    async def answer(self, text=None, **kwargs):
        self.acks.append((text, kwargs))


def add_item(todo: TodoStore, uid: int):
    draft = todo.begin_capture(uid)
    item = todo.capture_text(uid, draft.token, "Проверка")
    assert item is not None
    return item


def test_timezone_defaults_are_isolated_and_durable(tmp_path: Path):
    path = tmp_path / "todo.sqlite3"
    todo = TodoStore(path)
    prefs = TodoSettingsRepository(todo)
    assert prefs.get(1).timezone == DEFAULT_TIMEZONE
    assert prefs.get(2).revision == 0
    updated = prefs.set_timezone(1, "Europe/Berlin", expected_revision=0)
    assert updated is not None and updated.revision == 1
    assert prefs.get(2).timezone == DEFAULT_TIMEZONE
    todo.close()

    reopened = TodoStore(path)
    try:
        assert TodoSettingsRepository(reopened).get(1).timezone == "Europe/Berlin"
        assert TodoSettingsRepository(reopened).get(2).timezone == DEFAULT_TIMEZONE
        assert timezone_label(DEFAULT_TIMEZONE) == "Moscow (GMT+3)"
    finally:
        reopened.close()


def test_existing_v21_sqlite_automatically_adds_only_settings_table(tmp_path: Path):
    path = tmp_path / "todo.sqlite3"
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            "CREATE TABLE todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
            "completed_at TEXT, deleted_at TEXT)"
        )
        legacy.execute(
            "INSERT INTO todo_items(user_id,text,source,created_at,updated_at) "
            "VALUES (1,'Старое дело','text','2026-10-31','2026-10-31')"
        )
    todo = TodoStore(path)
    try:
        assert todo.get(1, 1).text == "Старое дело"
        assert TodoSettingsRepository(todo).get(1).timezone == DEFAULT_TIMEZONE
    finally:
        todo.close()


def test_timezone_change_cancels_only_draft_not_saved_reminder(tmp_path: Path):
    todo = TodoStore(tmp_path / "todo.sqlite3")
    try:
        prefs = TodoSettingsRepository(todo)
        reminders = ReminderRepository(todo)
        task = add_item(todo, 1)
        before = datetime.now(UTC) + timedelta(days=2)
        draft = reminders.begin(1, task.id, timezone=DEFAULT_TIMEZONE)
        assert reminders.propose(1, draft.token, before) is not None
        saved = reminders.confirm(1, draft.token)
        assert saved is not None
        old_timestamp = saved.remind_at_utc
        new_draft = reminders.begin(1, task.id, timezone=DEFAULT_TIMEZONE)
        assert reminders.pending(1).token == new_draft.token

        changed = prefs.set_timezone(1, "Europe/Berlin", expected_revision=0)
        assert changed is not None and changed.timezone == "Europe/Berlin"
        assert reminders.pending(1) is None
        retained = reminders.get(1, saved.id)
        assert retained is not None
        assert retained.remind_at_utc == old_timestamp
        assert retained.timezone == DEFAULT_TIMEZONE
        assert prefs.get(2).timezone == DEFAULT_TIMEZONE
    finally:
        todo.close()


def test_timezone_rejects_invalid_and_stale_updates(tmp_path: Path):
    todo = TodoStore(tmp_path / "todo.sqlite3")
    try:
        prefs = TodoSettingsRepository(todo)
        with pytest.raises(ValueError):
            prefs.set_timezone(1, "../etc/passwd", expected_revision=0)
        with pytest.raises(ValueError):
            prefs.set_timezone(-1, DEFAULT_TIMEZONE, expected_revision=0)
        assert prefs.set_timezone(1, "Europe/Berlin", expected_revision=0) is not None
        assert prefs.set_timezone(1, DEFAULT_TIMEZONE, expected_revision=0) is None
        assert prefs.get(1).timezone == "Europe/Berlin"
    finally:
        todo.close()


@pytest.mark.asyncio
async def test_settings_keyboard_scoped_and_stale_callbacks(tmp_path: Path):
    todo = TodoStore(tmp_path / "todo.sqlite3")
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1, 2}))
    try:
        prefs = TodoSettingsRepository(todo)
        router = create_todo_settings_router(settings=prefs, navigation=nav, access=access)
        nav.activate(1, "todo")
        nav.activate(2, "todo")
        assert TODO_SETTINGS in [
            b.text for row in todo_keyboard().keyboard for b in row
        ]

        msg = FakeMessage(1)
        await handler(router, "on_settings")(msg)
        assert "Moscow (GMT+3)" in msg.responses[-1][0]
        first = msg.responses[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data

        pick = FakeClick(1, first, msg)
        await handler(router, "on_settings_callback", callback=True)(pick)
        assert len(msg.responses) == 2
        # Kaliningrad is the second location on page 1.
        choice = msg.responses[-1][1]["reply_markup"].inline_keyboard[0][1].callback_data
        applied = FakeClick(1, choice, msg)
        await handler(router, "on_settings_callback", callback=True)(applied)
        assert prefs.get(1).timezone == "Europe/Kaliningrad"
        assert prefs.get(2).timezone == DEFAULT_TIMEZONE

        replay = FakeClick(1, choice, msg)
        await handler(router, "on_settings_callback", callback=True)(replay)
        assert "устарела" in replay.acks[-1][0]
        assert prefs.get(1).revision == 1

        foreign = FakeClick(3, choice, msg)
        await handler(router, "on_settings_callback", callback=True)(foreign)
        assert prefs.get(3).timezone == DEFAULT_TIMEZONE

        nav.activate(1, "home")
        stale = FakeClick(1, msg.responses[-1][1]["reply_markup"].inline_keyboard[0][0].callback_data, msg)
        await handler(router, "on_settings_callback", callback=True)(stale)
        assert "устарела" in stale.acks[-1][0]
    finally:
        access.close()
        nav.close()
        todo.close()


@pytest.mark.asyncio
async def test_new_reminder_uses_saved_timezone_without_changing_old_utc(tmp_path: Path):
    todo = TodoStore(tmp_path / "todo.sqlite3")
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    try:
        prefs = TodoSettingsRepository(todo)
        prefs.set_timezone(1, "Europe/Berlin", expected_revision=0)
        reminders = ReminderRepository(todo)
        task = add_item(todo, 1)
        nav.activate(1, "todo")
        router = create_reminder_router(
            reminders=reminders, navigation=nav, access=access, settings=prefs,
        )
        msg = FakeMessage(1)
        click = FakeClick(1, f"td:rem:new:{task.id}", msg)
        await handler(router, "on_reminder_callback", callback=True)(click)
        current = reminders.pending(1)
        assert current is not None and current.timezone == "Europe/Berlin"
        assert "Europe/Berlin" not in msg.responses[-1][0]
        assert "Europe/Moscow" not in _display(int(datetime.now(UTC).timestamp()),
                                               "Europe/Moscow")
    finally:
        access.close()
        nav.close()
        todo.close()
