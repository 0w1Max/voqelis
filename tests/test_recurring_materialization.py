from __future__ import annotations

import asyncio
from datetime import date

from voqelis.planner.config import PlannerConfig
from voqelis.planner.service import PlannerService
from voqelis.planner.store import PlannerStore
from voqelis.planner.models import TaskDraft


class FixedPlannerAI:
    def __init__(self, drafts: list[TaskDraft]):
        self.drafts = drafts

    async def extract_tasks(self, text, *, today, target_day, config):
        del text, today, target_day, config
        return self.drafts


def test_add_draft_materializes_recurring_plan_for_session_day_without_existing_plan(tmp_path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    config = PlannerConfig()
    day = date(2026, 10, 8)

    # This is a valid persisted planner state: the session/active day exists,
    # but the day has not yet been materialized.
    store.set_session(1, "planning", day, {})

    task = TaskDraft(
        "проверить почту",
        day,
        start_minute=20 * 60,
        duration_minutes=None,
        why="для работы",
        source_text="8 октября в 20:00 проверить почту для работы",
        source_excerpt="20:00 проверить почту для работы",
    )
    service = PlannerService(
        store,
        config,
        ai=FixedPlannerAI([task]),
    )

    replies = asyncio.run(
        service.add_from_text(
            1,
            "8 октября в 20:00 проверить почту для работы",
            date(2026, 10, 7),
        )
    )

    assert replies
    items = store.plan_items(1, day)

    recurring = [
        (item.start_minute, item.end_minute, item.title)
        for item in items
        if item.recurring_template_id is not None
    ]
    assert recurring == [
        (540, 600, "Проснуться + молитва + умыться + зарядка"),
        (600, 660, "Завтрак + душ"),
        (1440, 1500, "Подготовка ко сну + дневник успеха + благодарность + молитва"),
    ]

    ordinary = [
        (item.start_minute, item.end_minute, item.title, item.kind.value)
        for item in items
        if item.recurring_template_id is None
    ]
    assert ordinary == [(1200, 1260, "проверить почту", "ordinary")]

    store.close()
