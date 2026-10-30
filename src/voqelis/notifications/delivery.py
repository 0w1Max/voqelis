"""Rate-limited private-chat announcement delivery, never automatic without owner approval."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from ..access import AccessStore
from .store import AnnouncementStore

logger = logging.getLogger(__name__)


async def run_announcement_sender(
    bot: Bot, store: AnnouncementStore, access: AccessStore,
    *, interval: float = 2.0,
) -> None:
    """Consume one confirmed delivery at a time. A crash causes uncertain, never replay."""
    if interval < 1:
        raise ValueError("Interval must be >=1 second to limit broadcast rate")
    count = store.recover()
    if count:
        logger.warning("ANNOUNCEMENT_UNKNOWN_INFLIGHT count=%d", count)
    while True:
        try:
            delivery = store.claim()
            if delivery is not None:
                recipient = delivery.recipient_id
                active = store.get(access.admin_id, delivery.announcement_id)
                if active is None or active.state != "queued":
                    store.release_claim(delivery.announcement_id, recipient)
                elif not access.is_allowed(recipient) or store.opted_out(recipient):
                    store.finish_delivery(
                        delivery.announcement_id, recipient, "skipped",
                    )
                else:
                    try:
                        await bot.send_message(
                            recipient,
                            "📣 Новости Voqelis\n\n" + delivery.body
                            + "\n\nОтключить объявления: /notify_off",
                            parse_mode=None,
                        )
                    except TelegramRetryAfter as exc:
                        delay = max(1, int(exc.retry_after) + 1)
                        if delay > 3600 or not store.defer(
                            delivery.announcement_id, recipient, delay,
                        ):
                            store.finish_delivery(
                                delivery.announcement_id, recipient, "failed",
                            )
                        logger.warning("ANNOUNCEMENT_RATE_LIMITED")
                    except (TelegramForbiddenError, TelegramBadRequest):
                        store.finish_delivery(
                            delivery.announcement_id, recipient, "blocked",
                        )
                        logger.warning("ANNOUNCEMENT_UNDELIVERABLE")
                    except TelegramAPIError:
                        store.finish_delivery(
                            delivery.announcement_id, recipient, "uncertain",
                        )
                        logger.warning("ANNOUNCEMENT_DELIVERY_UNCERTAIN")
                    except Exception:  # noqa: BLE001
                        store.finish_delivery(
                            delivery.announcement_id, recipient, "uncertain",
                        )
                        logger.error("ANNOUNCEMENT_UNKNOWN_DELIVERY")
                    else:
                        store.finish_delivery(
                            delivery.announcement_id, recipient, "sent",
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ANNOUNCEMENT_SENDER_ERROR")
        await asyncio.sleep(interval)
