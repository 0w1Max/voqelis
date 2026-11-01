"""Restart-safe single-process sender of user-approved ToDo reminders."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..access import AccessStore
from .reminders import Reminder, ReminderRepository

logger = logging.getLogger(__name__)


def _reminder_markup(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнено", callback_data=f"td:rem:complete:{item_id}"),
        InlineKeyboardButton(text="📋 Открыть дело", callback_data=f"td:rem:open:{item_id}"),
    ]])


async def _send_one(
    bot: Bot, repository: ReminderRepository, access: AccessStore,
    reminder: Reminder,
) -> None:
    now = int(time.time())
    item = repository.todo.get(reminder.user_id, reminder.item_id)
    if item is None or item.completed_at is not None or not access.is_allowed(reminder.user_id):
        repository.finish_send(reminder.id, sent=False, now=now)
        return
    current = repository.get(reminder.user_id, reminder.id)
    if current is None:
        # A concurrently removed task must not leave a claimed reminder in flight.
        repository.finish_send(reminder.id, sent=False, now=now)
        return
    if current.state != "sending":
        return
    local = datetime.fromtimestamp(reminder.remind_at_utc, ZoneInfo(reminder.timezone))
    overdue = now - reminder.remind_at_utc > 60
    message = (
        "🔔 Напоминание о деле\n\n"
        f"{item.text}\n\n"
        f"⏰ {local:%d.%m.%Y, %H:%M}"
        + ("\n⚠ Доставлено с задержкой." if overdue else "")
    )
    try:
        await bot.send_message(
            reminder.user_id, message,
            reply_markup=_reminder_markup(reminder.item_id),
            parse_mode=None,
        )
    except TelegramRetryAfter as exc:
        # A rate-limit response unambiguously rejected this attempt.
        delay = max(1, int(exc.retry_after) + 1)
        if delay > 3600 or not repository.retry_after(
            reminder.id, now=now, delay=delay,
        ):
            # Do not retry earlier than Telegram's explicit RetryAfter.
            repository.finish_send(reminder.id, sent=False, now=now)
        logger.warning("TODO_REMINDER_RATE_LIMITED")
    except (TelegramForbiddenError, TelegramBadRequest):
        repository.finish_send(reminder.id, sent=False, now=now)
        logger.warning("TODO_REMINDER_UNDELIVERABLE")
    except TelegramAPIError:
        # With API/server errors we cannot rule out delivery after a network break.
        repository.finish_send(reminder.id, sent=False, uncertain=True, now=now)
        logger.warning("TODO_REMINDER_DELIVERY_UNCERTAIN")
    except Exception:  # noqa: BLE001
        repository.finish_send(reminder.id, sent=False, uncertain=True, now=now)
        logger.error("TODO_REMINDER_DELIVERY_UNKNOWN")
    else:
        repository.finish_send(reminder.id, sent=True, now=int(time.time()))


async def run_reminder_sender(
    bot: Bot, repository: ReminderRepository, access: AccessStore,
    *, interval: float = 15.0,
) -> None:
    """Recover ambiguous in-flight sends and process a bounded due batch."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    recovered = repository.recover_inflight()
    if recovered:
        logger.warning("TODO_REMINDER_INFLIGHT_UNCERTAIN count=%d", recovered)
    while True:
        try:
            # Claim only immediately before each send. A crash during delivery
            # must not mark the remaining, never-attempted batch as uncertain.
            for _ in range(10):
                due = repository.claim_due(now=int(time.time()), limit=1)
                if not due:
                    break
                await _send_one(bot, repository, access, due[0])
                # Conservative per-chat pacing; keeps bursts below Telegram limits.
                await asyncio.sleep(1.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("TODO_REMINDER_SCHEDULER_FAILURE")
        await asyncio.sleep(interval)
