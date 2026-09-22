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
    assert not isinstance(result, Conflict)
    assert result.start_minute == 24 * 60
    assert result.end_minute == 25 * 60
    store.close()


def test_planner_config_rejects_invalid_timezone():
    from voqelis.planner.config import PlannerConfig
    import pytest
    with pytest.raises(ValueError, match="Invalid planner timezone"):
        PlannerConfig(timezone="Not/AZone")


def test_planner_config_default_timezone_is_explicit():
    from voqelis.planner.config import PlannerConfig
    assert PlannerConfig().timezone == "Europe/Berlin"


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
