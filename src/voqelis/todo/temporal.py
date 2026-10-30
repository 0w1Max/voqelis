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
HOURS = {
    "час": 1, "два": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12,
}
PARTS = frozenset({"утра", "дня", "вечера", "ночи"})
DATE_PATTERN = (
    r"(?:сегодня|завтра|послезавтра|\d{1,2}\.\d{1,2}(?:\.\d{4})?"
    r"|\d{1,2}\s+(?:" + "|".join(MONTHS) + r"))"
)
TEXT_PATTERN = re.compile(
    rf"^(?P<date>{DATE_PATTERN})\s+(?:в\s+)?(?P<clock>.+)$",
    flags=re.IGNORECASE,
)
NUMERIC_TIME = re.compile(r"^(?P<h>\d{1,2}):(?P<m>[0-5]\d)$")
SPOKEN_TIME = re.compile(
    r"^(?P<h>\d{1,2}|" + "|".join(HOURS) + r")\s+"
    r"(?P<p>утра|дня|вечера|ночи)$"
)


class ReminderTimeError(ValueError):
    """An ambiguous, invalid or out-of-window reminder time."""


def _clock(raw: str) -> time:
    raw = raw.lower().strip().replace("ё", "е")
    matched = NUMERIC_TIME.fullmatch(raw)
    if matched:
        hh, mm = int(matched["h"]), int(matched["m"])
        if hh > 23:
            raise ReminderTimeError("Час должен быть от 00 до 23.")
        return time(hh, mm)
    matched = SPOKEN_TIME.fullmatch(raw)
    if not matched:
        raise ReminderTimeError("Укажи точное время, например «18:30» или «девять утра».")
    raw_hour, part = matched["h"], matched["p"]
    hour = HOURS.get(raw_hour, -1)
    if hour < 0:
        hour = int(raw_hour)
    if not 1 <= hour <= 12:
        raise ReminderTimeError("Время суток неоднозначно.")
    if part == "утра" and 4 <= hour <= 11:
        return time(hour, 0)
    if part == "дня" and 1 <= hour <= 4:
        return time(hour + 12, 0)
    if part == "дня" and hour == 12:
        return time(12, 0)
    if part == "вечера" and 5 <= hour <= 11:
        return time(hour + 12, 0)
    if part == "ночи" and 1 <= hour <= 3:
        return time(hour, 0)
    if part == "ночи" and hour == 12:
        return time(0, 0)
    raise ReminderTimeError("Неоднозначное время суток. Введи формат ЧЧ:ММ.")


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
