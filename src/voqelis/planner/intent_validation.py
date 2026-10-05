from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .config import PlannerConfig

_CLOCK = r"(\d{1,2})(?:(?::|\.)(\d{2}))?"
_CLOCK_CONTEXT = re.compile(
    rf"\b(?:в|к)\s+{_CLOCK}\s*(?:час(?:а|ов)?|ч)?\s*"
    r"(утра|дня|вечера|ночи)?\b",
    re.IGNORECASE,
)
_RANGE = re.compile(
    rf"\b(?:с\s+)?{_CLOCK}\s*(?:час(?:а|ов)?|ч)?\s*"
    r"(утра|дня|вечера|ночи)?\s*(?:до|-)\s*"
    rf"{_CLOCK}\s*(?:час(?:а|ов)?|ч)?\s*(утра|дня|вечера|ночи)?\b",
    re.IGNORECASE,
)
_PERIODS = {
    "утром": "morning",
    "утро": "morning",
    "утра": "morning",
    "днём": "day",
    "днем": "day",
    "день": "day",
    "вечером": "evening",
    "вечер": "evening",
    "ночью": "night",
    "ночь": "night",
}
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
_DATE_WORDS = (
    ("послезавтра", 2),
    ("завтра", 1),
    ("сегодня", 0),
)


@dataclass(frozen=True, slots=True)
class ExplicitConstraints:
    day: date | None = None
    start_minute: int | None = None
    end_minute: int | None = None
    period: str | None = None
    relation: str | None = None
    anchor: str | None = None
    evidence: str = ""


class IntentValidationError(ValueError):
    """The model output contradicts explicit user constraints or lacks evidence."""


def _minute(hour: str, minute: str | None, part: str | None) -> int:
    value = int(hour) * 60 + int(minute or 0)
    suffix = (part or "").casefold()
    if suffix in {"дня", "вечера"} and 1 <= int(hour) < 12:
        value += 12 * 60
    elif suffix == "ночи" and int(hour) == 12:
        value = int(minute or 0)
    if not 0 <= value < 24 * 60:
        raise IntentValidationError("Временная граница вне суток.")
    return value


def explicit_constraints(text: str, *, today: date, config: PlannerConfig) -> ExplicitConstraints:
    del config
    source = text.strip()
    if not source:
        return ExplicitConstraints()

    lowered = source.casefold()
    day = None
    for word, offset in _DATE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            day = today + timedelta(days=offset)
            break

    start = end = None
    evidence_parts: list[str] = []
    range_match = _RANGE.search(source)
    if range_match:
        start = _minute(range_match.group(1), range_match.group(2), range_match.group(3))
        end = _minute(range_match.group(4), range_match.group(5), range_match.group(6))
        if start >= end:
            raise IntentValidationError("Явный временной диапазон некорректен.")
        evidence_parts.append(range_match.group(0))
    else:
        clock = _CLOCK_CONTEXT.search(source)
        if clock:
            start = _minute(clock.group(1), clock.group(2), clock.group(3))
            evidence_parts.append(clock.group(0))

    periods = {
        value
        for phrase, value in _PERIODS.items()
        if re.search(rf"\b{re.escape(phrase)}\b", lowered)
    }
    if len(periods) > 1:
        raise IntentValidationError("В одном фрагменте обнаружены разные периоды суток.")
    period = next(iter(periods), None)

    relation_match = re.search(
        r"\b(после|перед|до)\s+(завтрака|завтраком|завтрак|обеда|обедом|обед|ужина|ужином|ужин)\b",
        lowered,
    )
    relation = anchor = None
    if relation_match:
        relation = "after" if relation_match.group(1) == "после" else "before"
        anchor = _RELATIONS[relation_match.group(2)]
        evidence_parts.append(relation_match.group(0))

    return ExplicitConstraints(
        day=day,
        start_minute=start,
        end_minute=end,
        period=period,
        relation=relation,
        anchor=anchor,
        evidence="; ".join(evidence_parts),
    )

def _canonical_source(text: str) -> str:
    return " ".join(text.casefold().split())


def _contains_excerpt(source: str, excerpt: str) -> bool:
    return _canonical_source(excerpt) in _canonical_source(source)


def validate_task_intents(
    intents,
    *,
    source_text: str,
    today: date,
    config: PlannerConfig,
) -> None:
    if not intents:
        return

    seen_excerpts: set[str] = set()
    for intent in intents:
        excerpt = (intent.source_excerpt or "").strip()
        if not excerpt:
            if len(intents) != 1:
                raise IntentValidationError(
                    "Для нескольких задач модель обязана указать источник каждой задачи."
                )
            excerpt = source_text.strip()
        if not _contains_excerpt(source_text, excerpt):
            raise IntentValidationError("Источник задачи не совпадает с исходным текстом.")

        if excerpt in seen_excerpts:
            raise IntentValidationError("Две задачи ссылаются на один и тот же фрагмент.")
        seen_excerpts.add(excerpt)

        facts = explicit_constraints(excerpt, today=today, config=config)

        if facts.day is not None and intent.day != facts.day:
            raise IntentValidationError(
                f"Модель изменила явную дату {facts.day.isoformat()}."
            )

        if facts.start_minute is not None and intent.start_minute != facts.start_minute:
            raise IntentValidationError(
                f"Модель изменила явное время {facts.start_minute}."
            )

        if facts.end_minute is not None and intent.end_minute != facts.end_minute:
            raise IntentValidationError(
                f"Модель изменила явный конец диапазона {facts.end_minute}."
            )

        if facts.period is not None and intent.period != facts.period:
            raise IntentValidationError(
                f"Модель изменила явный период {facts.period}."
            )

        if facts.relation is not None:
            if intent.relation != facts.relation or intent.anchor != facts.anchor:
                raise IntentValidationError(
                    "Модель изменила явное отношение к приёму пищи."
                )
        elif intent.relation is not None or intent.anchor is not None:
            raise IntentValidationError(
                "Модель добавила отношение к приёму пищи, которого нет в источнике."
            )

        if (
            facts.start_minute is None
            and facts.end_minute is None
            and intent.start_minute is not None
        ):
            raise IntentValidationError(
                "Модель придумала точное время, которого нет в источнике."
            )

        if facts.period is None and intent.period is not None:
            raise IntentValidationError(
                "Модель придумала период суток, которого нет в источнике."
            )
