from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum

from .temporal import (
    TemporalKind,
    TemporalRecognitionError,
    recognize_temporal_expressions,
)


class EvidenceRecognitionError(ValueError):
    """The source contains explicit constraints that cannot be recognized unambiguously."""


class EvidenceKind(StrEnum):
    DATE = "date"
    TIME = "time"
    TIME_RANGE = "time_range"
    PERIOD = "period"
    RELATION = "relation"


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int
    text: str

    def contains(self, other: SourceSpan) -> bool:
        return self.start <= other.start and other.end <= self.end

    def overlaps(self, other: SourceSpan) -> bool:
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True, slots=True)
class ExplicitEntity:
    kind: EvidenceKind
    span: SourceSpan
    day: date | None = None
    start_minute: int | None = None
    end_minute: int | None = None
    period: str | None = None
    relation: str | None = None
    anchor: str | None = None


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    day: date | None = None
    start_minute: int | None = None
    end_minute: int | None = None
    period: str | None = None
    relation: str | None = None
    anchor: str | None = None
    evidence: str = ""


@dataclass(frozen=True, slots=True)
class IntentEvidence:
    source_text: str
    entities: tuple[ExplicitEntity, ...]

    def for_task(self, span: SourceSpan) -> TaskEvidence:
        relevant = [entity for entity in self.entities if span.contains(entity.span)]

        date_values = {
            entity.day
            for entity in relevant
            if entity.kind == EvidenceKind.DATE and entity.day is not None
        }
        if not date_values:
            global_date_values = {
                entity.day
                for entity in self.entities
                if entity.kind == EvidenceKind.DATE and entity.day is not None
            }
            if len(global_date_values) == 1:
                date_values = global_date_values
            elif len(global_date_values) > 1:
                raise EvidenceRecognitionError(
                    "Для задачи не удалось однозначно определить явную дату."
                )

        starts = {
            entity.start_minute
            for entity in relevant
            if entity.kind in {EvidenceKind.TIME, EvidenceKind.TIME_RANGE}
            and entity.start_minute is not None
        }
        ends = {
            entity.end_minute
            for entity in relevant
            if entity.kind == EvidenceKind.TIME_RANGE
            and entity.end_minute is not None
        }
        periods = {
            entity.period
            for entity in relevant
            if entity.kind == EvidenceKind.PERIOD and entity.period is not None
        }
        relations = {
            (entity.relation, entity.anchor)
            for entity in relevant
            if entity.kind == EvidenceKind.RELATION
        }

        day = _single_value(date_values, "дат")
        start_minute = _single_value(starts, "времени")
        end_minute = _single_value(ends, "конца диапазона")
        period = _single_value(periods, "периода")
        relation_pair = _single_value(
            relations,
            "отношения к приёму пищи",
        )
        relation, anchor = relation_pair if relation_pair else (None, None)

        evidence_text = "; ".join(entity.span.text for entity in relevant)
        return TaskEvidence(
            day=day,
            start_minute=start_minute,
            end_minute=end_minute,
            period=period,
            relation=relation,
            anchor=anchor,
            evidence=evidence_text,
        )


def _single_value(values: set, label: str):
    if len(values) > 1:
        raise EvidenceRecognitionError(
            f"Невозможно однозначно определить явное значение: {label}."
        )
    return next(iter(values), None)


def _span(source: str, match: re.Match[str]) -> SourceSpan:
    start = match.start()
    end = match.end()

    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1

    return SourceSpan(start, end, source[start:end])

_DATE = re.compile(
    r"\b(послезавтра|завтра|сегодня)\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"\b(с\s+утра|утром|утро|днём|днем|вечером|вечер|ночью|ночь)\b",
    re.IGNORECASE,
)
_RELATION = re.compile(
    r"\b(после|перед|до)\s+"
    r"(завтрака|завтраком|завтрак|обеда|обедом|обед|ужина|ужином|ужин)\b",
    re.IGNORECASE,
)

_RELATIONS = {
    "завтрака": "breakfast",
    "завтраком": "breakfast",
    "завтрак": "breakfast",
    "обеда": "lunch",
    "обедом": "lunch",
    "обед": "lunch",
    "ужина": "dinner",
    "ужином": "dinner",
    "ужин": "dinner",
}
_PERIODS = {
    "с утра": "morning",
    "утром": "morning",
    "утро": "morning",
    "днём": "day",
    "днем": "day",
    "вечером": "evening",
    "вечер": "evening",
    "ночью": "night",
    "ночь": "night",
}

def recognize_intent_evidence(source_text: str, *, today: date) -> IntentEvidence:
    if not source_text.strip():
        return IntentEvidence(source_text=source_text, entities=())

    entities: list[ExplicitEntity] = []

    for match in _DATE.finditer(source_text):
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[
            match.group(1).casefold()
        ]
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.DATE,
                span=_span(source_text, match),
                day=today + timedelta(days=offset),
            )
        )

    try:
        temporal = recognize_temporal_expressions(source_text)
    except TemporalRecognitionError as exc:
        raise EvidenceRecognitionError(str(exc)) from exc

    for expression in temporal:
        span = SourceSpan(
            expression.start,
            expression.end,
            source_text[expression.start : expression.end],
        )
        if expression.kind == TemporalKind.TIME_RANGE:
            entities.append(
                ExplicitEntity(
                    kind=EvidenceKind.TIME_RANGE,
                    span=span,
                    start_minute=expression.start_minute,
                    end_minute=expression.end_minute,
                )
            )
        else:
            entities.append(
                ExplicitEntity(
                    kind=EvidenceKind.TIME,
                    span=span,
                    start_minute=expression.start_minute,
                )
            )

    for match in _PERIOD.finditer(source_text):
        key = " ".join(match.group(1).casefold().split())
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.PERIOD,
                span=_span(source_text, match),
                period=_PERIODS[key],
            )
        )

    for match in _RELATION.finditer(source_text):
        relation = (
            "after"
            if match.group(1).casefold() == "после"
            else "before"
        )
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.RELATION,
                span=_span(source_text, match),
                relation=relation,
                anchor=_RELATIONS[match.group(2).casefold()],
            )
        )

    entities.sort(key=lambda entity: (entity.span.start, entity.span.end))
    return IntentEvidence(
        source_text=source_text,
        entities=tuple(entities),
    )


def locate_source_span(source_text: str, source_excerpt: str) -> SourceSpan:
    excerpt = source_excerpt.strip()
    if not excerpt:
        raise EvidenceRecognitionError("Источник задачи пуст.")

    matches: list[int] = []
    start = source_text.find(excerpt)
    while start != -1:
        matches.append(start)
        start = source_text.find(excerpt, start + 1)

    if not matches:
        raise EvidenceRecognitionError(
            "Источник задачи не найден в исходном тексте без изменения текста."
        )
    if len(matches) > 1:
        raise EvidenceRecognitionError(
            "Источник задачи встречается в исходном тексте неоднозначно."
        )

    start = matches[0]
    return SourceSpan(start, start + len(excerpt), excerpt)
