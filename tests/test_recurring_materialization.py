from __future__ import annotations

import asyncio
from datetime import date

from voqelis.planner.config import PlannerConfig
from voqelis.planner.models import TaskDraft
from voqelis.planner.service import PlannerService
from voqelis.planner.store import PlannerStore


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
            "завтра в 20:00 проверить почту для работы",
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

def test_deleted_recurring_item_stays_excluded_after_re_materialization(tmp_path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    config = PlannerConfig()
    day = date(2026, 10, 8)
    service = PlannerService(store, config)

    store.set_active_plan_day(1, day)
    initial = store.ensure_daily_plan(1, day, config)
    deleted = next(item for item in initial if item.recurring_template_id == 14)

    asyncio.run(service.start_delete_item(1, day))
    index = next(
        index
        for index, item in enumerate(store.plan_items(1, day), 1)
        if item.id == deleted.id
    )
    asyncio.run(service.handle_text(1, str(index), day))

    after = asyncio.run(service.show_plan(1, day))
    assert "Проснуться + молитва + умыться + зарядка" not in after

    remaining = store.plan_items(1, day)
    assert not any(item.recurring_template_id == 14 for item in remaining)
    assert {item.recurring_template_id for item in remaining if item.recurring_template_id is not None} == {15, 17}
    assert store.db.execute(
        "SELECT 1 FROM recurring_exclusions WHERE user_id=? AND day=? AND recurring_template_id=?",
        (1, day.isoformat(), 14),
    ).fetchone() is not None

    store.close()


def test_clear_ordinary_items_does_not_remove_recurring_items(tmp_path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    config = PlannerConfig()
    day = date(2026, 10, 8)
    service = PlannerService(store, config)

    store.add_item(
        PlanItem(
            0,
            1,
            day,
            "Обычная задача",
            None,
            12 * 60,
            13 * 60,
            TaskKind.ORDINARY,
        )
    )
    store.set_active_plan_day(1, day)
    store.ensure_daily_plan(1, day, config)

    asyncio.run(service.start_clear_plan(1, day))
    result = asyncio.run(service.handle_callback(1, "pl:clear:ordinary", day))

    assert "ежедневные задачи сохранены" in result[0]
    assert [item.recurring_template_id for item in store.plan_items(1, day) if item.kind == TaskKind.RECURRING] == [14, 15, 17]
    assert not any(item.title == "Обычная задача" for item in store.plan_items(1, day))

    store.close()


def test_clear_all_prevents_recurring_materialization_on_reopen(tmp_path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    config = PlannerConfig()
    day = date(2026, 10, 8)
    service = PlannerService(store, config)

    store.set_active_plan_day(1, day)
    store.ensure_daily_plan(1, day, config)

    asyncio.run(service.start_clear_plan(1, day))
    result = asyncio.run(service.handle_callback(1, "pl:clear:all", day))

    assert "вместе с ежедневными задачами" in result[0]
    assert store.plan_items(1, day) == []
    assert store.is_day_cleared(1, day)

    reopened = asyncio.run(service.show_plan(1, day))
    assert "нет задач" in reopened.lower()
    assert store.plan_items(1, day) == []

    store.close()

