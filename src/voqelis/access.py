"""Owner-approved Telegram access, isolated from Planner/Notes/Day Facts data."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from collections.abc import Set as AbstractSet
from pathlib import Path

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .menu import ADMIN_PANEL, main_keyboard

logger = logging.getLogger(__name__)

REQUEST_BUTTON = "🔐 Запросить доступ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_requests (
    user_id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    username TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected', 'revoked')),
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS access_grants (
    user_id INTEGER PRIMARY KEY,
    approved_at INTEGER NOT NULL
);
"""


def _safe_name(value: str, limit: int = 80) -> str:
    """Keep Telegram-controlled names readable without control characters."""
    return "".join(char for char in value if char.isprintable()).strip()[:limit]


class AllowedUsers(AbstractSet[int]):
    """Live allowlist: legacy IDs plus persisted owner approvals."""

    def __init__(self, store: AccessStore) -> None:
        self.store = store

    def __contains__(self, value: object) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and self.store.is_allowed(value)
        )

    def __iter__(self) -> Iterator[int]:
        return iter(self.store.allowed_ids())

    def __len__(self) -> int:
        return len(self.store.allowed_ids())


class AccessStore:
    def __init__(self, path: Path, *, bootstrap_ids: frozenset[int]) -> None:
        self.path = path
        self.bootstrap_ids = bootstrap_ids
        # Never grant admin rights merely because someone was approved via Telegram.
        self.admin_id = next(iter(bootstrap_ids)) if len(bootstrap_ids) == 1 else None
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RuntimeError("Access database must not be a symlink")
        old_umask = os.umask(0o077)
        try:
            self.db = sqlite3.connect(path, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            self.db.commit()
        finally:
            os.umask(old_umask)
        self.allowed = AllowedUsers(self)

    def close(self) -> None:
        self.db.close()

    def allowed_ids(self) -> frozenset[int]:
        rows = self.db.execute("SELECT user_id FROM access_grants").fetchall()
        return self.bootstrap_ids | frozenset(row[0] for row in rows)

    def is_allowed(self, user_id: int) -> bool:
        if user_id in self.bootstrap_ids:
            return True
        return self.db.execute(
            "SELECT 1 FROM access_grants WHERE user_id = ?", (user_id,)
        ).fetchone() is not None

    def request(self, user_id: int, *, display_name: str, username: str) -> str:
        """Returns created/pending/denied/allowed. Pending repeats never notify owner."""
        if user_id <= 0:
            raise ValueError("Invalid Telegram user")
        if self.is_allowed(user_id):
            return "allowed"
        now = int(time.time())
        existing = self.db.execute(
            "SELECT status, updated_at FROM access_requests WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "pending":
                return "pending"
            if (
                existing["status"] in {"rejected", "revoked"}
                and now - int(existing["updated_at"]) < 24 * 3600
            ):
                return "denied"
        with self.db:
            self.db.execute(
                "INSERT INTO access_requests "
                "(user_id, display_name, username, status, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "display_name=excluded.display_name, "
                "username=excluded.username, status='pending', "
                "updated_at=excluded.updated_at",
                (user_id, _safe_name(display_name), _safe_name(username, 40), now),
            )
        return "created"

    def decide(self, admin_id: int, user_id: int, *, approve: bool) -> bool:
        """Atomic one-time owner decision; no approval of arbitrary IDs."""
        if self.admin_id is None or admin_id != self.admin_id or user_id <= 0:
            return False
        if user_id in self.bootstrap_ids:
            return False
        with self.db:
            updated = self.db.execute(
                "UPDATE access_requests SET status = ?, updated_at = ? "
                "WHERE user_id = ? AND status = 'pending'",
                ("approved" if approve else "rejected", int(time.time()), user_id),
            )
            if updated.rowcount != 1:
                return False
            if approve:
                self.db.execute(
                    "INSERT INTO access_grants (user_id, approved_at) VALUES (?, ?)",
                    (user_id, int(time.time())),
                )
        return True

    def revoke(self, admin_id: int, user_id: int) -> bool:
        if self.admin_id is None or admin_id != self.admin_id or user_id in self.bootstrap_ids:
            return False
        with self.db:
            deleted = self.db.execute(
                "DELETE FROM access_grants WHERE user_id = ?", (user_id,)
            )
            if deleted.rowcount != 1:
                return False
            self.db.execute(
                "UPDATE access_requests SET status = 'revoked', updated_at = ? "
                "WHERE user_id = ?",
                (int(time.time()), user_id),
            )
        return True

    def pending(self, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT user_id, display_name, username FROM access_requests "
            "WHERE status = 'pending' ORDER BY updated_at DESC, user_id DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    def granted(self, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT g.user_id, COALESCE(r.display_name, '') AS display_name "
            "FROM access_grants g LEFT JOIN access_requests r USING(user_id) "
            "ORDER BY g.approved_at DESC, g.user_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    def counts(self) -> tuple[int, int]:
        pending = self.db.execute(
            "SELECT COUNT(*) FROM access_requests WHERE status = 'pending'"
        ).fetchone()[0]
        approved = self.db.execute(
            "SELECT COUNT(*) FROM access_grants"
        ).fetchone()[0]
        return int(pending), int(approved)


def _request_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=REQUEST_BUTTON, callback_data="access:ask"),
    ]])


