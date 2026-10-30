"""ToDo V2.1b: interactive reminder picker, ownership and delivery regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.access import AccessStore
from voqelis.navigation import NavigationStore
from voqelis.todo.delivery import _send_one
from voqelis.todo.reminder_ui import create_reminder_router
from voqelis.todo.reminders import ReminderRepository
from voqelis.todo.store import TodoStore


class StubMessage:
    def __init__(self, uid: int, text: str = "") -> None:
        self.chat = SimpleNamespace(type="private", id=uid)
        self.from_user = SimpleNamespace(id=uid)
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))


class StubCallback:
    def __init__(self, uid: int, data: str, msg: StubMessage) -> None:
        self.from_user = SimpleNamespace(id=uid)
        self.message = msg
        self.data = data
        self.replies = []

    async def answer(self, text=None, **kwargs):
        self.replies.append((text, kwargs))


@pytest.fixture
def ctx(tmp_path: Path):
    todo = TodoStore(tmp_path / "todo.sqlite3")
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    reminders = ReminderRepository(todo)
    router = create_reminder_router(reminders=reminders, navigation=nav, access=access)
    try:
        yield todo, nav, access, reminders, router
    finally:
        access.close()
        nav.close()
        todo.close()


def add(todo: TodoStore):
    draft = todo.begin_capture(1)
    item = todo.capture_text(1, draft.token, "Купить лекарства")
    assert item
    return item


def choose_button(message: StubMessage, label: str) -> str:
    markup = message.sent[-1][1]["reply_markup"]
    return next(
        b.callback_data for row in markup.inline_keyboard for b in row
        if label in b.text
    )


@pytest.mark.asyncio
async def test_picker_choose_date_time_confirm_and_cancel(ctx):
    todo, nav, _access, reminder_repo, router = ctx
    task = add(todo)
    nav.activate(1, "todo")
    handler = router.callback_query.handlers[0].callback
    msg = StubMessage(1)
    await handler(StubCallback(1, f"td:rem:new:{task.id}", msg))
    assert "Когда напомнить" in msg.sent[-1][0]
    tomorrow = choose_button(msg, "Завтра")
    await handler(StubCallback(1, tomorrow, msg))
    time_button = choose_button(msg, "18:00")
    await handler(StubCallback(1, time_button, msg))
    assert "Подтверди" in msg.sent[-1][0]
    confirm = choose_button(msg, "Сохранить")
    await handler(StubCallback(1, confirm, msg))
    assert len(reminder_repo.for_item(1, task.id)) == 1
    await handler(StubCallback(1, confirm, msg))
    assert len(reminder_repo.for_item(1, task.id)) == 1
    await handler(StubCallback(1, f"td:rem:cancel", msg))
    assert todo.get(1, task.id) is not None


@pytest.mark.asyncio
async def test_manual_text_date_and_foreign_buttons(ctx):
    todo, nav, _access, reminders, router = ctx
    task = add(todo)
    nav.activate(1, "todo")
    cb = router.callback_query.handlers[0].callback
    msg = StubMessage(1)
    await cb(StubCallback(1, f"td:rem:new:{task.id}", msg))
    initial = reminders.pending(1)
    assert initial
    outsider = StubMessage(2)
    await cb(StubCallback(2, f"td:rem:ok:{initial.token}", outsider))
    assert reminders.for_item(2, task.id) == []
    text = StubMessage(1, "послезавтра в 18:30")
    await router.message.handlers[0].callback(text)
    assert "Подтверди" in text.sent[-1][0]
    await cb(StubCallback(1, choose_button(text, "Сохранить"), msg))
    assert len(reminders.for_item(1, task.id)) == 1


@pytest.mark.asyncio
async def test_stale_navigation_and_done_callback_from_other_section(ctx):
    todo, nav, _access, reminders, router = ctx
    task = add(todo)
    nav.activate(1, "todo")
    handler = router.callback_query.handlers[0].callback
    msg = StubMessage(1)
    await handler(StubCallback(1, f"td:rem:new:{task.id}", msg))
    stale = reminders.pending(1)
    nav.activate(1, "home")
    nav.activate(1, "todo")
    await handler(StubCallback(1, f"td:rem:ok:{stale.token}", msg))
    assert reminders.for_item(1, task.id) == []
    await handler(StubCallback(1, f"td:rem:new:{task.id}", msg))
    updated = reminders.pending(1)
    assert updated.navigation_revision > stale.navigation_revision
    now = datetime.now(UTC)
    reminders.propose(1, updated.token, now + timedelta(days=1))
    reminder = reminders.confirm(1, updated.token)
    assert reminder
    nav.activate(1, "home")
    await handler(StubCallback(1, f"td:rem:complete:{task.id}", msg))
    assert todo.get(1, task.id).completed_at is not None
    assert reminders.get(1, reminder.id).state == "cancelled"


@pytest.mark.asyncio
async def test_sender_delivers_only_allowed_active_task(ctx):
    todo, _nav, access, reminders, _router = ctx
    task = add(todo)
    now = int(datetime.now(UTC).timestamp())
    draft = reminders.begin(1, task.id)
    reminders.propose(1, draft.token, datetime.fromtimestamp(now + 3600, UTC))
    saved = reminders.confirm(1, draft.token)
    assert saved
    due = reminders.claim_due(now=saved.remind_at_utc)
    assert len(due) == 1

    class StubBot:
        def __init__(self):
            self.deliveries = []

        async def send_message(self, uid, text, **kwargs):
            self.deliveries.append((uid, text, kwargs))
            return SimpleNamespace(message_id=1)

    bot = StubBot()
    await _send_one(bot, reminders, access, due[0])
    assert len(bot.deliveries) == 1
    assert bot.deliveries[0][0] == 1
    assert "Купить лекарства" in bot.deliveries[0][1]
    assert reminders.get(1, saved.id).state == "sent"


@pytest.mark.asyncio
async def test_sender_does_not_deliver_completed_task(ctx):
    todo, _nav, access, reminders, _router = ctx
    task = add(todo)
    now = int(datetime.now(UTC).timestamp())
    draft = reminders.begin(1, task.id)
    reminders.propose(1, draft.token, datetime.fromtimestamp(now + 3600, UTC))
    saved = reminders.confirm(1, draft.token)
    assert saved
    claimed = reminders.claim_due(now=saved.remind_at_utc)
    todo.set_completed(1, task.id, completed=True)

    class FailIfSent:
        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("Cannot deliver completed task")

    await _send_one(FailIfSent(), reminders, access, claimed[0])
    assert reminders.get(1, saved.id).state in {"cancelled", "failed"}


def test_one_shot_voice_admission_per_reminder_draft(ctx):
    todo, nav, _access, reminders, _router = ctx
    task = add(todo)
    nav.activate(1, "todo")
    draft = reminders.begin(1, task.id, navigation_revision=nav.current(1)[1])
    assert reminders.claim_voice(1, draft.token)
    assert not reminders.claim_voice(1, draft.token)
    reminders.release_voice(1, draft.token)
    assert reminders.claim_voice(1, draft.token)
    later = reminders.begin(1, task.id, navigation_revision=nav.current(1)[1])
    assert later.token != draft.token
    reminders.release_voice(1, draft.token)
    assert not reminders.claim_voice(1, draft.token)
    assert reminders.claim_voice(1, later.token)
