"""Best-effort metadata-only telemetry. Never capture Telegram payloads."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType

from ..navigation import NavigationStore
from .store import OperationsStore

logger = logging.getLogger(__name__)


class OperationsTelemetry(BaseMiddleware):
    def __init__(
        self, store: OperationsStore, navigation: NavigationStore,
        allowed_user_ids: set[int],
    ) -> None:
        self.store = store
        self.navigation = navigation
        self.allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        uid = getattr(user, "id", None)
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = getattr(message, "chat", None)
        if not (
            isinstance(uid, int) and uid in self.allowed_user_ids
            and getattr(chat, "type", None) == ChatType.PRIVATE
        ):
            return await handler(event, data)

        section = self.navigation.current(uid)[0]
        operation = "callback" if getattr(event, "data", None) is not None else "message"
        started = time.monotonic()
        outcome = "handled"
        try:
            return await handler(event, data)
        except Exception:
            outcome = "failed"
            raise
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            try:
                self.store.record(uid, section, operation, outcome, elapsed)
            except Exception:
                # Telemetry must not block a user action or hide its exception.
                logger.exception("OPERATIONS_TELEMETRY_WRITE_FAILED")
