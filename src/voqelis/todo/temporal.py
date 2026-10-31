"""Conservative Russian text/ASR reminder date parser: no silent date/time guesses."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
# Spoken numbers preserve exact hours/minutes, without guessing an AM/PM.
SMALL_NUMBERS = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "час": 1,
    "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
PARTS = frozenset({"утра", "дня", "вечера", "ночи"})
HOUR_SUFFIXES = frozenset({"час", "часа", "часов"})
MINUTE_SUFFIXES = frozenset({"минута", "минуты", "минут", "минуту"})
DATE_PATTERN = (
    r"(?:сегодня|завтра|послезавтра|\d{1,2}\.\d{1,2}(?:\.\d{4})?"
    r"|\d{1,2}\s+(?:" + "|".join(MONTHS) + r"))"
)
TEXT_PATTERN = re.compile(
    rf"^(?P<date>{DATE_PATTERN}),?\s+(?:в\s+)?(?P<clock>.+)$",
    flags=re.IGNORECASE,
)
NUMERIC_TIME = re.compile(
    r"^(?P<h>\d{1,2})[:.](?P<m>[0-5]\d)"
    r"(?:\s+(?P<part>утра|дня|вечера|ночи))?$"
)


class ReminderTimeError(ValueError):
    """An ambiguous, invalid or out-of-window reminder time."""


def _spoken_number(words: list[str], at: int) -> tuple[int, int]:
    """Read exactly one number (0–59), including «двадцать два»."""
    if at >= len(words):
        raise ReminderTimeError("Не удалось определить точное время.")
    word = words[at]
    if re.fullmatch(r"\d{1,2}", word):
        return int(word), at + 1
    if word in SMALL_NUMBERS:
        return SMALL_NUMBERS[word], at + 1
    if word in TENS:
        value = TENS[word]
        if at + 1 < len(words) and words[at + 1] in SMALL_NUMBERS:
            unit = SMALL_NUMBERS[words[at + 1]]
            if 1 <= unit <= 9:
                return value + unit, at + 2
        return value, at + 1
    raise ReminderTimeError(
        "Укажи точное время, например «22:10» или «десять вечера десять минут»."
    )


def _resolve_clock(hour: int, minute: int, part: str | None, *,
                   numeric_clock: bool = False) -> time:
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ReminderTimeError("Часы и минуты должны быть в допустимых пределах.")

    if part is None:
        if numeric_clock or hour == 0 or 13 <= hour <= 23:
            return time(hour, minute)
        raise ReminderTimeError("Уточни время суток: утра, дня, вечера или ночи.")

    if 13 <= hour <= 23:
        # A redundant but consistent qualifier may occur in ASR.
        acceptable = (
            (part == "дня" and 13 <= hour <= 16)
            or (part == "вечера" and 17 <= hour <= 23)
        )
        if acceptable:
            return time(hour, minute)
        raise ReminderTimeError("Противоречие между часом и временем суток.")

    if part == "утра" and 4 <= hour <= 11:
        return time(hour, minute)
    if part == "дня" and (1 <= hour <= 4 or hour == 12):
        return time(hour + 12 if hour <= 4 else 12, minute)
    if part == "вечера" and 5 <= hour <= 11:
        return time(hour + 12, minute)
    if part == "ночи" and (1 <= hour <= 3 or hour == 12):
        return time(0 if hour == 12 else hour, minute)
    raise ReminderTimeError("Неоднозначное время суток. Укажи формат ЧЧ:ММ.")


def _clock(raw: str) -> time:
    raw = " ".join(raw.lower().strip().replace("ё", "е").replace(",", " ").split())
    matched = NUMERIC_TIME.fullmatch(raw)
    if matched:
        return _resolve_clock(
            int(matched["h"]), int(matched["m"]), matched["part"],
            numeric_clock=True,
        )

    words = raw.split()
    hour, pos = _spoken_number(words, 0)
    if pos < len(words) and words[pos] in HOUR_SUFFIXES:
        pos += 1

    minute: int | None = None
    part: str | None = None
    while pos < len(words):
        word = words[pos]
        if word in PARTS:
            if part is not None:
                raise ReminderTimeError("Время суток указано несколько раз.")
            part = word
            pos += 1
        elif word == "и" and minute is None and pos + 1 < len(words):
            pos += 1
        elif minute is None:
            minute, pos = _spoken_number(words, pos)
            if pos < len(words) and words[pos] in MINUTE_SUFFIXES:
                pos += 1
        else:
            raise ReminderTimeError("Не удалось однозначно определить минуты.")
    return _resolve_clock(hour, 0 if minute is None else minute, part)


def _date(raw: str, today: date) -> date:
    raw = raw.lower()
    relative = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    if raw in relative:
        return today + timedelta(days=relative[raw])
    if "." in raw:
        parts = raw.split(".")
        try:
            if len(parts) not in {2, 3}:
                raise ValueError
            d, m = int(parts[0]), int(parts[1])
            year = int(parts[2]) if len(parts) == 3 else today.year
            value = date(year, m, d)
        except ValueError as exc:
            raise ReminderTimeError("Неверная дата.") from exc
        if len(parts) == 2 and value < today:
            try:
                value = date(today.year + 1, m, d)
            except ValueError as exc:
                raise ReminderTimeError("Неверная дата. Укажи год.") from exc
        return value
    parts = raw.split()
    if len(parts) != 2 or parts[1] not in MONTHS:
        raise ReminderTimeError("Не удалось определить дату.")
    try:
        value = date(today.year, MONTHS[parts[1]], int(parts[0]))
        if value < today:
            value = date(today.year + 1, MONTHS[parts[1]], int(parts[0]))
    except ValueError as exc:
        raise ReminderTimeError("Неверная дата.") from exc
    return value


def _strict_local_datetime(day: date, clock: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(day, clock)
    valid = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        back = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if back == naive:
            valid.append(candidate)
    if not valid:
        raise ReminderTimeError("Это местное время не существует из-за смены часов.")
    if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
        raise ReminderTimeError("Время неоднозначно из-за перевода часов.")
    return valid[0].astimezone(UTC)


def parse_reminder_time(
    text: str, *, now: datetime, timezone: str = "Europe/Moscow",
) -> datetime:
    """Parse an exact local date+time into UTC, rejecting DST ambiguity and past times.

    Accepted examples: сегодня в 18:30, завтра в 9 утра,
    послезавтра в девять вечера, 5 ноября в 18:30, 05.11.2026 18:30.
    Does NOT infer a date/time from incomplete or arbitrary task text.
    """
    if now.tzinfo is None:
        raise ValueError("now must have timezone")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ReminderTimeError("Неизвестный часовой пояс.") from exc
    normalized = " ".join(text.strip().lower().replace("ё", "е").split())
    normalized = re.sub(r"^напомни(?:\s+мне)?\s+", "", normalized)
    normalized = normalized.rstrip(".!? ")
    match = TEXT_PATTERN.fullmatch(normalized)
    if match is None:
        raise ReminderTimeError(
            "Укажи дату и время: например, «завтра в 18:30»."
        )
    local_now = now.astimezone(zone)
    day = _date(match["date"], local_now.date())
    at = _strict_local_datetime(day, _clock(match["clock"]), zone)
    if at <= now.astimezone(UTC) + timedelta(seconds=30):
        raise ReminderTimeError("Время уже прошло. Выбери более позднее.")
    if at > now.astimezone(UTC) + timedelta(days=366):
        raise ReminderTimeError("Напоминание можно поставить на ближайший год.")
    return at