def _admin_markup(store: AccessStore, page: int = 0) -> InlineKeyboardMarkup:
    rows = []
    for row in store.pending(offset=page * 10):
        user_id = row["user_id"]
        rows.append([
            InlineKeyboardButton(
                text=f"✅ {user_id}", callback_data=f"access:approve:{user_id}"
            ),
            InlineKeyboardButton(
                text=f"❌ {user_id}", callback_data=f"access:reject:{user_id}"
            ),
        ])
    for row in store.granted(offset=page * 10):
        user_id = row["user_id"]
        rows.append([InlineKeyboardButton(
            text=f"🚫 Отозвать {user_id}", callback_data=f"access:revoke:{user_id}"
        )])
    pending_count, granted_count = store.counts()
    if page > 0:
        rows.append([InlineKeyboardButton(
            text="⬅️ Назад", callback_data=f"access:panel:{page - 1}"
        )])
    if (page + 1) * 10 < max(pending_count, granted_count):
        rows.append([InlineKeyboardButton(
            text="➡️ Ещё", callback_data=f"access:panel:{page + 1}"
        )])
    rows.append([InlineKeyboardButton(
        text="🔄 Обновить", callback_data=f"access:panel:{page}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _admin_summary(store: AccessStore, page: int = 0) -> str:
    pending = store.pending(offset=page * 10)
    granted = store.granted(offset=page * 10)
    total_pending, total_granted = store.counts()
    lines = [
        "🛡 Админ-панель Voqelis",
        f"Страница {page + 1} · Заявки: {total_pending} · Одобрено: {total_granted}",
        "Заявки:",
    ]
    if not pending:
        lines.append("Нет ожидающих заявок.")
    for row in pending:
        lines.append(
            f"• {_safe_name(row['display_name']) or 'Пользователь'} "
            f"(@{_safe_name(row['username'], 40) or 'без username'}) "
            f"· Telegram ID: {row['user_id']}"
        )
    lines.append("\nОдобренные пользователи:")
    if not granted:
        lines.append("Пока нет.")
    for row in granted:
        lines.append(
            f"• {row['user_id']} · {_safe_name(row['display_name']) or 'Без имени'}"
        )
    lines.append("По 10 записей каждого раздела на страницу.")
    return "\n".join(lines)


def create_access_router(store: AccessStore) -> Router:
    router = Router(name="access")
    owner = store.admin_id

    async def request_access(user: object, message: Message, bot: object) -> None:
        user_id = user.id
        if owner is None:
            await message.answer("Приём заявок пока недоступен. Попробуй позднее.")
            return
        result = store.request(
            user_id,
            display_name=user.full_name or "Пользователь",
            username=user.username or "",
        )
        if result == "allowed":
            await message.answer("Доступ уже разрешён. Нажми /start.")
        elif result == "pending":
            await message.answer("⏳ Твоя заявка уже ожидает решения администратора.")
        elif result == "denied":
            await message.answer(
                "Сейчас доступ не предоставлен. Повторный запрос возможен через 24 часа."
            )
        else:
            await message.answer("✅ Заявка отправлена администратору. Ожидай решения.")
            try:
                await bot.send_message(
                    owner,
                    "🔐 Новая заявка на доступ к Voqelis\n"
                    f"Имя: {_safe_name(user.full_name or '')}\n"
                    f"Username: @{_safe_name(user.username or '', 40) or 'нет'}\n"
                    f"Telegram ID: {user_id}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="✅ Одобрить", callback_data=f"access:approve:{user_id}"
                        ),
                        InlineKeyboardButton(
                            text="❌ Отклонить", callback_data=f"access:reject:{user_id}"
                        ),
                    ]]),
                )
            except Exception:
                logger.exception("ACCESS_REQUEST_NOTIFICATION_FAILED")
                # Request was committed and stays visible in /admin.
                await message.answer(
                    "Заявка сохранена. Уведомление администратору могло не дойти, "
                    "но он увидит её через /admin."
                )

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if message.chat.type != ChatType.PRIVATE or message.from_user is None:
            return
        if message.from_user.id in store.allowed:
            await message.answer(
                "🎙️ Добро пожаловать в Voqelis!\n"
                "Отправь голосовое для расшифровки или используй «Факты дня».",
                reply_markup=main_keyboard(admin=message.from_user.id == owner),
            )
        else:
            await message.answer(
                "👋 Добро пожаловать в Voqelis.\n"
                "Этот бот работает по приглашениям. "
                "Нажми кнопку ниже, чтобы отправить запрос администратору.",
                reply_markup=_request_markup(),
            )

    @router.message(Command("access"))
    @router.message(F.text == REQUEST_BUTTON)
    async def on_request(message: Message, bot: object) -> None:
        if message.chat.type == ChatType.PRIVATE and message.from_user is not None:
            await request_access(message.from_user, message, bot)

    @router.message(Command("admin"))
    @router.message(F.text == ADMIN_PANEL)
    async def on_admin(message: Message) -> None:
        if (
            message.chat.type != ChatType.PRIVATE
            or message.from_user is None
            or message.from_user.id != owner
        ):
            return
        await message.answer(
            _admin_summary(store),
            reply_markup=_admin_markup(store),
        )

    @router.callback_query(F.data.startswith("access:"))
    async def on_callback(callback: CallbackQuery, bot: object) -> None:
        if (
            callback.message is None
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        data = callback.data or ""
        if data == "access:ask":
            await callback.answer()
            await request_access(callback.from_user, callback.message, bot)
            return
        if callback.from_user.id != owner:
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        if data == "access:panel" or data.startswith("access:panel:"):
            try:
                page = int(data.split(":")[2]) if data.count(":") == 2 else 0
            except ValueError:
                page = -1
            pending_count, granted_count = store.counts()
            if page < 0 or page > 100000 or page * 10 >= max(
                1, pending_count, granted_count
            ):
                await callback.answer("Страница недоступна.", show_alert=True)
                return
            await callback.answer()
            await callback.message.answer(
                _admin_summary(store, page), reply_markup=_admin_markup(store, page)
            )
            return
        parts = data.split(":")
        if len(parts) != 3 or parts[1] not in {"approve", "reject", "revoke"}:
            await callback.answer("Неизвестное действие.", show_alert=True)
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            await callback.answer("Неверный ID.", show_alert=True)
            return
        if parts[1] == "revoke":
            changed = store.revoke(callback.from_user.id, target_id)
        else:
            changed = store.decide(
                callback.from_user.id,
                target_id,
                approve=parts[1] == "approve",
            )
        if not changed:
            await callback.answer("Заявка уже обработана или недоступна.", show_alert=True)
            return
        await callback.answer("Изменение сохранено.")
        notification = {
            "approve": "✅ Доступ к Voqelis одобрен! Нажми /start.",
            "reject": "❌ Администратор отклонил запрос на доступ.",
            "revoke": "🔒 Доступ к Voqelis отозван администратором.",
        }[parts[1]]
        try:
            await bot.send_message(target_id, notification)
            result = "Уведомление доставлено."
        except Exception:
            logger.exception("ACCESS_DECISION_NOTIFICATION_FAILED")
            result = "Не удалось отправить уведомление, решение сохранено."
        await callback.message.answer(
            f"Пользователь {target_id}: {parts[1]}. {result}",
            reply_markup=_admin_markup(store),
        )

    return router
