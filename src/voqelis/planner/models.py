from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class TaskKind(str, Enum):
    RECURRING = "recurring"
    ORDINARY = "ordinary"


class ReviewStatus(str, Enum):
    DONE = "+"
    MISSED = "-"
    PARTIAL = "+-"


@dataclass(frozen=True, slots=True)
class PlanItem:
    id: int
    user_id: int
    day: date
    title: str
    why: str | None
    start_minute: int
    end_minute: int
    kind: TaskKind
    recurring_template_id: int | None = None
    urgent: bool = False
    source_text: str | None = None


@dataclass(frozen=True, slots=True)
class TaskDraft:
    title: str
    day: date
    start_minute: int | None = None
    end_minute: int | None = None
    duration_minutes: int | None = None
    period: str | None = None
    preferred_minute: int | None = None
    relation: str | None = None
    anchor: str | None = None
    why: str | None = None
    urgent: bool = False
    source_text: str = ""


@dataclass(frozen=True, slots=True)
class ScheduleMove:
    plan_item_id: int
    new_start_minute: int
    new_end_minute: int


@dataclass(frozen=True, slots=True)
class ConflictProposal:
    draft: TaskDraft
    desired_start_minute: int
    desired_end_minute: int
    conflicts: tuple[PlanItem, ...]
    alternatives: tuple[tuple[int, int], ...]
    moves: tuple[ScheduleMove, ...] = ()


@dataclass(frozen=True, slots=True)
class Conflict:
    proposal: ConflictProposal
    reason: str


@dataclass(frozen=True, slots=True)
class ReviewItem:
    plan_item: PlanItem
    status: str | None
    activity: str | None
    feelings: tuple[str, ...]
    missed_reason: str | None


@dataclass(frozen=True, slots=True)
class DayReview:
    day: date
    what_would_change: str | None
    relapse_signs: tuple[str, ...]
    completed: bool


class PlannerAIError(RuntimeError):
    pass


class PlannerAIUnavailable(PlannerAIError):
    pass


class PlannerAIInvalidResponse(PlannerAIError):
    pass


class ScheduleValidationError(ValueError):
    pass
