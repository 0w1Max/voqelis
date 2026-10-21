"""Protocol for Planner's structured AI extraction."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from .config import PlannerConfig
from .models import TaskDraft


class PlannerAI(Protocol):
    async def extract_tasks(
        self,
        text: str,
        *,
        today: date,
        target_day: date,
        config: PlannerConfig,
    ) -> list[TaskDraft]:
        ...

    async def extract_review(
        self,
        text: str,
        *,
        task_title: str,
    ) -> tuple[str | None, tuple[str, ...], str | None]:
        ...

    async def extract_full_review(
        self,
        text: str,
        *,
        items: list[dict],
    ) -> list[dict]:
        ...
