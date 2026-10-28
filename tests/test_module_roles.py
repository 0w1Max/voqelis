"""Owner-only feature visibility and module command authorization."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
async def test_owner_notes_button_arms_capture_without_network(tmp_path):
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    note_store = NotesStore(tmp_path / "notes.sqlite3")
    try:
        notes = NotesService(note_store)
        router = create_notes_router(
            service=notes, allowed_user_ids=frozenset({1}),
            navigation=nav,
        )
        nav.activate(1, "notes")
        owner = FakeMessage(1, NOTES_NEW)
        await handler(router, "on_note")(owner)
        assert notes.pending_voice(1)
        assert "Отправь одно голосовое" in owner.replies[0][0]

        guest = FakeMessage(2, "/note")
        await handler(router, "on_note")(guest)
        assert guest.replies == []
        assert not notes.pending_voice(2)
    finally:
        nav.close()
        note_store.close()
