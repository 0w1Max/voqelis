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


# Planner V1 supports an intentionally bounded 12-hour Russian clock vocabulary.
# Forms outside this table are rejected instead of being guessed.
_MERIDIEM_HOURS: dict[str, dict[int, int]] = {
    "утра": {**{hour: hour for hour in range(1, 12)}, 12: 0},
    "дня": {**{hour: hour + 12 for hour in range(1, 12)}, 12: 12},
    "вечера": {
        **{hour: hour + 12 for hour in range(1, 12)},
        **{hour: hour for hour in range(13, 24)},
    },
    "ночи": {
        **{hour: hour for hour in range(1, 6)},
        11: 23,
        12: 0,
    },
}


_NUMBER_WORDS = {
    "один": 1,
    "два": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "одного": 1,
    "двух": 2,
    "трех": 3,
    "трёх": 3,
    "четырех": 4,
    "четырёх": 4,
    "пяти": 5,
    "шести": 6,
    "семи": 7,
    "восьми": 8,
    "девяти": 9,
    "десяти": 10,
    "одиннадцати": 11,
    "двенадцати": 12,
    # Cardinal hour forms commonly emitted by Russian ASR systems.
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "двадцать один": 21,
    "двадцать два": 22,
    "двадцать три": 23,
    # Genitive forms used with «к» and in spoken time ranges.
    "тринадцати": 13,
    "четырнадцати": 14,
    "пятнадцати": 15,
    "шестнадцати": 16,
    "семнадцати": 17,
    "восемнадцати": 18,
    "девятнадцати": 19,
    "двадцати": 20,
    "двадцати одного": 21,
    "двадцати одному": 21,
    "двадцати двух": 22,
    "двадцати двум": 22,
    "двадцати трёх": 23,
    "двадцати трём": 23,
    "двадцати трех": 23,
    "двадцати трем": 23,
    # In clock context, Russian can omit the numeral «один»: «в час», «к часу», «до часа».
    "час": 1,
    "часа": 1,
    "часу": 1,
}

_HOUR_WORD_PATTERN = "|".join(
    re.escape(word) for word in sorted(_NUMBER_WORDS, key=len, reverse=True)
)

# Spoken minute values emitted by ASR, e.g. «тринадцать пятнадцать».
_MINUTE_NUMBER_WORDS = {
    "ноль": 0,
    "нуль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
}
_MINUTE_TENS = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
}
for _tens_text, _tens_value in _MINUTE_TENS.items():
    _MINUTE_NUMBER_WORDS[_tens_text] = _tens_value
    for _unit_text, _unit_value in tuple(_MINUTE_NUMBER_WORDS.items()):
        if 1 <= _unit_value <= 9:
            _MINUTE_NUMBER_WORDS[f"{_tens_text} {_unit_text}"] = (
                _tens_value + _unit_value
            )
_MINUTE_NUMBER_WORDS["ноль ноль"] = 0
_MINUTE_NUMBER_WORDS["нуль нуль"] = 0
_MINUTE_WORD_PATTERN = "|".join(
    re.escape(word)
    for word in sorted(_MINUTE_NUMBER_WORDS, key=len, reverse=True)
)


_RANGE = re.compile(
    r"(?<!\w)"
    rf"(?P<start_hour>\d{{1,2}}|{_HOUR_WORD_PATTERN})(?:(?::|\.)(?P<start_minute>\d{{2}}))?"
    rf"\s*(?:час(?:а|ов)?|ч)?\s*(?P<start_minute_words>{_MINUTE_WORD_PATTERN})?"
    r"\s*(?:минут(?:а|ы)?\s*)?(?P<start_meridiem>утра|дня|вечера|ночи)?"
    r"\s*(?:до|[-–—])\s*"
    rf"(?P<end_hour>\d{{1,2}}|{_HOUR_WORD_PATTERN})(?:(?::|\.)(?P<end_minute>\d{{2}}))?"
    r"\s*(?:час(?:а|ов)?|ч)?\s*(?P<end_minute_words>" + _MINUTE_WORD_PATTERN + r")?"
    r"\s*(?:минут(?:а|ы)?\s*)?(?P<end_meridiem>утра|дня|вечера|ночи)?"
    r"(?!\w)",
    re.IGNORECASE,
)

_CLOCK = re.compile(
    r"(?<!\w)(?:в|к)\s+"
    rf"(?P<hour>\d{{1,2}}|{_HOUR_WORD_PATTERN})(?:(?::|\.)(?P<minute>\d{{2}}))?"
    rf"\s*(?:час(?:а|ов)?|ч)?\s*(?P<minute_words>{_MINUTE_WORD_PATTERN})?"
    r"\s*(?:минут(?:а|ы)?\s*)?(?P<meridiem>утра|дня|вечера|ночи)?"
    r"(?!\w)",
    re.IGNORECASE,
)


def _clock_to_minute(
    hour_text: str,
    minute_text: str | None,
    meridiem: str | None,
    spoken_minute_text: str | None = None,
) -> int:
    normalized_hour = " ".join(hour_text.casefold().split())
    hour = (
        _NUMBER_WORDS[normalized_hour]
        if normalized_hour in _NUMBER_WORDS
        else int(hour_text)
    )
    if minute_text is not None:
        minute = int(minute_text)
    elif spoken_minute_text is not None:
        normalized_minute = " ".join(spoken_minute_text.casefold().split())
        minute = _MINUTE_NUMBER_WORDS[normalized_minute]
    else:
        minute = 0

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
        match.group("start_minute_words"),
    )
    end_minute = _clock_to_minute(
        match.group("end_hour"),
        match.group("end_minute"),
        end_meridiem or shared_meridiem,
        match.group("end_minute_words"),
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
                    match.group("minute_words"),
                ),
            )
        )

    return tuple(
        sorted(
            expressions,
            key=lambda expression: (expression.start, expression.end),
        )
    )
