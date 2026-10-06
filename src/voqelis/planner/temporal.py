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


@dataclass(frozen=True, slots=True)
class _ClockToken:
    hour: int
    minute: int
    meridiem: str | None


_RANGE = re.compile(
    r"(?<!\w)"
    r"(?P<sh>\d{1,2})(?:(?::|\.)(?P<sm>\d{2}))?"
    r"\s*(?:час(?:а|ов)?|ч)?\s*(?P<sp>утра|дня|вечера|ночи)?"
    r"\s*(?:до|[-–—])\s*"
    r"(?P<eh>\d{1,2})(?:(?::|\.)(?P<em>\d{2}))?"
    r"\s*(?:час(?:а|ов)?|ч)?\s*(?P<ep>утра|дня|вечера|ночи)?"
    r"(?!\w)",
    re.IGNORECASE,
)

_CLOCK = re.compile(
    r"(?<!\w)(?:в|к)\s+"
    r"(?P<hour>\d{1,2})(?:(?::|\.)(?P<minute>\d{2}))?"
    r"\s*(?:час(?:а|ов)?|ч)?\s*(?P<part>утра|дня|вечера|ночи)?"
    r"(?!\w)",
    re.IGNORECASE,
)

# This is the supported Russian 12-hour clock grammar. Keeping the mapping
# declarative makes normalization independent from individual hour cases.
_MERIDIEM_HOURS: dict[str, dict[int, int]] = {
    "утра": {hour: hour for hour in range(1, 12)} | {12: 0},
    "дня": {hour: hour + 12 for hour in range(1, 12)} | {12: 12},
    "вечера": {hour: hour + 12 for hour in range(1, 12)},
    "ночи": {hour: hour for hour in range(1, 12)} | {12: 0},
}


def _parse_clock(match: re.Match[str], *, prefix: str) -> _ClockToken:
    hour = int(match.group(f"{prefix}h" if prefix else "hour"))
    minute_value = match.group(f"{prefix}m" if prefix else "minute")
    minute = int(minute_value or 0)
    meridiem = match.group(f"{prefix}p" if prefix else "part")

    if not 0 <= minute <= 59:
        raise TemporalRecognitionError("Минуты вне допустимого диапазона.")

    if meridiem is None:
        if not 0 <= hour <= 23:
            raise TemporalRecognitionError(
                "24-часовые часы вне допустимого диапазона."
            )
        return _ClockToken(hour=hour, minute=minute, meridiem=None)

    hour_table = _MERIDIEM_HOURS.get(meridiem.casefold())
    if hour_table is None or hour not in hour_table:
        raise TemporalRecognitionError(
            f"Некорректное сочетание часа и обозначения «{meridiem}»."
        )

    return _ClockToken(
        hour=hour_table[hour],
        minute=minute,
        meridiem=meridiem.casefold(),
    )


def _to_minute(clock: _ClockToken) -> int:
    return clock.hour * 60 + clock.minute


def _parse_range(match: re.Match[str]) -> tuple[int, int]:
    start_meridiem = match.group("sp")
    end_meridiem = match.group("ep")
    shared_meridiem = start_meridiem or end_meridiem

    if start_meridiem is None:
        start_meridiem = shared_meridiem
    if end_meridiem is None:
        end_meridiem = shared_meridiem

    start = _parse_clock(
        _RangeView(match, "s", start_meridiem),
        prefix="s",
    )
    end = _parse_clock(
        _RangeView(match, "e", end_meridiem),
        prefix="e",
    )

    start_minute = _to_minute(start)
    end_minute = _to_minute(end)
    if start_minute >= end_minute:
        raise TemporalRecognitionError(
            "Явный временной диапазон должен иметь возрастающие границы."
        )
    return start_minute, end_minute


@dataclass(frozen=True, slots=True)
class _RangeView:
    match: re.Match[str]
    side: str
    meridiem: str | None

    def group(self, name: str) -> str | None:
        if name == "sp" or name == "ep":
            return self.meridiem
        return self.match.group(f"{self.side}{name[-1]}")


def recognize_temporal_expressions(source_text: str) -> tuple[TemporalExpression, ...]:
    ranges: list[TemporalExpression] = []

    for match in _RANGE.finditer(source_text):
        start_minute, end_minute = _parse_range(match)
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

    expressions = ranges[:]
    range_spans = [(item.start, item.end) for item in ranges]

    for match in _CLOCK.finditer(source_text):
        start, end = match.span()
        if any(start < range_end and range_start < end for range_start, range_end in range_spans):
            continue

        clock = _parse_clock(match, prefix="")
        expressions.append(
            TemporalExpression(
                kind=TemporalKind.TIME,
                start=start,
                end=end,
                text=match.group(0),
                start_minute=_to_minute(clock),
            )
        )

    return tuple(sorted(expressions, key=lambda item: (item.start, item.end)))
