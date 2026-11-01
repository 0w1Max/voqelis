"""Persistent per-user Telegram module navigation.

Navigation is presentation intent, never an authorization grant. The access
allowlist must still be checked by every router before exposing user data.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from .menu import (
    FACTS_START,
    HOME,
    NOTES_START,
    PLANNER_START,
    TODO_LEGACY,
    TODO_START,
    TRANSCRIBE_START,
    facts_keyboard,
    main_keyboard,
    notes_keyboard,
    planner_section_keyboard,
    todo_keyboard,
)

if TYPE_CHECKING:
    from .operations.store import OperationsStore


SECTIONS = frozenset({"home", "facts", "facts_capture", "notes", "planner", "todo", "todo_capture"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS navigation_state (
    user_id INTEGER PRIMARY KEY,
    section TEXT NOT NULL CHECK(section IN ('home','facts','facts_capture','notes','planner','transcribe','todo','todo_capture')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
);
"""


class NavigationStore:
    """A small SQLite store; changing section increments a queue-safety revision."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Navigation database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # SQLite cannot ALTER a CHECK constraint. Transactionally preserve
        # user IDs, active sections and navigation revisions.
        ddl = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='navigation_state'"
        ).fetchone()[0]
        if "'todo_capture'" not in ddl:
            with self.db:
                self.db.execute(
                    "CREATE TABLE navigation_state_v2 ("
                    "user_id INTEGER PRIMARY KEY, "
                    "section TEXT NOT NULL CHECK(section IN "
                    "('home','facts','facts_capture','notes','planner',"
                    "'transcribe','todo','todo_capture')), "
                    "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0))"
                )
                self.db.execute(
                    "INSERT INTO navigation_state_v2(user_id, section, revision) "
                    "SELECT user_id, section, revision FROM navigation_state"
                )
                self.db.execute("DROP TABLE navigation_state")
                self.db.execute(
                    "ALTER TABLE navigation_state_v2 RENAME TO navigation_state"
                )
        # Legacy "transcribe" was a dedicated module. Bring persisted users
        # home without touching any module draft or other state.
        # Keep its old CHECK value in SCHEMA for existing SQLite databases.
        with self.db:
            self.db.execute(
                "UPDATE navigation_state SET section='home', revision=revision+1 "
                "WHERE section='transcribe'"
            )

    def close(self) -> None:
        self.db.close()

    def current(self, user_id: int) -> tuple[str, int]:
        row = self.db.execute(
            "SELECT section, revision FROM navigation_state WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return (str(row[0]), int(row[1])) if row else ("home", 0)

    def activate(self, user_id: int, section: str) -> tuple[str, int]:
        if user_id <= 0 or section not in SECTIONS:
            raise ValueError("Invalid navigation target")
        with self.db:
            self.db.execute(
                "INSERT INTO navigation_state (user_id, section, revision) "
                "VALUES (?, ?, 1) ON CONFLICT(user_id) DO UPDATE "
                "SET section=excluded.section, "
                "revision=CASE WHEN section=excluded.section "
                "THEN revision ELSE revision+1 END",
                (user_id, section),
            )
        return self.current(user_id)


def create_navigation_router(
    *, navigation: NavigationStore, allowed_user_ids: frozenset[int],
    admin_user_id: int | None,
    operations: OperationsStore | None = None,
) -> Router:
    router = Router(name="navigation")

    def permitted(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_user_ids
        )

    def owner_permitted(message: Message) -> bool:
        return (
            permitted(message)
            and admin_user_id is not None
            and message.from_user.id == admin_user_id
        )

    @router.message(Command("menu"))
    @router.message(F.text == HOME)
    async def on_home(message: Message) -> None:
        if not permitted(message):
            return
        user_id = message.from_user.id
        navigation.activate(user_id, "home")
        await message.answer(
            "🏠 Главное меню Voqelis. Отправь голосовое или аудиофайл "
            "для обычной расшифровки либо выбери модуль.",
            reply_markup=main_keyboard(
                admin=user_id == admin_user_id,
                reporter=operations is not None and operations.eligible(
                    user_id, approved=user_id in allowed_user_ids,
                ),
            ),
        )

    @router.message(F.text == FACTS_START)
    async def on_facts(message: Message) -> None:
        if not permitted(message):
            return
        navigation.activate(message.from_user.id, "facts")
        await message.answer(
            "📘 Факты дня\nВыбери действие. Открытие меню не создаёт запись.",
            reply_markup=facts_keyboard(),
        )

    @router.message(F.text == NOTES_START)
    async def on_notes(message: Message) -> None:
        if not owner_permitted(message):
            return
        navigation.activate(message.from_user.id, "notes")
        await message.answer(
            "📝 Заметки\nВыбери действие. Сохранённые заметки не меняются.",
            reply_markup=notes_keyboard(),
        )

    @router.message(F.text == PLANNER_START)
    async def on_planner(message: Message) -> None:
        if not owner_permitted(message):
            return
        navigation.activate(message.from_user.id, "planner")
        await message.answer(
            "📅 Планировщик\nВыбери действие. Текущая сессия не изменяется.",
            reply_markup=planner_section_keyboard(),
        )

    @router.message(F.text == TRANSCRIBE_START)
    async def on_transcribe(message: Message) -> None:
        """An old Telegram keyboard button returns to home transcription."""
        if not permitted(message):
            return
        user_id = message.from_user.id
        navigation.activate(user_id, "home")
        await message.answer(
            "🎙 Отдельный режим расшифровки больше не нужен. "
            "Пришли голосовое или аудиофайл прямо из главного меню.",
            reply_markup=main_keyboard(
                admin=user_id == admin_user_id,
                reporter=operations is not None and operations.eligible(
                    user_id, approved=user_id in allowed_user_ids,
                ),
            ),
        )

    @router.message(F.text == TODO_START)
    @router.message(F.text == TODO_LEGACY)
    async def on_todo(message: Message) -> None:
        if not permitted(message):
            return
        navigation.activate(message.from_user.id, "todo")
        await message.answer(
            "✅ Список дел\nДобавляй дела голосом или текстом, "
            "отмечай выполненные и настраивай напоминания.",
            reply_markup=todo_keyboard(),
        )

    return router
