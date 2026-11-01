"""Bounded delivery of daily active ToDo summaries in the user's configured timezone."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..access import AccessStore
from .digest import DueDigest, TodoDigestRepository, clock

logger = logging.getLogger(__name__)


def digest_message(due: DueDigest) -> str:
    day = date.fromisoformat(due.day)
    rows = [
        f"📋 Активные дела · {day:%d.%m.%Y}",
        f"⏰ Ежедневная сводка · {clock(due.minute)}",
        "",
        f"Всего активных дел: {due.count}",
        "",
    ]
    for index, item in enumerate(due.items, start=1):
        compact = " ".join(item.text.split())
        if len(compact) > 150:
            compact = compact[:149].rstrip() + "…"
        rows.append(f"{index}. {compact}")
    if due.count > len(due.items):
        rows.extend(("", f"И ещё {due.count - len(due.items)} дел в списке."))
    return "\n".join(rows)


def digest_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Открыть список дел", callback_data="td:digest:open"),
    ]])


async def send_due_digests(
    bot: Bot, repository: TodoDigestRepository, access: AccessStore,
    *, now: int | None = None,
) -> int:
    current = int(time.time()) if now is None else now
    batch = repository.claim_due(access.allowed_ids(), now=current, limit=5)
    for due in batch:
        # Never deliver to a user whose approval has been revoked.
        if not access.is_allowed(due.user_id):
            repository.finish(due, "skipped")
            continue
        prefs = repository.get(due.user_id)
        zone = repository.timezones.get(due.user_id).timezone
        if not prefs.enabled or due.minute not in prefs.times or zone != due.timezone:
            repository.finish(due, "skipped")
            continue
        # The task list can change between claiming and sending.
        items, count = repository.todo.list_items(due.user_id, completed=False)
        if not count:
            repository.finish(due, "skipped")
            continue
        refreshed = DueDigest(
            due.user_id, due.day, due.minute, due.timezone, tuple(items), count,
        )
        try:
            await bot.send_message(
                due.user_id, digest_message(refreshed),
                reply_markup=digest_markup(), parse_mode=None,
            )
        except TelegramRetryAfter:
            # Explicit rejection. No auto-repeat to avoid unexpected daily duplicates.
            repository.finish(due, "failed")
            logger.warning("TODO_DIGEST_RATE_LIMITED")
        except (TelegramForbiddenError, TelegramBadRequest):
            repository.finish(due, "failed")
            logger.warning("TODO_DIGEST_UNDELIVERABLE")
        except TelegramAPIError:
            repository.finish(due, "uncertain")
            logger.warning("TODO_DIGEST_DELIVERY_UNCERTAIN")
        except Exception:  # noqa: BLE001
            repository.finish(due, "uncertain")
            logger.exception("TODO_DIGEST_DELIVERY_UNKNOWN")
        else:
            repository.finish(due, "sent")
        await asyncio.sleep(1.1)
    return len(batch)
