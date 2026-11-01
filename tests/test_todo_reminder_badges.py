"""UI reminder-bell indicators on active ToDo list buttons."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.menu import TODO_ACTIVE, TODO_DONE
from voqelis.navigation import NavigationStore
from voqelis.todo.bot import create_todo_router, list_markup
from voqelis.todo.reminders import ReminderRepository
from voqelis.todo.service import TodoService
from voqelis.todo.store import TodoStore


class Message:
    def __init__(self, uid: int, text: str) -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(id=uid, type="private")
        self.text = text
        self.responses: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.responses.append((text, kwargs))


class Click:
    def __init__(self, uid: int, data: str, message: Message) -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.data = data
        self.message = message
        self.acks: list[str | None] = []

    async def answer(self, text: str | None = None, **_kwargs) -> None:
        self.acks.append(text)


def task(store: TodoStore, user_id: int, title: str):
    draft = store.begin_capture(user_id)
    saved = store.capture_text(user_id, draft.token, title)
    assert saved is not None
    return saved


def remind(repo: ReminderRepository, uid: int, item_id: int, *, days: int = 1):
    now = int(time.time())
    draft = repo.begin(uid, item_id, now=now)
    assert repo.propose(
        uid, draft.token, datetime.fromtimestamp(now + days * 86400, UTC),
        now=now,
    ) is not None
    result = repo.confirm(uid, draft.token, now=now)
    assert result is not None
    return result


def labels(message: Message) -> dict[str, str]:
    markup = message.responses[-1][1]["reply_markup"]
    return {
        button.callback_data: button.text
        for row in markup.inline_keyboard for button in row
        if button.callback_data.startswith("td:item:")
    }


def test_pending_item_ids_bulk_owner_scoped_and_states(tmp_path: Path):
    store = TodoStore(tmp_path / "todo.sqlite3")
    try:
        repo = ReminderRepository(store)
        first = task(store, 1, "Первое")
        second = task(store, 1, "Второе")
        third = task(store, 1, "Третье")
        foreign = task(store, 2, "Чужое дело")
        remind(repo, 1, first.id)
        remind(repo, 1, first.id, days=2)
        cancelled = remind(repo, 1, second.id)
        assert repo.cancel(1, cancelled.id)
        sent = remind(repo, 1, third.id)
        with store.db:
            store.db.execute(
                "UPDATE todo_reminders SET state='sent' WHERE id=?",
                (sent.id,),
            )
        remind(repo, 2, foreign.id)

        ids = [first.id, second.id, third.id, foreign.id]
        assert repo.pending_item_ids(1, ids) == frozenset({first.id})
        assert repo.pending_item_ids(2, ids) == frozenset({foreign.id})
        assert repo.pending_item_ids(1, []) == frozenset()
        with pytest.raises(ValueError):
            repo.pending_item_ids(0, ids)
        with pytest.raises(ValueError):
            repo.pending_item_ids(1, [0])
    finally:
        store.close()


def test_button_text_has_only_one_bell_and_keeps_callback():
    store_item = SimpleNamespace(id=42, text="Купить продукты", completed_at=None)
    pending = frozenset({42})
    active = list_markup(
        [store_item], completed=False, page=1, total=1, reminder_item_ids=pending,
    )
    assert active.inline_keyboard[0][0].text == "#42 · Купить продукты 🔔"
    assert active.inline_keyboard[0][0].callback_data == "td:item:42"

    completed = list_markup(
        [store_item], completed=True, page=1, total=1, reminder_item_ids=pending,
    )
    assert completed.inline_keyboard[0][0].text == "#42 · Купить продукты"


@pytest.mark.asyncio
async def test_active_list_badges_refresh_and_do_not_leak_between_users(tmp_path: Path):
    store = TodoStore(tmp_path / "todo.sqlite3")
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    try:
        repo = ReminderRepository(store)
        first = task(store, 1, "Позвонить")
        second = task(store, 1, "Прогулка")
        outsider = task(store, 2, "Чужое дело")
        r1 = remind(repo, 1, first.id)
        remind(repo, 1, first.id, days=2)
        remind(repo, 2, outsider.id)
        router = create_todo_router(
            service=TodoService(store), navigation=nav,
            allowed_user_ids=frozenset({1, 2}),
        )
        nav.activate(1, "todo")
        nav.activate(2, "todo")
        on_active = next(
            h.callback for h in router.message.handlers
            if h.callback.__name__ == "on_active"
        )
        msg = Message(1, TODO_ACTIVE)
        await on_active(msg)
        shown = labels(msg)
        assert shown[f"td:item:{first.id}"].endswith(" 🔔")
        assert shown[f"td:item:{first.id}"].count("🔔") == 1
        assert not shown[f"td:item:{second.id}"].endswith(" 🔔")
        assert f"td:item:{outsider.id}" not in shown

        other = Message(2, TODO_ACTIVE)
        await on_active(other)
        assert labels(other)[f"td:item:{outsider.id}"].endswith(" 🔔")
        assert f"td:item:{first.id}" not in labels(other)

        # Cancelling one of two reminders keeps the marker.
        assert repo.cancel(1, r1.id)
        again = Message(1, TODO_ACTIVE)
        await on_active(again)
        assert labels(again)[f"td:item:{first.id}"].endswith(" 🔔")

        second_reminder = next(
            r for r in repo.for_item(1, first.id) if r.state == "pending"
        )
        assert repo.cancel(1, second_reminder.id)
        refreshed = Message(1, TODO_ACTIVE)
        await on_active(refreshed)
        assert not labels(refreshed)[f"td:item:{first.id}"].endswith(" 🔔")

        # Completed tasks must never show a pending-reminder marker.
        store.set_completed(1, first.id, completed=True)
        done = Message(1, TODO_DONE)
        on_done = next(
            h.callback for h in router.message.handlers
            if h.callback.__name__ == "on_done"
        )
        await on_done(done)
        assert not labels(done)[f"td:item:{first.id}"].endswith(" 🔔")
    finally:
        nav.close()
        store.close()


@pytest.mark.asyncio
async def test_second_page_uses_one_bulk_query_and_clicking_user(tmp_path: Path):
    store = TodoStore(tmp_path / "todo.sqlite3")
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    try:
        entries = [task(store, 1, f"Дело {n}") for n in range(11)]
        repo = ReminderRepository(store)
        remind(repo, 1, entries[0].id)
        nav.activate(1, "todo")
        router = create_todo_router(
            service=TodoService(store), navigation=nav,
            allowed_user_ids=frozenset({1}),
        )
        message = Message(999, "Страница 2")  # Telegram message authored by bot.
        callback = Click(1, "td:list:a:2", message)
        sql: list[str] = []
        store.db.set_trace_callback(sql.append)
        try:
            await router.callback_query.handlers[0].callback(callback)
        finally:
            store.db.set_trace_callback(None)
        assert callback.acks
        shown = labels(message)
        assert set(shown) == {f"td:item:{entries[0].id}"}
        assert shown[f"td:item:{entries[0].id}"].endswith(" 🔔")
        assert len([
            statement for statement in sql
            if statement.startswith("SELECT DISTINCT item_id FROM todo_reminders ")
        ]) == 1
    finally:
        nav.close()
        store.close()
