import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from voqelis.planner.config import PlannerConfig, RecurringTemplateSpec
from voqelis.planner.export import build_docx, build_pdf
from voqelis.planner.intent_validation import (
    IntentValidationError,
    explicit_constraints,
    validate_task_intents,
)
from voqelis.planner.models import (
    Conflict,
    PlanItem,
    ScheduleValidationError,
    TaskDraft,
    TaskKind,
)
from voqelis.planner.scheduler import Scheduler
from voqelis.planner.service import PlannerService
from voqelis.planner.store import PlannerStore


class FixedPlannerAI:
    def __init__(self, drafts):
        self.drafts = drafts

    async def extract_tasks(self, text, *, today, target_day, config):
        del text, today, target_day, config
        return list(self.drafts)

def test_explicit_constraints_capture_clock_range_period_and_relation():
    config = PlannerConfig(recurring_templates=())
    facts = explicit_constraints(
        "завтра после обеда в 16.00 читать книгу",
        today=date(2026, 10, 4),
        config=config,
    )
    assert facts.day == date(2026, 10, 5)
    assert facts.start_minute == 16 * 60
    assert facts.relation == "after"
    assert facts.anchor == "lunch"


def test_intent_validation_rejects_ai_time_not_supported_by_source():
    config = PlannerConfig(recurring_templates=())
    draft = TaskDraft(
        "ужинать",
        date(2026, 10, 5),
        start_minute=14 * 60,
        source_text="завтра в 21.00 ужинать",
        source_excerpt="завтра в 21.00 ужинать",
    )
    with pytest.raises(IntentValidationError):
        validate_task_intents(
            [draft],
            source_text=draft.source_text,
            today=date(2026, 10, 4),
            config=config,
        )


def test_intent_validation_accepts_ai_result_with_explicit_source_evidence():
    config = PlannerConfig(recurring_templates=())
    draft = TaskDraft(
        "ужинать",
        date(2026, 10, 5),
        start_minute=21 * 60,
        duration_minutes=60,
        source_text="завтра в 21.00 ужинать",
        source_excerpt="завтра в 21.00 ужинать",
    )
    validate_task_intents(
        [draft],
        source_text=draft.source_text,
        today=date(2026, 10, 4),
        config=config,
    )


def test_intent_validation_requires_distinct_source_for_multiple_tasks():
    config = PlannerConfig(recurring_templates=())
    text = "завтра утром зарядка и вечером прогулка"
    drafts = [
        TaskDraft("зарядка", date(2026, 10, 5), source_text=text, source_excerpt="завтра утром зарядка"),
        TaskDraft("прогулка", date(2026, 10, 5), source_text=text, source_excerpt="завтра вечером прогулка"),
    ]
    validate_task_intents(drafts, source_text=text, today=date(2026, 10, 4), config=config)


def test_planner_refuses_semantic_extraction_without_ai(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig(recurring_templates=()), ai=None)
    replies = asyncio.run(service.add_from_text(1, "завтра в 21.00 ужинать", date(2026, 10, 4)))
    assert "Не удалось разобрать задачу" in replies[0]
    assert store.plan_items(1, date(2026, 10, 5)) == []
    store.close()




def test_new_recurring_materialization_has_no_review_status(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    config = PlannerConfig()
    first_day = date(2026, 10, 2)
    second_day = first_day + timedelta(days=1)

    first_items = store.ensure_daily_plan(1, first_day, config)
    store.save_review(first_items[0].id, "+", "сделал", (), None)

    second_items = store.ensure_daily_plan(1, second_day, config)

    assert len(second_items) == len(config.recurring_templates)
    assert all(item.status is None for item in store.reviews(1, second_day))
    store.close()


def test_active_day_rolls_forward_and_materializes_new_recurring_plan(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())
    store.set_active_plan_day(1, date(2026, 9, 30))

    active = asyncio.run(service.resolve_active_day(1, date(2026, 10, 1)))

    assert active == date(2026, 10, 2)
    assert store.active_plan_day(1) == date(2026, 10, 2)
    assert len(store.plan_items(1, date(2026, 10, 2))) == 3
    store.close()


def test_active_day_today_is_preserved_for_review(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())
    today = date(2026, 10, 1)
    store.set_active_plan_day(1, today)

    active = asyncio.run(service.resolve_active_day(1, today))

    assert active == today
    assert store.active_plan_day(1) == today
    store.close()


def test_planner_markup_is_absent_without_active_session(tmp_path: Path):
    from voqelis.planner.bot import planner_markup_for_state

    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())

    assert planner_markup_for_state(service, 1) is None
    store.close()


