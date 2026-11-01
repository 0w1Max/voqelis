"""Admin announcements V1: privacy, owner policy, durable queue and refusal paths."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from voqelis.access import AccessStore
from voqelis.navigation import NavigationStore
from voqelis.notifications.bot import create_notification_router
from voqelis.notifications.delivery import run_announcement_sender
from voqelis.notifications.store import AnnouncementStore


@pytest.fixture
def env(tmp_path: Path):
    access = AccessStore(
        tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}),
    )
    assert access.request(2, display_name="Tester", username="friend") == "created"
    assert access.decide(1, 2, approve=True)
    assert access.request(3, display_name="Tester 2", username="second") == "created"
    assert access.decide(1, 3, approve=True)
    store = AnnouncementStore(tmp_path / "admin_notifications.sqlite3", owner_id=1)
    navigation = NavigationStore(tmp_path / "navigation.sqlite3")
    try:
        yield store, access, navigation
    finally:
        navigation.close()
        store.close()
        access.close()


def draft(store, *, body="Новый модуль Факты дня доступен всем!"):
    announcement = store.create(1)
    assert announcement and announcement.state == "draft"
    result = store.submit_text(1, body)
    assert result and result.id == announcement.id
    assert result.revision == 1
    return result


def test_owner_only_and_secret_from_guest(env):
    store, _, _ = env
    assert store.create(2) is None
    item = draft(store)
    assert store.get(2, item.id) is None
    assert store.latest(2) == []
    assert store.confirmation(2, item.id, 1, item.token, [2]) is None
    assert not store.begin_edit(2, item.id)
    assert not store.pause(2, item.id)
    assert not store.cancel(2, item.id)


def test_edit_rotates_revision_and_stale_preview_fails(env):
    store, _, _ = env
    original = draft(store)
    assert store.begin_edit(1, original.id)
    updated = store.submit_text(1, "Теперь открыт список дел.")
    assert updated.revision == 2
    assert updated.token != original.token
    assert store.confirmation(
        1, original.id, original.revision, original.token, [2],
    ) is None
    assert store.confirmation(
        1, updated.id, updated.revision, updated.token, [2, 3],
    ) == 2
    assert store.confirmation(
        1, updated.id, updated.revision, updated.token, [2, 3],
    ) is None
    assert store.get(1, updated.id).state == "queued"
    assert store.begin_edit(1, updated.id) is False


def test_queue_snapshot_claim_once_and_delivery_state(env):
    store, _, _ = env
    item = draft(store)
    assert store.confirmation(1, item.id, 1, item.token, [2, 3, 3, 1]) == 2
    first = store.claim()
    assert first and first.recipient_id == 2
    assert store.claim() is not None
    assert store.claim() is None
    assert store.finish_delivery(first.announcement_id, 2, "sent")
    assert not store.finish_delivery(first.announcement_id, 2, "sent")
    assert store.finish_delivery(item.id, 3, "blocked")
    assert store.get(1, item.id).state == "completed"
    assert store.summary(1, item.id)["sent"] == 1
    assert store.summary(1, item.id)["blocked"] == 1


def test_pause_resume_and_cancel(env):
    store, _, _ = env
    item = draft(store)
    assert store.confirmation(1, item.id, 1, item.token, [2, 3]) == 2
    assert store.pause(1, item.id)
    assert store.claim() is None
    assert store.resume(1, item.id)
    assert store.claim() is not None
    assert store.cancel(1, item.id)
    assert store.claim() is None
    assert store.summary(1, item.id)["skipped"] == 1


def test_crash_recovery_no_repeated_sends(tmp_path: Path):
    path = tmp_path / "notifications.sqlite3"
    store = AnnouncementStore(path, owner_id=1)
    item = draft(store)
    store.confirmation(1, item.id, 1, item.token, [2])
    assert store.claim()
    store.close()
    reopened = AnnouncementStore(path, owner_id=1)
    try:
        assert reopened.recover() == 1
        assert reopened.claim() is None
        assert reopened.summary(1, item.id)["uncertain"] == 1
        assert reopened.get(1, item.id).state == "completed"
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_input_expiry_optout_and_bad_body(env):
    store, _, _ = env
    created = store.create(1, now=1000)
    assert store.awaiting(1, now=1001)
    assert not store.awaiting(1, now=2000)
    assert store.submit_text(1, "Too late", now=2000) is None
    assert store.begin_edit(1, created.id, now=2001)
    with pytest.raises(ValueError):
        store.submit_text(1, "x" * 3501, now=2002)
    with pytest.raises(ValueError):
        store.submit_text(1, "Bad\x00content", now=2002)
    assert store.submit_text(1, "Okay", now=2002)
    store.optout(2, enabled=True)
    assert store.opted_out(2)
    store.optout(2, enabled=False)
    assert not store.opted_out(2)


def test_cancel_draft_and_no_reactivate(env):
    store, _, _ = env
    item = store.create(1)
    assert store.cancel(1, item.id)
    assert not store.awaiting(1)
    assert not store.begin_edit(1, item.id)
    assert store.confirmation(1, item.id, 0, item.token, [2]) is None


def test_separate_sqlite_from_todo_and_access(env, tmp_path):
    store, access, _ = env
    item = draft(store)
    assert store.confirmation(1, item.id, item.revision, item.token, [2]) == 1
    assert access.is_allowed(2)
    assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (tmp_path / "admin_notifications.sqlite3").is_file()
    assert (tmp_path / "access.sqlite3").is_file()
    assert store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='todo_items'",
    ).fetchone() is None


class FakeMessage:
    def __init__(self, uid=1, text=""):
        self.from_user = SimpleNamespace(id=uid)
        self.chat = SimpleNamespace(type="private")
        self.text = text
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeCallback:
    def __init__(self, uid=1, data="", message=None):
        self.from_user = SimpleNamespace(id=uid)
        self.message = message or FakeMessage(uid)
        self.data = data
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_owner_preview_confirm_and_guest_denied(env):
    store, access, navigation = env
    router = create_notification_router(
        store=store, access=access, navigation=navigation, enabled=True,
    )
    cb = router.callback_query.handlers[0].callback
    owner = FakeMessage()
    guest = FakeMessage(2)
    await cb(FakeCallback(2, "news:new", guest), object())
    assert not store.latest(2)
    assert not store.latest(1)
    await cb(FakeCallback(1, "news:new", owner), object())
    item = store.latest(1)[0]
    assert store.awaiting(1)
    capture = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "on_text"
    )
    owner.text = "В Voqelis вышел новый модуль"
    await capture(owner)
    ready = store.get(1, item.id)
    assert ready.revision == 1
    markup = owner.replies[-1][1]["reply_markup"]
    data = next(
        button.callback_data for row in markup.inline_keyboard for button in row
        if "Подтвердить рассылку" in button.text
    )
    await cb(FakeCallback(1, data, owner), object())
    assert store.get(1, ready.id).state == "queued"
    await cb(FakeCallback(1, data, owner), object())
    assert store.summary(1, item.id)["pending"] == 2


@pytest.mark.asyncio
async def test_export_disabled_fails_closed(env):
    store, access, navigation = env
    router = create_notification_router(
        store=store, access=access, navigation=navigation, enabled=False,
    )
    cb = router.callback_query.handlers[0].callback
    item = draft(store)
    message = FakeMessage()
    await cb(FakeCallback(
        1, f"news:send:{item.id}:1:{item.token}:0000000000000000", message,
    ), object())
    assert store.get(1, item.id).state == "draft"
    assert store.summary(1, item.id)["pending"] == 0


@pytest.mark.asyncio
async def test_sender_rechecks_revoked_and_user_optout(env):
    store, access, _ = env
    item = draft(store)
    assert store.confirmation(1, item.id, 1, item.token, [2, 3]) == 2
    assert access.revoke(1, 2)
    store.optout(3, enabled=True)
    sent = []
    class FakeBot:
        async def send_message(self, uid, text, **kwargs):
            sent.append(uid)

    task = asyncio.create_task(
        run_announcement_sender(FakeBot(), store, access, interval=1.0),
    )
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sent == []
    assert store.summary(1, item.id)["skipped"] == 2


@pytest.mark.asyncio
async def test_sender_only_confirmed_private_recipients(env):
    store, access, _ = env
    item = draft(store)
    assert store.confirmation(1, item.id, 1, item.token, [2]) == 1
    messages = []
    event = asyncio.Event()
    class FakeBot:
        async def send_message(self, uid, text, **kwargs):
            messages.append((uid, text, kwargs))
            event.set()

    task = asyncio.create_task(
        run_announcement_sender(FakeBot(), store, access, interval=1.0),
    )
    try:
        await asyncio.wait_for(event.wait(), timeout=2.5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(messages) == 1
    assert messages[0][0] == 2
    assert "Новый модуль" in messages[0][1]
    assert "/notify_off" in messages[0][1]
    assert messages[0][2]["parse_mode"] is None
    assert store.summary(1, item.id)["sent"] == 1



def test_cancel_while_claimed_does_not_leave_pending(env):
    store, _, _ = env
    item = draft(store)
    assert store.confirmation(1, item.id, 1, item.token, [2]) == 1
    delivery = store.claim()
    assert delivery and delivery.recipient_id == 2
    assert store.cancel(1, item.id)
    assert store.release_claim(item.id, 2)
    assert store.delivery_state(item.id, 2) == "skipped"
    assert store.summary(1, item.id)["pending"] == 0
    assert store.claim() is None



@pytest.mark.asyncio
async def test_broadcast_rechecks_exact_previewed_recipients(env):
    store, access, navigation = env
    router = create_notification_router(
        store=store, access=access, navigation=navigation, enabled=True,
    )
    cb = router.callback_query.handlers[0].callback
    owner = FakeMessage()
    item = draft(store)
    await cb(FakeCallback(1, f"news:preview:{item.id}", owner), object())
    markup = owner.replies[-1][1]["reply_markup"]
    first_button = next(
        button.callback_data for row in markup.inline_keyboard for button in row
        if "Подтвердить рассылку" in button.text
    )
    # The count remains two, but the identities change.
    assert access.revoke(1, 2)
    assert access.request(4, display_name="New recipient", username="new") == "created"
    assert access.decide(1, 4, approve=True)
    assert len(access.allowed_ids() - {1}) == 2
    click = FakeCallback(1, first_button, owner)
    await cb(click, object())
    assert store.get(1, item.id).state == "draft"
    assert not store.summary(1, item.id)["pending"]
    assert any(
        "изменился" in str(message) for message, _ in click.answers
    )
    # The refreshed preview now binds to the new recipient set.
    markup = owner.replies[-1][1]["reply_markup"]
    next_button = next(
        button.callback_data for row in markup.inline_keyboard for button in row
        if "Подтвердить рассылку" in button.text
    )
    assert first_button != next_button
    await cb(FakeCallback(1, next_button, owner), object())
    assert store.get(1, item.id).state == "queued"
    assert store.delivery_state(item.id, 4) == "pending"
    assert store.delivery_state(item.id, 2) is None



def test_expired_announcement_confirmation_requires_fresh_preview(env):
    store, _, _ = env
    created = store.create(1, now=1000)
    saved = store.submit_text(1, "Важное обновление.", now=1001)
    assert saved and saved.id == created.id
    assert store.confirmation(
        1, saved.id, saved.revision, saved.token, [2], now=1902,
    ) is None
    assert store.get(1, saved.id).state == "draft"
    refreshed = store.refresh_preview(1, saved.id, now=1903)
    assert refreshed and refreshed.token != saved.token
    assert store.confirmation(
        1, saved.id, saved.revision, saved.token, [2], now=1904,
    ) is None
    assert store.confirmation(
        1, refreshed.id, refreshed.revision, refreshed.token, [2], now=1904,
    ) == 1


@pytest.mark.asyncio
async def test_old_broadcast_preview_button_is_rejected(env):
    store, access, navigation = env
    router = create_notification_router(
        store=store, access=access, navigation=navigation, enabled=True,
    )
    cb = router.callback_query.handlers[0].callback
    owner = FakeMessage()
    item = draft(store)
    await cb(FakeCallback(1, f"news:preview:{item.id}", owner), object())
    first_markup = owner.replies[-1][1]["reply_markup"]
    first = next(
        b.callback_data for row in first_markup.inline_keyboard for b in row
        if "Подтвердить рассылку" in b.text
    )
    await cb(FakeCallback(1, f"news:preview:{item.id}", owner), object())
    second_markup = owner.replies[-1][1]["reply_markup"]
    second = next(
        b.callback_data for row in second_markup.inline_keyboard for b in row
        if "Подтвердить рассылку" in b.text
    )
    assert first != second
    await cb(FakeCallback(1, first, owner), object())
    assert store.get(1, item.id).state == "draft"
    await cb(FakeCallback(1, second, owner), object())
    assert store.get(1, item.id).state == "queued"


def test_news_draft_does_not_swallow_todo_settings_button(env):
    # Current ToDo V2.2 reply-keyboard button must be handled by its own router.
    from voqelis.menu import TODO_SETTINGS
    from voqelis.notifications.bot import MENU_BUTTONS

    store, access, navigation = env
    item = store.create(1)
    assert item and store.awaiting(1)
    assert TODO_SETTINGS in MENU_BUTTONS
    assert store.get(1, item.id).state == "draft"
    assert access.is_allowed(1)
    assert navigation.current(1)[0] == "home"

def test_curated_suggestions_are_owner_only_idempotent_and_unsent(env, tmp_path):
    store, access, _ = env
    entries = (
        ("todo-reminders-v21", "В Voqelis доступен список дел с напоминаниями."),
        ("day-facts-v1", "В Voqelis доступен раздел Факты дня."),
    )
    assert store.seed_suggestions(2, entries) == 0
    assert store.seed_suggestions(1, entries, now=1000) == 2
    assert store.seed_suggestions(1, entries, now=1001) == 0
    proposals = store.suggested(1)
    assert len(proposals) == 2 and all(p.state == "draft" for p in proposals)
    assert store.suggested(2) == []
    assert all(store.summary(1, p.id)["pending"] == 0 for p in proposals)
    assert access.is_allowed(2)

    reopened = AnnouncementStore(tmp_path / "admin_notifications.sqlite3", owner_id=1)
    try:
        assert reopened.seed_suggestions(1, entries, now=2000) == 0
        assert len(reopened.suggested(1)) == 2
    finally:
        reopened.close()

    # Even cancelling a proposal does not recreate or send it on restart.
    assert store.cancel(1, proposals[0].id)
    assert store.seed_suggestions(1, entries, now=3000) == 0
    assert len(store.suggested(1)) == 1


@pytest.mark.asyncio
async def test_suggestions_admin_center_is_private_and_does_not_send(env):
    store, access, navigation = env
    store.seed_suggestions(1, (("day-facts-v1", "Доступен раздел Факты дня."),))
    router = create_notification_router(
        store=store, access=access, navigation=navigation, enabled=False,
    )
    on_callback = router.callback_query.handlers[0].callback
    guest = FakeCallback(2, "news:ideas", FakeMessage(2))
    await on_callback(guest, object())
    assert guest.answers and "прав" in guest.answers[-1][0].lower()
    owner_msg = FakeMessage(1)
    await on_callback(FakeCallback(1, "news:ideas", owner_msg), object())
    markup = owner_msg.replies[-1][1]["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data.startswith("news:preview:")
    proposal = store.suggested(1)[0]
    assert proposal.state == "draft"
    assert store.summary(1, proposal.id)["pending"] == 0

def test_announcement_input_does_not_intercept_other_module_buttons():
    from voqelis import menu
    from voqelis.notifications.bot import MENU_BUTTONS

    keyboards = (
        menu.main_keyboard(admin=True, reporter=True),
        menu.facts_keyboard(),
        menu.notes_keyboard(),
        menu.planner_section_keyboard(),
        menu.todo_keyboard(),
        menu.todo_capture_keyboard(),
    )
    for keyboard in keyboards:
        for row in keyboard.keyboard:
            for button in row:
                assert button.text in MENU_BUTTONS
    assert menu.TODO_LEGACY in MENU_BUTTONS
    assert menu.TRANSCRIBE_START in MENU_BUTTONS


