"""Authorization regression tests: owner-only approvals, privacy and persistence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

from voqelis.access import AccessStore, create_access_router
from voqelis.menu import ADMIN_PANEL, main_keyboard


@pytest.fixture
def store(tmp_path: Path):
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    yield access
    access.close()


def test_owner_bootstrap_and_private_sqlite_file(store):
    assert store.admin_id == 1
    assert store.path.stat().st_mode & 0o077 == 0
    assert 1 in store.allowed
    assert 2 not in store.allowed
    assert store.allowed_ids() == frozenset({1})


def test_guest_requires_real_request_and_owner_approval(store):
    assert not store.decide(1, 2, approve=True)  # forged callback, no request
    assert store.request(2, display_name="Тест", username="guest") == "created"
    assert store.request(2, display_name="Тест", username="guest") == "pending"
    assert len(store.pending()) == 1
    assert not store.decide(3, 2, approve=True)  # attacker cannot approve
    assert 2 not in store.allowed
    assert store.decide(1, 2, approve=True)
    assert 2 in store.allowed
    assert not store.decide(1, 2, approve=True)  # stale admin button
    assert store.request(2, display_name="Тест", username="guest") == "allowed"
    assert store.revoke(1, 2)
    assert 2 not in store.allowed
    assert not store.revoke(1, 2)
    assert not store.revoke(1, 1)  # bootstrap owner is not revocable
    assert store.request(2, display_name="Тест", username="guest") == "denied"


def test_rejected_requests_do_not_repeat_or_grant(store):
    assert store.request(2, display_name="Тест", username="guest") == "created"
    assert store.decide(1, 2, approve=False)
    assert store.request(2, display_name="Тест", username="guest") == "denied"
    assert not store.pending()
    assert 2 not in store.allowed
    # Later request (24-hour cooldown) is allowed but still needs fresh approval.
    with store.db:
        store.db.execute(
            "UPDATE access_requests SET updated_at = updated_at - 86401 "
            "WHERE user_id = 2"
        )
    assert store.request(2, display_name="Тест", username="guest") == "created"
    assert 2 not in store.allowed


def test_grants_persist_across_restart(tmp_path):
    path = tmp_path / "access.sqlite3"
    first = AccessStore(path, bootstrap_ids=frozenset({1}))
    assert first.request(2, display_name="Друг", username="guest") == "created"
    assert first.decide(1, 2, approve=True)
    first.close()

    second = AccessStore(path, bootstrap_ids=frozenset({1}))
    try:
        assert 2 in second.allowed
        assert 2 in second.allowed_ids()
        assert second.revoke(1, 2)
    finally:
        second.close()
    third = AccessStore(path, bootstrap_ids=frozenset({1}))
    try:
        assert 2 not in third.allowed
    finally:
        third.close()


def test_no_implicit_owner_when_bootstrap_is_ambiguous(tmp_path):
    store = AccessStore(
        tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1, 2})
    )
    try:
        assert store.admin_id is None
        assert not store.decide(1, 3, approve=True)
        assert not store.decide(2, 3, approve=True)
        assert 3 not in store.allowed
    finally:
        store.close()


def test_owner_menu_only_for_owner():
    regular = [
        btn.text for row in main_keyboard().keyboard for btn in row
    ]
    owner = [
        btn.text for row in main_keyboard(admin=True).keyboard for btn in row
    ]
    assert ADMIN_PANEL not in regular
    assert ADMIN_PANEL in owner
    assert all(button in owner for button in regular)


class FakeMessage:
    def __init__(self, user_id: int, text: str = "/start"):
        self.text = text
        self.chat = SimpleNamespace(type=ChatType.PRIVATE)
        self.from_user = SimpleNamespace(
            id=user_id,
            full_name=f"Имя {user_id}",
            username=f"user{user_id}",
        )
        self.responses = []

    async def answer(self, text, **kwargs):
        self.responses.append((text, kwargs))


class FakeCallback:
    def __init__(self, user_id: int, data: str, message: FakeMessage):
        self.from_user = SimpleNamespace(id=user_id, full_name="User")
        self.data = data
        self.message = message
        self.acks = []

    async def answer(self, text=None, **kwargs):
        self.acks.append((text, kwargs))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, user_id, text, **kwargs):
        self.sent.append((user_id, text, kwargs))


def _handler(router, name: str):
    return next(
        item.callback
        for item in router.message.handlers
        if item.callback.__name__ == name
    )


def _callback_handler(router):
    return next(item.callback for item in router.callback_query.handlers)


@pytest.mark.asyncio
async def test_full_request_approval_revocation_flow_with_callbacks(store):
    router = create_access_router(store)
    bot = FakeBot()
    on_start = _handler(router, "on_start")
    on_request = _handler(router, "on_request")
    on_admin = _handler(router, "on_admin")
    on_callback = _callback_handler(router)

    guest = FakeMessage(2)
    await on_start(guest)
    assert "по приглашениям" in guest.responses[-1][0]
    assert "Запросить доступ" in str(guest.responses[-1][1]["reply_markup"])

    await on_request(guest, bot)
    assert len(store.pending()) == 1
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 1
    assert "Telegram ID: 2" in bot.sent[0][1]
    await on_request(guest, bot)
    assert len(bot.sent) == 1  # repeated request does not spam owner

    forged = FakeCallback(2, "access:approve:2", FakeMessage(2))
    await on_callback(forged, bot)
    assert 2 not in store.allowed
    assert forged.acks[-1][1]["show_alert"] is True

    admin = FakeMessage(1, "/admin")
    await on_admin(admin)
    assert "Заявки:" in admin.responses[-1][0]
    assert "Telegram ID: 2" in admin.responses[-1][0]

    approved = FakeCallback(1, "access:approve:2", admin)
    await on_callback(approved, bot)
    assert 2 in store.allowed
    assert bot.sent[-1][0] == 2
    assert "одобрен" in bot.sent[-1][1]
    await on_start(guest)
    assert "Добро пожаловать" in guest.responses[-1][0]
    assert "Запросить доступ" not in str(guest.responses[-1][1]["reply_markup"])

    revoked = FakeCallback(1, "access:revoke:2", admin)
    await on_callback(revoked, bot)
    assert 2 not in store.allowed
    assert "отозван" in bot.sent[-1][1]
    await on_start(guest)
    assert "по приглашениям" in guest.responses[-1][0]


@pytest.mark.asyncio
async def test_non_owner_admin_command_does_not_disclose_requests(store):
    store.request(2, display_name="Личная информация", username="private")
    router = create_access_router(store)
    on_admin = _handler(router, "on_admin")
    intruder = FakeMessage(3, "/admin")
    await on_admin(intruder)
    assert not intruder.responses


@pytest.mark.asyncio
async def test_rejected_request_requires_new_approval(store):
    router = create_access_router(store)
    bot = FakeBot()
    guest = FakeMessage(2)
    await _handler(router, "on_request")(guest, bot)
    admin = FakeMessage(1)
    denied = FakeCallback(1, "access:reject:2", admin)
    await _callback_handler(router)(denied, bot)
    assert 2 not in store.allowed
    assert "отклонил" in bot.sent[-1][1]
    assert store.request(2, display_name="Имя", username="name") == "denied"