def test_only_three_core_recurring_tasks_are_created(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    items = store.ensure_daily_plan(1, date(2026, 9, 23))
    assert len(items) == 3
    assert all(item.kind == TaskKind.RECURRING for item in items)
    assert [(item.start_minute, item.end_minute) for item in items] == [
        (9 * 60, 10 * 60),
        (10 * 60, 11 * 60),
        (24 * 60, 25 * 60),
    ]
    store.close()


def test_legacy_thirteen_recurring_tasks_are_migrated(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    legacy = tuple(
        RecurringTemplateSpec(title, "why", start, 60)
        for title, start in (
            ("Проснуться + молитва + умыться + зарядка", 540),
            ("Завтрак + душ", 600),
            ("Послушать спикерскую + заниматься проектами", 660),
            ("Читать книгу", 780),
            ("Переделать резюме", 840),
            ("Обед + отдых", 900),
            ("Делать домашку по психотерапии", 960),
            ("Собираться на группу", 1020),
            ("Дорога на группу + собрание + прогулка", 1080),
            ("Дорога домой + ужин", 1260),
            ("Делать домашку по шагам", 1320),
            ("Читать книгу", 1380),
            ("Подготовка ко сну + дневник успеха + молитва + благодарности за день", 1440),
        )
    )
    legacy_config = PlannerConfig(recurring_templates=legacy)
    first = store.ensure_daily_plan(1, date(2026, 9, 23), legacy_config)
    assert len(first) == 13

    current = PlannerConfig()
    migrated = store.ensure_daily_plan(1, date(2026, 9, 24), current)
    assert len(migrated) == 3
    assert len(store.recurring(1)) == 3
    old_day_items = store.plan_items(1, date(2026, 9, 23))
    assert len(old_day_items) == 13
    assert all(item.kind == TaskKind.RECURRING for item in old_day_items)
    store.close()


def test_legacy_sleep_kd_variant_is_migrated_to_canonical_template(tmp_path: Path):
    db_path = tmp_path / "planner.sqlite3"
    store = PlannerStore(db_path)

    old = store.db.execute(
        "INSERT INTO recurring_templates "
        "(user_id, title, why, start_minute, duration_minutes, recurrence, recurrence_days, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            1,
            "Подготовка ко сну + дневник успеха + благодарность + молитва (КД)",
            "why",
            24 * 60,
            60,
            "daily",
            "[]",
        ),
    )
    canonical = store.db.execute(
        "INSERT INTO recurring_templates "
        "(user_id, title, why, start_minute, duration_minutes, recurrence, recurrence_days, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            1,
            "Подготовка ко сну + дневник успеха + благодарность + молитва",
            "why",
            24 * 60,
            60,
            "daily",
            "[]",
        ),
    )
    old_id = int(old.lastrowid)
    canonical_id = int(canonical.lastrowid)
    store.db.commit()
    store.close()

    store = PlannerStore(db_path)
    rows = store.db.execute(
        "SELECT id, title, active FROM recurring_templates "
        "WHERE user_id=? AND start_minute=? ORDER BY id",
        (1, 24 * 60),
    ).fetchall()

    assert [(int(row["id"]), row["title"], int(row["active"])) for row in rows] == [
        (
            old_id,
            "Подготовка ко сну + дневник успеха + благодарность + молитва",
            0,
        ),
        (
            canonical_id,
            "Подготовка ко сну + дневник успеха + благодарность + молитва",
            1,
        ),
    ]
    store.close()


