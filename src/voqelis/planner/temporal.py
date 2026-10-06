from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class TemporalRecognitionError(ValueError):
    """A temporal expression is invalid or outside the supported grammar."""


class TemporalKind(StrEnum):
    TIME = "time"
    TIME_RANGE = "time_range"


@dataclass(frozen=True, slots=True)
class TemporalExpression:
    kind: TemporalKind
    start: int
    end: int
    text: str
    start_minute: int
    end_minute: int | None = None


# Planner V1 accepts a deliberately bounded Russian clock grammar.
# The mapping is declarative: no hour-specific branches are used.
_MERIDIEM_HOURS: dict[str, dict[int, int]] = {
    "утра": {**{hour: hour for hour in range(1, 12)}, 12: 0},
    "дня": {**{hour: hour + 12 for hour in range(1, 12)}, 12: 12},
    "вечера": {hour: hour + 12 for hour in range(1, 12)},
    # Supported colloquial night forms: 11 ночи means 23:00,
    # while 1–5 ночи are after midnight; 12 ночи is midnight.
    "ночи": {
        **{hour: hour for hour in range(1, 6)},
        11: 23,
        12: 0,
    },
}


_RANGE = re.compile(
    r"(?<!w)"
    r"(?P<start_hour>d{1,2})(?:(?::|.)(?P<start_minute>d{2}))?"
    r"s*(?:час(?:а|ов)?|ч)?s*(?P<start_meridiem>утра|дня|вечера|ночи)?"
    r"s*(?:до|[-–—])s*"
    r"(?P<end_hour>d{1,2})(?:(?::|.)(?P<end_minute>d{2}))?"
    r"s*(?:час(?:а|ов)?|ч)?s*(?P<end_meridiem>утра|дня|вечера|ночи)?"
    r"(?!w)",
    re.IGNORECASE,
)

_CLOCK = re.compile(
    r"(?<!w)(?:в|к)s+"
    r"(?P<hour>d{1,2})(?:(?::|.)(?P<minute>d{2}))?"
    r"s*(?:час(?:а|ов)?|ч)?s*(?P<meridiem>утра|дня|вечера|ночи)?"
    r"(?!w)",
    re.IGNORECASE,
)


def _clock_to_minute(
    hour_text: str,
    minute_text: str | None,
    meridiem: str | None,
) -> int:
    hour = int(hour_text)
    minute = int(minute_text or 0)

    if not 0 <= minute <= 59:
        raise TemporalRecognitionError("Минуты вне допустимого диапазона.")

    if meridiem is None:
        if not 0 <= hour <= 23:
            raise TemporalRecognitionError(
                "24-часовые часы вне допустимого диапазона."
            )
        return hour * 60 + minute

    hour_table = _MERIDIEM_HOURS.get(meridiem.casefold())
    if hour_table is None or hour not in hour_table:
        raise TemporalRecognitionError(
            f"Некорректное сочетание часа и обозначения «{meridiem}»."
        )
    return hour_table[hour] * 60 + minute


def _range_end_minute(
    start_minute: int,
    end_minute: int,
    start_meridiem: str | None,
    end_meridiem: str | None,
) -> int:
    if end_minute > start_minute:
        return end_minute

    # Midnight rollover is accepted only when the lexical clock expression
    # makes the next-day interpretation explicit. This produces the planner's
    # continuous day timeline (23:00 -> 25:00), without guessing ordinary
    # backwards ranges.
    start_part = (start_meridiem or "").casefold()
    end_part = (end_meridiem or "").casefold()
    if end_part == "ночи" and start_part in {"", "вечера"}:
        return end_minute + 24 * 60

    raise TemporalRecognitionError(
        "Явный временной диапазон должен иметь возрастающие границы "
        "или явно переходить через полночь."
    )


def _range_to_minutes(match: re.Match[str]) -> tuple[int, int]:
    start_meridiem = match.group("start_meridiem")
    end_meridiem = match.group("end_meridiem")
    shared_meridiem = start_meridiem or end_meridiem

    start_minute = _clock_to_minute(
        match.group("start_hour"),
        match.group("start_minute"),
        start_meridiem or shared_meridiem,
    )
    end_minute = _clock_to_minute(
        match.group("end_hour"),
        match.group("end_minute"),
        end_meridiem or shared_meridiem,
    )

    return start_minute, _range_end_minute(
        start_minute,
        end_minute,
        start_meridiem,
        end_meridiem,
    )


def recognize_temporal_expressions(
    source_text: str,
) -> tuple[TemporalExpression, ...]:
    """Recognize only temporal forms explicitly supported by Planner V1."""
    ranges: list[TemporalExpression] = []

    for match in _RANGE.finditer(source_text):
        start_minute, end_minute = _range_to_minutes(match)
        ranges.append(
            TemporalExpression(
                kind=TemporalKind.TIME_RANGE,
                start=match.start(),
                end=match.end(),
                text=match.group(0),
                start_minute=start_minute,
                end_minute=end_minute,
            )
        )

    occupied = [(item.start, item.end) for item in ranges]
    expressions = list(ranges)

    for match in _CLOCK.finditer(source_text):
        start, end = match.span()
        if any(
            start < occupied_end and occupied_start < end
            for occupied_start, occupied_end in occupied
        ):
            continue

        expressions.append(
            TemporalExpression(
                kind=TemporalKind.TIME,
                start=start,
                end=end,
                text=match.group(0),
                start_minute=_clock_to_minute(
                    match.group("hour"),
                    match.group("minute"),
                    match.group("meridiem"),
                ),
            )
        )

    return tuple(
        sorted(
            expressions,
            key=lambda expression: (expression.start, expression.end),
        )
    )
