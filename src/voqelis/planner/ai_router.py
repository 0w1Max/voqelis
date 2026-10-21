"""Failover, cooldown and retry routing for Planner AI providers."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from .ai_contracts import PlannerAI
from .config import PlannerConfig
from .models import (
    PlannerAIError,
    PlannerAIInvalidResponse,
    PlannerAIProviderError,
    PlannerAIUnavailable,
    TaskDraft,
)

logger = logging.getLogger(__name__)


class AIProviderRouter:
    def __init__(
        self,
        *,
        primary: PlannerAI | None,
        fallback: PlannerAI | None,
        tertiary: PlannerAI | None = None,
    ):
        if primary is None and fallback is None and tertiary is None:
            raise PlannerAIUnavailable("No planner AI provider is configured")
        self.primary = primary
        self.fallback = fallback
        self.tertiary = tertiary
        self._blocked_until = [0.0, 0.0, 0.0]
        self._lock = asyncio.Lock()

    @staticmethod
    def _name(provider: PlannerAI | None) -> str:
        if provider is None:
            return "none"
        return str(getattr(provider, "provider_name", provider.__class__.__name__))

    @staticmethod
    def _cooldown_for_error(error: PlannerAIError) -> float:
        if isinstance(error, PlannerAIProviderError):
            if error.retry_after_seconds is not None:
                return max(1.0, error.retry_after_seconds)
            if error.status_code == 429:
                return 60.0
            if error.status_code in {401, 403}:
                return 300.0
            if error.status_code in {408, 409, 425} or (
                error.status_code is not None and error.status_code >= 500
            ):
                return 15.0
            if error.retryable:
                return 30.0
            return 60.0
        if isinstance(error, PlannerAIInvalidResponse):
            return 30.0
        return 60.0

    async def _available(self, index: int, provider: PlannerAI | None) -> bool:
        if provider is None:
            return False
        async with self._lock:
            return time.monotonic() >= self._blocked_until[index]

    async def _block(self, index: int, error: PlannerAIError) -> float:
        cooldown = self._cooldown_for_error(error)
        async with self._lock:
            self._blocked_until[index] = max(
                self._blocked_until[index],
                time.monotonic() + cooldown,
            )
        return cooldown

    async def _reset(self, index: int) -> None:
        async with self._lock:
            self._blocked_until[index] = 0.0

    async def _call(self, method: str, *args, **kwargs):
        providers = (self.primary, self.fallback, self.tertiary)
        last_error: PlannerAIError | None = None

        for index, provider in enumerate(providers):
            if provider is None or not await self._available(index, provider):
                continue

            try:
                result = await getattr(provider, method)(*args, **kwargs)
            except (
                PlannerAIProviderError,
                PlannerAIInvalidResponse,
                PlannerAIUnavailable,
            ) as exc:
                last_error = exc
                cooldown = await self._block(index, exc)
                logger.warning(
                    "PLANNER_AI_FAILOVER provider=%s next=%s reason=%s cooldown=%.1fs",
                    self._name(provider),
                    self._name(providers[index + 1]) if index + 1 < len(providers) else "local",
                    exc,
                    cooldown,
                )
                continue

            await self._reset(index)
            logger.info("PLANNER_AI_PROVIDER provider=%s", self._name(provider))
            return result

        if last_error is not None:
            raise last_error
        raise PlannerAIUnavailable("All planner AI providers are temporarily unavailable")

    async def extract_tasks(
        self,
        text: str,
        *,
        today: date,
        target_day: date,
        config: PlannerConfig,
    ) -> list[TaskDraft]:
        return await self._call(
            "extract_tasks",
            text,
            today=today,
            target_day=target_day,
            config=config,
        )

    async def extract_review(
        self,
        text: str,
        *,
        task_title: str,
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        return await self._call(
            "extract_review",
            text,
            task_title=task_title,
        )

    async def extract_full_review(
        self,
        text: str,
        *,
        items: list[dict],
    ) -> list[dict]:
        return await self._call(
            "extract_full_review",
            text,
            items=items,
        )