def test_delete_recurring_item_does_not_reappear_on_same_day(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    recurring = items[0]

    deleted = store.delete_plan_item(1, recurring.id)
    assert deleted.id == recurring.id
    assert not any(item.id == recurring.id for item in store.plan_items(1, day))

    again = store.ensure_daily_plan(1, day)
    assert not any(item.recurring_template_id == recurring.recurring_template_id for item in again)
    store.close()


def test_clear_day_explicitly_removes_all_rows_and_suppresses_recurring_materialization(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    assert items

    removed = store.clear_day(1, day, include_recurring=True)
    assert removed == len(items)
    assert store.plan_items(1, day) == []

    again = store.ensure_daily_plan(1, day)
    assert again == []
    store.close()


def test_plan_item_fields_can_be_edited(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    items = store.ensure_daily_plan(1, date(2026, 9, 23))
    edited = store.update_plan_item(
        items[0].id,
        title="Новое дело",
        why="Новая причина",
    )
    assert edited.title == "Новое дело"
    assert edited.why == "Новая причина"
    store.close()


def test_conflict_is_not_auto_rescheduled(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("делать резюме", date(2026, 9, 23), start_minute=10 * 60)
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.conflicts[0].kind == TaskKind.RECURRING
    store.close()


def test_exact_range_can_be_scheduled_when_free(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("заниматься проектом", date(2026, 9, 23), start_minute=12 * 60, end_minute=13 * 60)
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)
    assert result.start_minute == 12 * 60
    assert result.end_minute == 13 * 60
    store.close()


def test_non_hour_duration_is_rounded_up_to_hourly_slots(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())

    ninety = TaskDraft(
        "Задача 90 минут",
        day,
        start_minute=19 * 60,
        duration_minutes=90,
    )
    result = scheduler.schedule(1, ninety)
    assert not isinstance(result, Conflict)
    assert result.start_minute == 19 * 60
    assert result.end_minute == 21 * 60
    assert len(
        [x for x in store.plan_items(1, day) if x.title == "Задача 90 минут"]
    ) == 1

    two_and_half = TaskDraft(
        "Задача 2.5 часа",
        day,
        start_minute=15 * 60,
        duration_minutes=150,
    )
    result = scheduler.schedule(1, two_and_half)
    assert not isinstance(result, Conflict)
    assert result.start_minute == 15 * 60
    assert result.end_minute == 18 * 60
    assert len(
        [x for x in store.plan_items(1, day) if x.title == "Задача 2.5 часа"]
    ) == 1

    store.close()


def test_urgent_task_can_propose_one_day_kd_move(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft(
        "Срочная встреча", day, start_minute=10 * 60,
        duration_minutes=60, urgent=True,
    )
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.moves
    assert all(
        item.kind == TaskKind.RECURRING for item in result.proposal.conflicts
    )
    store.close()


def test_exports_create_files_with_merged_multihour_item(tmp_path: Path):

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Большая задача", day, start_minute=19 * 60, duration_minutes=90)
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)

    docx = build_docx(1, day, store, PlannerConfig(), tmp_path / "plan.docx")
    pdf = build_pdf(1, day, store, PlannerConfig(), tmp_path / "plan.pdf")
    assert docx.exists() and docx.stat().st_size > 0
    assert pdf.exists() and pdf.stat().st_size > 0
    store.close()


def test_clear_day_preserves_recurring_tasks_by_default(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    store.add_item(
        PlanItem(
            0, 1, day, "Обычная задача", None,
            15 * 60, 16 * 60, TaskKind.ORDINARY,
        )
    )

    deleted = store.clear_day(1, day)

    assert deleted == 1
    remaining = store.plan_items(1, day)
    assert len(remaining) == 3
    assert all(item.kind == TaskKind.RECURRING for item in remaining)
    store.close()


def test_clear_ordinary_items_preserves_recurring_after_refresh(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    service = PlannerService(store, PlannerConfig())
    store.ensure_daily_plan(1, day)
    store.add_item(
        PlanItem(
            0,
            1,
            day,
            "Обычная задача",
            None,
            15 * 60,
            16 * 60,
            TaskKind.ORDINARY,
        )
    )

    assert store.clear_ordinary_items(1, day) == 1
    assert len(store.plan_items(1, day)) == 3

    asyncio.run(service.show_plan(1, day))
    assert len(store.plan_items(1, day)) == 3
    store.close()


def test_clear_plan_requires_explicit_recurring_choice(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    store.add_item(
        PlanItem(
            0, 1, day, "Обычная задача", None,
            15 * 60, 16 * 60, TaskKind.ORDINARY,
        )
    )
    service = PlannerService(store, PlannerConfig())

    prompt = asyncio.run(service.start_clear_plan(1, day))
    assert "Ежедневных задач: 3" in prompt
    assert "\\n\\n" not in prompt
    assert "\n\n" in prompt
    assert store.session(1)["state"] == "plan_clear_confirm"

    replies = asyncio.run(service.handle_callback(1, "pl:clear:ordinary", day))
    assert "ежедневные задачи сохранены" in replies[0]
    assert len(store.plan_items(1, day)) == 3
    store.close()


def test_clear_day_can_explicitly_remove_recurring_tasks(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)

    deleted = store.clear_day(1, day, include_recurring=True)

    assert deleted == 3
    assert store.plan_items(1, day) == []
    assert store.is_day_cleared(1, day)
    store.close()


def test_flexible_task_uses_first_free_slot(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.add_item(
        PlanItem(
            0, 1, day, "Первая задача", None,
            9 * 60, 10 * 60, TaskKind.ORDINARY,
        )
    )
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Вторая задача", day)
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)
    assert (result.start_minute, result.end_minute) == (10 * 60, 11 * 60)
    store.close()


def test_exact_duplicate_submission_returns_existing_item_without_conflict(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft(
        "Сходить в магазин",
        day,
        start_minute=20 * 60,
        duration_minutes=60,
        why="Купить продукты",
    )
    first = scheduler.schedule(1, draft)
    assert not isinstance(first, Conflict)

    second = scheduler.schedule(1, draft)
    assert not isinstance(second, Conflict)
    assert second.id == first.id
    assert len(
        [x for x in store.plan_items(1, day) if x.title == "Сходить в магазин"]
    ) == 1
    store.close()


def test_exact_conflict_suggests_nearest_free_slots(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Резюме", day, start_minute=10 * 60, duration_minutes=60)
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.alternatives
    assert result.proposal.alternatives[0] == (11 * 60, 12 * 60)
    store.close()

def test_night_period_uses_after_midnight_plan_window(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Ночная задача", day, period="ночью", duration_minutes=60)
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.desired_start_minute == 24 * 60
    store.close()


def test_planner_config_rejects_invalid_timezone():
    with pytest.raises(ValueError, match="Invalid planner timezone"):
        PlannerConfig(timezone="Not/AZone")


def test_planner_config_default_timezone_is_explicit():
    assert PlannerConfig().timezone == "Europe/Moscow"


def test_ensure_daily_plan_is_idempotent_after_existing_ordinary_item(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)

    # An ordinary task may be created before the daily template is materialized.
    store.add_item(
        PlanItem(
            0, 1, day, "Обычная задача", None,
            12 * 60, 13 * 60, TaskKind.ORDINARY,
        )
    )

    items = store.ensure_daily_plan(1, day)
    assert any(item.kind == TaskKind.RECURRING for item in items)

    # Repeating the operation must not duplicate either ordinary or recurring items.
    again = store.ensure_daily_plan(1, day)
    assert [(item.id, item.title) for item in again] == [(item.id, item.title) for item in items]

    recurring_ids = [item.recurring_template_id for item in again if item.kind == TaskKind.RECURRING]
    assert len(recurring_ids) == len(set(recurring_ids))
    store.close()


def test_move_proposal_carries_expected_old_position(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Срочная задача", day, start_minute=10 * 60, duration_minutes=60, urgent=True)
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.moves
    move = result.proposal.moves[0]
    blocker = store.get_plan_item(move.plan_item_id)
    assert (move.old_start_minute, move.old_end_minute) == (blocker.start_minute, blocker.end_minute)
    store.close()


def test_confirmed_move_is_rejected_if_target_becomes_occupied(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())

    draft = TaskDraft(
        "Срочная встреча", day, start_minute=10 * 60,
        duration_minutes=60, urgent=True,
    )
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    move = result.proposal.moves[0]
    blocker = store.get_plan_item(move.plan_item_id)

    # Simulate a concurrent/manual change into the proposed destination.
    store.add_item(
        PlanItem(
            0, 1, day, "Новая задача", None,
            move.new_start_minute, move.new_end_minute, TaskKind.ORDINARY,
        )
    )

    with pytest.raises(ScheduleValidationError):
        scheduler.apply_proposal(1, result.proposal)

    unchanged = store.get_plan_item(blocker.id)
    assert (unchanged.start_minute, unchanged.end_minute) == (
        move.old_start_minute,
        move.old_end_minute,
    )
    assert not any(x.title == "Срочная встреча" for x in store.plan_items(1, day))
    store.close()


def test_callback_conflict_confirmation_uses_same_resolution_path(tmp_path: Path):

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig())
    service.store.set_session(
        1,
        "planning_conflict",
        day,
        {
            "draft": {
                "title": "Срочная встреча",
                "day": day.isoformat(),
                "start_minute": 10 * 60,
                "duration_minutes": 60,
                "why": None,
                "urgent": True,
                "source_text": "",
            },
            "desired": [10 * 60, 11 * 60],
            "conflicts": [
                {
                    "id": store.plan_items(1, day)[0].id,
                    "title": store.plan_items(1, day)[0].title,
                    "start": store.plan_items(1, day)[0].start_minute,
                    "end": store.plan_items(1, day)[0].end_minute,
                    "kind": TaskKind.RECURRING.value,
                }
            ],
            "alternatives": [],
            "moves": [],
            "pending": [],
        },
    )
    replies = asyncio.run(service.handle_callback(1, "pl:conf:no", date(2026, 9, 22)))
    assert replies
    assert service.store.session(1)["state"] == "planning"
    store.close()


def test_start_review_rejects_future_day(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())
    future = date(2026, 10, 3)
    result = asyncio.run(service.start_review(1, future, today=date(2026, 10, 2)))
    assert "ещё не наступил" in result
    assert store.session(1) is None
    store.close()


def test_start_full_review_rejects_future_day(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())
    future = date(2026, 10, 3)
    result = asyncio.run(service.start_full_review(1, future, today=date(2026, 10, 2)))
    assert "ещё не наступил" in result
    assert store.session(1) is None
    store.close()


def test_review_callback_cannot_resume_future_day_session(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())
    future = date(2026, 10, 3)
    store.set_session(1, "review_status", future, {"current_item_id": 1})
    replies = asyncio.run(service.handle_callback(1, "pl:review:+", date(2026, 10, 2)))
    assert "ещё не наступил" in replies[0]
    assert store.session(1) is None
    store.close()


def test_review_text_cannot_resume_future_day_session(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())
    future = date(2026, 10, 3)
    store.set_session(1, "review_detail", future, {"plan_item_id": 1})
    replies = asyncio.run(service.handle_text(1, "сделал", date(2026, 10, 2)))
    assert "ещё не наступил" in replies[0]
    assert store.session(1) is None
    store.close()


def test_review_status_callback_sets_detail_state(tmp_path: Path):

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig())
    service.store.set_session(1, "review_status", day, {"current_item_id": items[0].id})
    replies = asyncio.run(service.handle_callback(1, "pl:review:partial", day))
    assert replies
    session = service.store.session(1)
    assert session["state"] == "review_detail"
    assert service.store.session_payload(1)["status"] == "+-"
    store.close()


def test_review_uses_local_fallback_when_ai_is_unavailable(tmp_path: Path):
    class FailingReviewAI:
        async def extract_tasks(self, text, *, today, target_day, config):
            del text, today, target_day, config
            return []

        async def extract_review(self, text, *, task_title):
            del text, task_title
            from voqelis.planner.models import PlannerAIUnavailable

            raise PlannerAIUnavailable("review provider unavailable")

        async def extract_full_review(self, text, *, items):
            del text, items
            return []

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig(), ai=FailingReviewAI())

    asyncio.run(service.start_review(1, day))
    replies = asyncio.run(service.handle_callback(1, "pl:review:+", day))
    assert "Расскажи" in replies[0]

    saved = asyncio.run(service.handle_text(1, "сделал, чувствовал себя нормально", day))
    assert "Сохранено." in saved[0]

    review = next(
        item for item in store.reviews(1, day)
        if item.plan_item.id == items[0].id
    )
    assert review.status == "+"
    assert review.activity == "сделал, чувствовал себя нормально"
    assert review.feelings == ()
    assert review.missed_reason is None
    assert store.session(1)["state"] == "review_status"
    assert store.session_payload(1)["current_item_id"] == items[1].id
    store.close()


def test_review_can_resume_final_questions(tmp_path: Path):

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig())
    for item in items:
        store.save_review(item.id, "+", "сделал", (), None)
    asyncio.run(service._review_final1(1, "стал лучше планировать", day))
    assert store.session(1)["state"] == "review_final2"
    resumed = asyncio.run(service.start_review(1, day))
    assert "признаки срыва" in resumed.lower()
    assert store.session(1)["state"] == "review_final2"
    store.close()


def test_review_resume_after_all_items_without_day_review(tmp_path: Path):

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig())
    for item in items:
        store.save_review(item.id, "+", "сделал", (), None)
    resumed = asyncio.run(service.start_review(1, day))
    assert "оздоровлению" in resumed
    assert store.session(1)["state"] == "review_final1"
    store.close()


def test_full_review_requires_confirmation_and_then_continues_unmatched_tasks(tmp_path: Path):

    class FakeAI:
        async def extract_tasks(self, text, *, today, target_day, config):
            return []

        async def extract_review(self, text, *, task_title):
            return text, (), None

        async def extract_full_review(self, text, *, items):
            return [{
                "plan_item_id": items[0]["plan_item_id"],
                "status": "+",
                "activity": "сделал",
                "feelings": ["интерес"],
                "missed_reason": None,
            }]

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig(), ai=FakeAI())

    prompt = asyncio.run(service.start_full_review(1, day))
    assert "одним сообщением" in prompt

    proposal = asyncio.run(service.handle_text(1, "Весь день прошёл нормально", day))
    assert "Сохранить этот разбор?" in proposal[0]
    assert store.reviews(1, day)[0].status is None
    assert store.session(1)["state"] == "review_full_confirm"

    saved = asyncio.run(service.handle_callback(1, "pl:full:yes", day))
    assert "Остались пункты" in saved[0]
    assert store.session(1)["state"] == "review_status"
    assert store.reviews(1, day)[0].status == "+"

    store.close()


def test_full_review_provider_failure_falls_back_to_sequential_review(tmp_path: Path):
    class FailingFullReviewAI:
        async def extract_tasks(self, text, *, today, target_day, config):
            del text, today, target_day, config
            return []

        async def extract_review(self, text, *, task_title):
            return text, (), None

        async def extract_full_review(self, text, *, items):
            del text, items
            from voqelis.planner.models import PlannerAIUnavailable

            raise PlannerAIUnavailable("full review provider unavailable")

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    items = store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig(), ai=FailingFullReviewAI())

    asyncio.run(service.start_full_review(1, day))
    replies = asyncio.run(service.handle_text(1, "мой день прошёл нормально", day))

    assert "ничего не сохранено" in replies[0].casefold()
    assert "Выполнено?" in replies[1]
    assert store.session(1)["state"] == "review_status"
    assert store.session_payload(1)["current_item_id"] == items[0].id
    assert all(item.status is None for item in store.reviews(1, day))
    store.close()


def test_full_review_cancel_does_not_write_results(tmp_path: Path):

    class FakeAI:
        async def extract_tasks(self, text, *, today, target_day, config):
            return []

        async def extract_review(self, text, *, task_title):
            return text, (), None

        async def extract_full_review(self, text, *, items):
            return [{
                "plan_item_id": items[0]["plan_item_id"],
                "status": "+",
                "activity": "сделал",
                "feelings": [],
                "missed_reason": None,
            }]

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    service = PlannerService(store, PlannerConfig(), ai=FakeAI())

    asyncio.run(service.start_full_review(1, day))
    asyncio.run(service.handle_text(1, "мой день", day))
    result = asyncio.run(service.handle_callback(1, "pl:full:no", day))

    assert "не сохранён" in result[0]
    assert not any(x.status is not None for x in store.reviews(1, day))
    assert store.session(1) is None
    store.close()


def test_active_plan_day_survives_session_clear(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 10, 2)

    store.set_session(1, "planning", day, {})
    assert store.active_plan_day(1) == day

    store.clear_session(1)

    assert store.session(1) is None
    assert store.active_plan_day(1) == day
    store.close()


def test_legacy_session_target_day_is_migrated_to_active_day(tmp_path: Path):
    db_path = tmp_path / "planner.sqlite3"
    db = sqlite3.connect(db_path)
    db.execute(
        "CREATE TABLE planner_sessions ("
        "user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'idle', "
        "state TEXT NOT NULL DEFAULT 'idle', target_day TEXT, "
        "payload TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO planner_sessions(user_id,state,target_day,payload,updated_at) "
        "VALUES(?,?,?,?,?)",
        (1, "planning", "2026-10-03", "{}", "2026-09-30T00:00:00+00:00"),
    )
    db.commit()
    db.close()

    store = PlannerStore(db_path)

    assert store.active_plan_day(1) == date(2026, 10, 3)
    store.close()


def test_history_preserves_active_plan_day(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig(), ai=None)
    active_day = date(2026, 10, 2)

    store.set_session(1, "planning", active_day, {})
    history = asyncio.run(service.start_history(1, date(2026, 10, 1)))

    assert "Активный план: 02.10.2026" in history
    assert "02.10.2026" in history
    assert store.session(1)["state"] == "history_select"
    assert store.session(1)["target_day"] == "2026-10-02"
    store.close()


def test_recurring_rules_materialize_only_on_matching_days(tmp_path: Path):

    config = PlannerConfig(
        recurring_templates=(
            RecurringTemplateSpec(
                "Будняя задача",
                None,
                9 * 60,
                60,
                recurrence="weekdays",
            ),
            RecurringTemplateSpec(
                "Выходная задача",
                None,
                10 * 60,
                60,
                recurrence="weekends",
            ),
            RecurringTemplateSpec(
                "Среда",
                None,
                11 * 60,
                60,
                recurrence="custom",
                days_of_week=(2,),
            ),
        )
    )
    store = PlannerStore(tmp_path / "planner.sqlite3")

    monday = date(2026, 9, 21)
    saturday = date(2026, 9, 26)
    wednesday = date(2026, 9, 23)

    monday_items = store.ensure_daily_plan(1, monday, config)
    assert [x.title for x in monday_items] == ["Будняя задача"]

    saturday_items = store.ensure_daily_plan(2, saturday, config)
    assert [x.title for x in saturday_items] == ["Выходная задача"]

    wednesday_items = store.ensure_daily_plan(3, wednesday, config)
    assert [x.title for x in wednesday_items] == ["Будняя задача", "Среда"]

    store.close()


def test_recurring_rule_migration_preserves_existing_daily_templates(tmp_path: Path):

    db_path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE recurring_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            why TEXT,
            start_minute INTEGER NOT NULL,
            duration_minutes INTEGER NOT NULL,
            recurrence TEXT NOT NULL DEFAULT 'daily',
            active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO recurring_templates(user_id,title,why,start_minute,duration_minutes)
        VALUES(1,'Старая КД',NULL,540,60);
    """)
    db.commit()
    db.close()

    store = PlannerStore(db_path)
    columns = {row["name"] for row in store.db.execute("PRAGMA table_info(recurring_templates)")}
    assert "recurrence_days" in columns
    items = store.ensure_daily_plan(1, date(2026, 9, 23), PlannerConfig(
        recurring_templates=()
    ))
    assert [x.title for x in items] == ["Старая КД"]
    store.close()


def test_planning_prompts_for_missing_reason_and_reuses_previous_reason(tmp_path: Path):

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    config = PlannerConfig(recurring_templates=())
    service = PlannerService(store, config, ai=FixedPlannerAI([TaskDraft("Позвонить клиенту", day + timedelta(days=1), source_text="завтра позвонить клиенту", source_excerpt="завтра позвонить клиенту")]))

    store.add_item(
        PlanItem(
            0, 1, day - timedelta(days=1), "Позвонить клиенту",
            "Чтобы закрыть вопрос", 9 * 60, 10 * 60, TaskKind.ORDINARY,
        )
    )

    replies = asyncio.run(service.add_from_text(1, "завтра позвонить клиенту", day))
    assert "раньше была указана причина" in replies[0]
    assert store.session(1)["state"] == "planning_why"

    saved = asyncio.run(service.handle_callback(1, "pl:why:yes", day))
    assert "Добавил" in saved[0]
    item = store.plan_items(1, day + timedelta(days=1))[-1]
    assert item.why == "Чтобы закрыть вопрос"

    service.ai = FixedPlannerAI([TaskDraft("проверить почту", day + timedelta(days=1), source_text="завтра проверить почту", source_excerpt="завтра проверить почту")])
    replies = asyncio.run(service.add_from_text(1, "завтра проверить почту", day))
    assert "не указана причина" in replies[0]
    assert store.session(1)["state"] == "planning_why"

    saved = asyncio.run(service.handle_callback(1, "pl:why:skip", day))
    assert "Добавил" in saved[0]
    item = store.plan_items(1, day + timedelta(days=1))[-1]
    assert item.why is None
    store.close()


def test_previous_why_is_case_insensitive_for_cyrillic_titles(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.add_item(
        PlanItem(
            0, 1, day, "Позвонить Клиенту",
            "Чтобы закрыть вопрос", 9 * 60, 10 * 60, TaskKind.ORDINARY,
        )
    )
    assert store.previous_why(1, "позвонить клиенту") == "Чтобы закрыть вопрос"
    store.close()

def test_review_flow_never_deletes_plan_items(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    service = PlannerService(store, PlannerConfig(), ai=None)

    before = store.ensure_daily_plan(1, day)
    before_snapshot = [
        (item.id, item.title, item.start_minute, item.end_minute, item.kind.value)
        for item in before
    ]

    asyncio.run(service.start_review(1, day))
    for item in before:
        session = store.session(1)
        assert session is not None
        assert int(store.session_payload(1)["current_item_id"]) == item.id
        asyncio.run(service.handle_callback(1, "pl:review:+", day))
        asyncio.run(service.handle_text(1, "сделал", day))

    assert store.session(1)["state"] == "review_final1"
    asyncio.run(service.handle_text(1, "следовал рекомендации", day))
    assert store.session(1)["state"] == "review_final2"
    asyncio.run(service.handle_text(1, "замечал усталость", day))

    after = store.plan_items(1, day)
    after_snapshot = [
        (item.id, item.title, item.start_minute, item.end_minute, item.kind.value)
        for item in after
    ]
    assert after_snapshot == before_snapshot
    day_review = store.day_review(1, day)
    assert day_review is not None
    assert day_review.completed is True
    store.close()


def test_plan_edit_service_changes_title_and_why_without_moving_item(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    service = PlannerService(store, PlannerConfig())
    items = store.ensure_daily_plan(1, day)
    original = items[0]
    slot = (
        original.start_minute,
        original.end_minute,
        original.kind,
        original.recurring_template_id,
    )

    asyncio.run(service.start_plan_edit(1, day))
    asyncio.run(service.handle_text(1, "1", day))
    asyncio.run(service.handle_text(1, "дело", day))
    asyncio.run(service.handle_text(1, "Новое утреннее дело", day))

    edited = store.get_plan_item(original.id)
    assert edited.title == "Новое утреннее дело"
    assert (
        edited.start_minute,
        edited.end_minute,
        edited.kind,
        edited.recurring_template_id,
    ) == slot

    asyncio.run(service.start_plan_edit(1, day))
    asyncio.run(service.handle_text(1, "1", day))
    asyncio.run(service.handle_text(1, "зачем", day))
    asyncio.run(service.handle_text(1, "Для здоровья и бодрости", day))

    edited = store.get_plan_item(original.id)
    assert edited.why == "Для здоровья и бодрости"
    assert (
        edited.start_minute,
        edited.end_minute,
        edited.kind,
        edited.recurring_template_id,
    ) == slot
    store.close()


def test_delete_ordinary_item_service_preserves_recurring_items(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    service = PlannerService(store, PlannerConfig())
    recurring = store.ensure_daily_plan(1, day)
    store.add_item(
        PlanItem(
            0,
            1,
            day,
            "Обычная задача",
            None,
            15 * 60,
            16 * 60,
            TaskKind.ORDINARY,
        )
    )
    items = store.plan_items(1, day)
    ordinary_index = next(
        index
        for index, item in enumerate(items, 1)
        if item.title == "Обычная задача"
    )

    asyncio.run(service.start_delete_item(1, day))
    asyncio.run(service.handle_text(1, str(ordinary_index), day))

    remaining = store.plan_items(1, day)
    assert not any(item.title == "Обычная задача" for item in remaining)
    assert [item.id for item in remaining if item.kind == TaskKind.RECURRING] == [
        item.id for item in recurring
    ]
    store.close()


def test_clear_all_callback_removes_recurring_items_and_marks_day_cleared(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    service = PlannerService(store, PlannerConfig())
    store.ensure_daily_plan(1, day)

    asyncio.run(service.start_clear_plan(1, day))
    replies = asyncio.run(service.handle_callback(1, "pl:clear:all", day))

    assert "вместе с ежедневными задачами" in replies[0]
    assert store.plan_items(1, day) == []
    assert store.is_day_cleared(1, day)
    store.close()
