from datetime import date
from pathlib import Path

from voqelis.planner.config import PlannerConfig
from voqelis.planner.models import Conflict, TaskDraft, TaskKind
from voqelis.planner.parser import parse_voice
from voqelis.planner.scheduler import Scheduler
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


def test_default_duration_is_one_hour():
    drafts = parse_voice("Завтра делать резюме", today=date(2026, 9, 22), config=PlannerConfig())
    assert drafts[0].duration_minutes == 60


def test_recurring_tasks_are_created_first(tmp_path: Path):
    store = PlannerStore(tmp_path / "planner.sqlite3")
    items = store.ensure_daily_plan(1, date(2026, 9, 23))
    assert items
    assert all(item.kind == TaskKind.RECURRING for item in items)
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
    draft = TaskDraft("Большая задача", day, start_minute=12 * 60, duration_minutes=90)
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)
    assert result.start_minute == 12 * 60
    assert result.end_minute == 14 * 60
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
    from voqelis.planner.export import build_docx, build_pdf

    store = PlannerStore(tmp_path / "planner.sqlite3")
    day = date(2026, 9, 23)
    store.ensure_daily_plan(1, day)
    scheduler = Scheduler(store, PlannerConfig())
    draft = TaskDraft("Большая задача", day, start_minute=12 * 60, duration_minutes=90)
    result = scheduler.schedule(1, draft)
    assert not isinstance(result, Conflict)

    docx = build_docx(1, day, store, PlannerConfig(), tmp_path / "plan.docx")
    pdf = build_pdf(1, day, store, PlannerConfig(), tmp_path / "plan.pdf")
    assert docx.exists() and docx.stat().st_size > 0
    assert pdf.exists() and pdf.stat().st_size > 0
    store.close()
