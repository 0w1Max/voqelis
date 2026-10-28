"""Role-gated module navigation and real aiogram dispatch for Notes button."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User

from voqelis.day_facts.bot import create_day_facts_router
from voqelis.day_facts.service import DayFactsService
from voqelis.day_facts.store import DayFactsStore
from voqelis.menu import (
    ADMIN_PANEL,
    FACTS_START,
    NOTES_NEW,
    NOTES_START,
    PLANNER_START,
    TODO_START,
    TRANSCRIBE_START,
    main_keyboard,
)
from voqelis.navigation import NavigationStore, create_navigation_router
from voqelis.notes.bot import create_notes_router
from voqelis.notes.service import NotesService
from voqelis.notes.store import NotesStore
from voqelis.planner.bot import create_planner_router


def labels(keyboard):
    return [item.text for row in keyboard.keyboard for item in row]


def test_owner_sees_all_modules_guest_only_two():
    assert labels(main_keyboard()) == [FACTS_START, TODO_START]
    assert labels(main_keyboard(admin=True)) == [
        FACTS_START, TODO_START, NOTES_START, PLANNER_START,
        TRANSCRIBE_START, ADMIN_PANEL,
    ]


class FakeMessage:
    def __init__(self, user_id, text):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type="private")
        self.text = text
        self.replies = []

    async def answer(self, body, **kwargs):
        self.replies.append((body, kwargs))


def handler(router, name):
    return next(h.callback for h in router.message.handlers
                if h.callback.__name__ == name)


@pytest.mark.asyncio
async def test_guest_cannot_force_owner_sections_by_old_buttons(tmp_path):
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    try:
        router = create_navigation_router(
            navigation=nav, allowed_user_ids=frozenset({1, 2}),
            admin_user_id=1,
        )
        for text, name in (
            (NOTES_START, "on_notes"),
            (PLANNER_START, "on_planner"),
            (TRANSCRIBE_START, "on_transcribe"),
        ):
            guest = FakeMessage(2, text)
            await handler(router, name)(guest)
            assert guest.replies == []
            assert nav.current(2) == ("home", 0)

        for text, name in ((FACTS_START, "on_facts"), (TODO_START, "on_todo")):
            guest = FakeMessage(2, text)
            await handler(router, name)(guest)
            assert guest.replies
        assert nav.current(2)[0] == "facts"
    finally:
        nav.close()


@pytest.mark.asyncio
async def test_real_dispatcher_note_button_arms_owner_voice_capture(
    tmp_path, monkeypatch
):
    """Exercise real aiogram filters and router ordering, not direct handler calls."""
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    note_store = NotesStore(tmp_path / "notes.sqlite3")
    fact_store = DayFactsStore(tmp_path / "day_facts.sqlite3")
    service = NotesService(note_store)
    day_facts = DayFactsService(fact_store)
    planner_stub = SimpleNamespace(
        store=SimpleNamespace(session=lambda _id: None),
    )
    dp = Dispatcher()
    dp.include_router(create_navigation_router(
        navigation=nav, allowed_user_ids=frozenset({1, 2}),
        admin_user_id=1,
    ))
    dp.include_router(create_day_facts_router(
        service=day_facts, allowed_user_ids=frozenset({1, 2}),
        planner=planner_stub, notes=service, export_dir=tmp_path,
        navigation=nav,
    ))
    dp.include_router(create_notes_router(
        service=service, allowed_user_ids=frozenset({1}),
        planner=planner_stub, day_facts=day_facts, navigation=nav,
    ))

    replies = []

    async def patched_answer(self, body, **kwargs):
        replies.append((self.from_user.id, body, kwargs))

    monkeypatch.setattr(Message, "answer", patched_answer)

    async def dispatch(bot, user_id, text, seq):
        msg = Message(
            message_id=seq,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            text=text,
        )
        return await dp.feed_update(bot, Update(update_id=seq, message=msg))

    bot = Bot(token="123456:TESTTOKEN")
    try:
        await dispatch(bot, 1, NOTES_START, 1)
        assert nav.current(1)[0] == "notes"
        await dispatch(bot, 1, NOTES_NEW, 2)
        assert service.pending_voice(1)
        assert any("Отправь одно голосовое" in body for _, body, _ in replies)

        await dispatch(bot, 2, NOTES_START, 3)
        await dispatch(bot, 2, NOTES_NEW, 4)
        await dispatch(bot, 2, "/note", 5)
        assert not service.pending_voice(2)
        assert all(user_id != 2 for user_id, _, _ in replies)

        await dispatch(bot, 1, "/notes", 6)
        assert any("Пока нет заметок" in body for _, body, _ in replies)
    finally:
        await bot.session.close()
        fact_store.close()
        note_store.close()
        nav.close()
