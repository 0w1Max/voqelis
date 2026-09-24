import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from voqelis.planner.ai import GeminiPlannerAI
from voqelis.planner.config import PlannerConfig, RecurringTemplateSpec
from voqelis.planner.models import (
    Conflict,
    PlanItem,
    PlannerAIInvalidResponse,
    ScheduleValidationError,
    TaskDraft,
    TaskKind,
)
from voqelis.planner.parser import parse_voice
from voqelis.planner.scheduler import Scheduler
from voqelis.planner.service import PlannerService
from voqelis.planner.store import PlannerStore

def test_period_and_duration_extraction():
    drafts = parse_voice(
        "Завтра днем с 12 до 15 заниматься проектом",
        today=date(2026, 9, 22),
        config=PlannerConfig(),
    )
    assert len(drafts) == 1
    assert drafts[0].start_minute == 12 * 60
    assert drafts[0].end_minute == 15 * 60


def test_parser_supports_minute_and_mixed_durations():
    config = PlannerConfig()
    minute = parse_voice("завтра делать проект 90 минут", today=date(2026, 9, 22), config=config)[0]
    mixed = parse_voice("завтра делать проект 1 час 30 минут", today=date(2026, 9, 22), config=config)[0]
    assert minute.duration_minutes == 90
    assert mixed.duration_minutes == 90


def test_default_duration_is_one_hour():
    drafts = parse_voice("Завтра делать резюме", today=date(2026, 9, 22), config=PlannerConfig())
    assert drafts[0].duration_minutes == 60


def test_planner_markup_is_absent_without_active_session(tmp_path: Path):
    from voqelis.planner.bot import planner_markup_for_state

    store = PlannerStore(tmp_path / "planner.sqlite3")
    service = PlannerService(store, PlannerConfig())

    assert planner_markup_for_state(service, 1) is None
    store.close()


def test_recurring_tasks_are_created_first(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    items = store.ensure_daily_plan(1, date(2026, 9, 23))
    assert items
    assert all(item.kind == TaskKind.RECURRING for item in items)
    group = next(item for item in items if item.title.startswith("Дорога на группу"))
    assert (group.start_minute, group.end_minute) == (18 * 60, 19 * 60)
    assert not any(item.start_minute == 19 * 60 for item in items)
    store.close()


def test_conflict_is_not_auto_rescheduled(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = parse_voice("завтра в 10 делать резюме", today=date(2026, 9, 22), config=PlannerConfig())[0]
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.conflicts[0].kind == TaskKind.RECURRING
    store.close()


def test_exact_range_can_be_scheduled_when_free(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = parse_voice("завтра с 12 до 13 заниматься проектом", today=date(2026, 9, 22), config=PlannerConfig())[0]
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)
    assert result.start_minute == 12 * 60
    assert result.end_minute == 13 * 60
    store.close()


def test_ninety_minute_task_stays_one_logical_item(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Большая задача", day, start_minute=19 * 60, duration_minutes=90)
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)
    assert result.start_minute == 19 * 60
    assert result.end_minute == 21 * 60
    assert len([x for x in store.plan_items(1, day) if x.title == "Большая задача"]) == 1
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


def test_exact_conflict_suggests_nearest_free_slots(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Резюме", day, start_minute=10 * 60, duration_minutes=60)
    result = scheduler.schedule(1, draft)
    assert isinstance(result, Conflict)
    assert result.proposal.alternatives
    assert result.proposal.alternatives[0] == (12 * 60, 13 * 60)
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
    service = PlannerService(store, config, ai=None)

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
