from __future__ import annotations

from datetime import date

from .config import PlannerConfig
from .models import PlanItem, ReviewItem
from .scheduler import fmt_time


def _rows_for_plan(items: list[PlanItem], config: PlannerConfig) -> list[tuple[int, int, PlanItem | None]]:
    rows = []
    cursor = config.plan_start_minute
    while cursor < config.plan_end_minute:
        end = min(cursor + config.slot_minutes, config.plan_end_minute)
        item = next(
            (x for x in items if x.start_minute <= cursor and x.end_minute >= end),
            None,
        )
        rows.append((cursor, end, item))
        cursor = end
    return rows


def render_plan_text(day: date, items: list[PlanItem], config: PlannerConfig) -> str:
    lines = [f"📅 План на {day.strftime('%d.%m.%Y')}"]
    for start, end, item in _rows_for_plan(items, config):
        lines.append(
            f"{fmt_time(start)}–{fmt_time(end)}  "
            + (f"{item.title}" if item else "свободно")
        )
    return "\n".join(lines)


def render_review_prompt(item: ReviewItem) -> str:
    return f"📝 {fmt_time(item.plan_item.start_minute)}–{fmt_time(item.plan_item.end_minute)}\n{item.plan_item.title}"
