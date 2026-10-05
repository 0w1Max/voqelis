from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


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

    def for_task(self, span: SourceSpan, *, task_count: int) -> TaskEvidence:
        relevant = [entity for entity in self.entities if entity.span.overlaps(span)]

        date_values = {
            entity.day
            for entity in relevant
            if entity.kind == EvidenceKind.DATE and entity.day is not None
        }
        if not date_values and task_count > 1:
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

        day = _single_value(date_values, "дат")
        starts = {
            entity.start_minute
            for entity in relevant
            if entity.kind in {EvidenceKind.TIME, EvidenceKind.TIME_RANGE}
            and entity.start_minute is not None
        }
        ends = {
            entity.end_minute
            for entity in relevant
            if entity.kind == EvidenceKind.TIME_RANGE and entity.end_minute is not None
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

        start_minute = _single_value(starts, "времени")
        end_minute = _single_value(ends, "конца диапазона")
        period = _single_value(periods, "периода")
        relation_pair = _single_value(relations, "отношения к приёму пищи")
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


_CLOCK = r"(?P<hour>\d{1,2})(?:(?::|\.)(?P<minute>\d{2}))?"
_RANGE = re.compile(
    rf"\bс\s+"
    rf"(?P<sh>\d{{1,2}})(?:(?::|\.)(?P<sm>\d{{2}}))?\s*"
    rf"(?:час(?:а|ов)?|ч)?\s*(?P<sp>утра|дня|вечера|ночи)?\s+"
    rf"до\s+"
    rf"(?P<eh>\d{{1,2}})(?:(?::|\.)(?P<em>\d{{2}}))?\s*"
    rf"(?:час(?:а|ов)?|ч)?\s*(?P<ep>утра|дня|вечера|ночи)?\b",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    rf"\b(?:в|к)\s+{_CLOCK}\s*"
    rf"(?:час(?:а|ов)?|ч)?\s*(?P<part>утра|дня|вечера|ночи)?"
    rf"(?!\s+(?:день|дня|дней|раз|раза|час|часа|часов|минут|минуты|минуту)\b)\b",
    re.IGNORECASE,
)
_DATE = re.compile(r"\b(послезавтра|завтра|сегодня)\b", re.IGNORECASE)
_PERIOD = re.compile(
    r"\b(с\s+утра|утром|утро|днём|днем|день|вечером|вечер|ночью|ночь)\b",
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
    "день": "day",
    "вечером": "evening",
    "вечер": "evening",
    "ночью": "night",
    "ночь": "night",
}


def _minute(hour: str, minute: str | None, part: str | None) -> int:
    hour_value = int(hour)
    minute_value = int(minute or 0)
    value = hour_value * 60 + minute_value
    suffix = (part or "").casefold()

    if suffix in {"дня", "вечера"} and 1 <= hour_value < 12:
        value += 12 * 60
    elif suffix == "ночи" and hour_value == 12:
        value = minute_value

    if not 0 <= value < 24 * 60:
        raise EvidenceRecognitionError("Временная граница вне суток.")
    return value


def _span(source: str, match: re.Match[str]) -> SourceSpan:
    return SourceSpan(match.start(), match.end(), source[match.start():match.end()])


def _entity_overlaps(entity: ExplicitEntity, spans: list[SourceSpan]) -> bool:
    return any(entity.span.overlaps(span) for span in spans)


def recognize_intent_evidence(source_text: str, *, today: date) -> IntentEvidence:
    source = source_text.strip()
    if not source:
        return IntentEvidence(source_text=source_text, entities=())

    entities: list[ExplicitEntity] = []

    for match in _DATE.finditer(source):
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[match.group(1).casefold()]
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.DATE,
                span=_span(source, match),
                day=today + timedelta(days=offset),
            )
        )

    range_spans: list[SourceSpan] = []
    for match in _RANGE.finditer(source):
        start_minute = _minute(
            match.group("sh"), match.group("sm"), match.group("sp")
        )
        end_minute = _minute(
            match.group("eh"), match.group("em"), match.group("ep")
        )
        if start_minute >= end_minute:
            raise EvidenceRecognitionError("Явный временной диапазон некорректен.")
        span = _span(source, match)
        range_spans.append(span)
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.TIME_RANGE,
                span=span,
                start_minute=start_minute,
                end_minute=end_minute,
            )
        )

    for match in _CLOCK.finditer(source):
        span = _span(source, match)
        if _entity_overlaps(
            ExplicitEntity(EvidenceKind.TIME, span), range_spans
        ):
            continue
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.TIME,
                span=span,
                start_minute=_minute(
                    match.group("hour"),
                    match.group("minute"),
                    match.group("part"),
                ),
            )
        )

    for match in _PERIOD.finditer(source):
        key = " ".join(match.group(1).casefold().split())
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.PERIOD,
                span=_span(source, match),
                period=_PERIODS[key],
            )
        )

    for match in _RELATION.finditer(source):
        relation = "after" if match.group(1).casefold() == "после" else "before"
        entities.append(
            ExplicitEntity(
                kind=EvidenceKind.RELATION,
                span=_span(source, match),
                relation=relation,
                anchor=_RELATIONS[match.group(2).casefold()],
            )
        )

    entities.sort(key=lambda entity: (entity.span.start, entity.span.end))
    return IntentEvidence(source_text=source_text, entities=tuple(entities))


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
