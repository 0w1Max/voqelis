"""End-to-end aiogram Dispatcher checks for owner announcement authorization.

These tests never start polling or contact Telegram; Bot API requests are
intercepted at the Bot.__call__ boundary.
"""

from __future__ import annotations

import time

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update

from voqelis.access import AccessStore, create_access_router
from voqelis.navigation import NavigationStore, create_navigation_router
from voqelis.notifications.bot import create_notification_router
from voqelis.notifications.store import AnnouncementStore


def _user(uid: int, *, bot: bool = False) -> dict:
    return {"id": uid, "is_bot": bot, "first_name": "Mock User"}


def _chat(uid: int) -> dict:
    return {"id": uid, "type": "private", "first_name": "Mock User"}


def _message(uid: int, message_id: int, text: str) -> Update:
    return Update.model_validate({
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": _chat(uid),
            "from": _user(uid),
            "text": text,
        },
    })


def _callback(uid: int, event_id: int, data: str, bot_uid: int) -> Update:
    return Update.model_validate({
        "update_id": event_id,
        "callback_query": {
            "id": f"mock-{event_id}",
            "from": _user(uid),
            "chat_instance": "test-session",
            "message": {
                "message_id": 5,
                "date": int(time.time()),
                "chat": _chat(uid),
                "from": _user(bot_uid, bot=True),
                "text": "📣 Уведомления пользователям",
            },
            "data": data,
        },
    })


@pytest.fixture
def dispatcher_env(tmp_path, monkeypatch):
    access = AccessStore(
        tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}),
    )
    assert access.request(2, display_name="Tester", username="guest") == "created"
    assert access.decide(1, 2, approve=True)
    navigation = NavigationStore(tmp_path / "navigation.sqlite3")
    store = AnnouncementStore(tmp_path / "admin_notifications.sqlite3", owner_id=1)
    calls = []

    async def capture_api(self, method, request_timeout=None):
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", capture_api)

    def build(*, enabled: bool):
        dp = Dispatcher()
        # Match the production router precedence for these modules.
        dp.include_router(create_access_router(access, navigation=navigation))
        dp.include_router(create_navigation_router(
            navigation=navigation, allowed_user_ids=access.allowed,
            admin_user_id=access.admin_id,
        ))
        dp.include_router(create_notification_router(
            store=store, access=access, navigation=navigation, enabled=enabled,
        ))
        return dp

    try:
        yield build, store, access, calls
    finally:
        store.close()
        navigation.close()
        access.close()


@pytest.mark.asyncio
async def test_dispatcher_owner_draft_does_not_capture_menu_or_guest(
    dispatcher_env,
):
    build, store, _access, calls = dispatcher_env
    dp = build(enabled=False)
    bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        await dp.feed_update(bot, _message(2, 10, "/announce"))
        assert store.latest(2) == []
        assert store.latest(1) == []

        await dp.feed_update(bot, _message(1, 11, "/announce"))
        assert any(
            isinstance(c, SendMessage) and "Уведомления пользователям" in c.text
            for c in calls
        )
        await dp.feed_update(bot, _callback(2, 12, "news:new", bot.id))
        assert store.latest(1) == []

        await dp.feed_update(bot, _callback(1, 13, "news:new", bot.id))
        item = store.latest(1)[0]
        assert item.state == "draft" and store.awaiting(1)

        # This is a real reply-keyboard label of a different module.
        await dp.feed_update(bot, _message(1, 14, "📋 Активные дела"))
        assert store.get(1, item.id).body == ""
        assert store.awaiting(1)

        # Another approved user cannot consume the owner's draft.
        await dp.feed_update(bot, _message(2, 15, "Поддельное объявление"))
        assert store.get(1, item.id).body == ""

        await dp.feed_update(bot, _message(1, 16, "Доступна новая функция."))
        saved = store.get(1, item.id)
        assert saved.body == "Доступна новая функция."
        assert saved.revision == 1
        assert not store.awaiting(1)
        assert store.summary(1, item.id)["pending"] == 0

        # Even a forged enabled-looking confirmation must fail closed.
        await dp.feed_update(bot, _callback(
            1, 17,
            f"news:send:{item.id}:{saved.revision}:{saved.token}:0000000000000000",
            bot.id,
        ))
        assert store.get(1, item.id).state == "draft"
        assert store.summary(1, item.id)["pending"] == 0
        assert any(isinstance(c, AnswerCallbackQuery) for c in calls)
        # Every outgoing message was a response to the owner's UI;
        # there was no broadcast to the approved user.
        assert not any(
            isinstance(c, SendMessage) and c.chat_id == 2 for c in calls
        )
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_dispatcher_confirmation_is_owner_only_and_one_shot(
    dispatcher_env,
):
    build, store, _access, calls = dispatcher_env
    dp = build(enabled=True)
    bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        await dp.feed_update(bot, _callback(1, 20, "news:new", bot.id))
        item = store.latest(1)[0]
        await dp.feed_update(bot, _message(1, 21, "Важное обновление Voqelis."))
        preview = next(
            c for c in reversed(calls)
            if isinstance(c, SendMessage)
            and c.reply_markup is not None
            and any(
                b.callback_data.startswith("news:send:")
                for row in c.reply_markup.inline_keyboard for b in row
            )
        )
        confirm = next(
            b.callback_data for row in preview.reply_markup.inline_keyboard
            for b in row if b.callback_data.startswith("news:send:")
        )
        assert len(confirm.encode("utf-8")) <= 64
        await dp.feed_update(bot, _callback(2, 22, confirm, bot.id))
        assert store.get(1, item.id).state == "draft"
        await dp.feed_update(bot, _callback(1, 23, confirm, bot.id))
        assert store.get(1, item.id).state == "queued"
        assert store.summary(1, item.id)["pending"] == 1
        await dp.feed_update(bot, _callback(1, 24, confirm, bot.id))
        assert store.summary(1, item.id)["pending"] == 1
        assert not any(
            isinstance(c, SendMessage) and c.chat_id == 2 for c in calls
        )
    finally:
        await bot.session.close()
