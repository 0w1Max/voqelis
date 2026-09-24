from __future__ import annotations

import re
from datetime import date, timedelta

from .config import PlannerConfig
from .models import TaskDraft

_TIME_CONTEXT = re.compile(
    r"\b(?:в|к)\s+(\d{1,2})(?::(\d{2}))?\s*"
    r"(?:час(?:а|ов)?|ч)?\s*(?:утра|дня|вечера|ночи)?\b",
    re.IGNORECASE,
)
_TIME_BARE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_RANGE = re.compile(
    r"\b(?:с\s+)?(\d{1,2})(?::(\d{2}))?\s*"
    r"(?:час(?:а|ов)?|ч)?\s*(?:утра|дня|вечера|ночи)?\s*"
    r"(?:до|-)\s*(\d{1,2})(?::(\d{2}))?\s*"
    r"(?:час(?:а|ов)?|ч)?\s*(?:утра|дня|вечера|ночи)?\b",
    re.IGNORECASE,
)
_DURATION = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(?:час(?:а|ов)?|ч)\b", re.IGNORECASE)
_DURATION_MINUTES = re.compile(
    r"\b(\d+)\s*(?:минут(?:а|ы)?|мин\b)", re.IGNORECASE
)
_DURATION_HOURS_AND_MINUTES = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\s*(?:час(?:а|ов)?|ч)\s*(?:и\s*)?"
    r"(\d+)\s*(?:минут(?:а|ы)?|мин\b)",
    re.IGNORECASE,
)


def _minute(hour: str, minute: str | None = None, part: str | None = None) -> int:
    value = int(hour) * 60 + int(minute or 0)
    normalized = (part or "").casefold()
    if normalized == "дня" and 1 <= int(hour) < 12:
        value += 12 * 60
    elif normalized == "вечера" and 1 <= int(hour) < 12:
        value += 12 * 60
    elif normalized == "ночи" and int(hour) == 12:
        value = int(minute or 0)
    return value


def _date(text: str, today: date) -> date:
    normalized = text.casefold()
    if "послезавтра" in normalized:
        return today + timedelta(days=2)
    if "завтра" in normalized:
        return today + timedelta(days=1)
    if "сегодня" in normalized:
        return today
    return today + timedelta(days=1)


def _period(text: str) -> str | None:
    normalized = text.casefold()
    for value in (
        "утром",
        "утро",
        "днём",
        "днем",
        "день",
        "вечером",
        "вечер",
        "ночью",
        "ночь",
    ):
        if re.search(rf"\b{re.escape(value)}\b", normalized):
            return value
    return None


def _why(text: str) -> str | None:
    match = re.search(
        r"\b(?:чтобы|для того чтобы|для)\s+(.+)$",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip().rstrip(".") if match else None


def _time_match(text: str) -> re.Match[str] | None:
    match = _TIME_CONTEXT.search(text)
    if match:
        return match
    return _TIME_BARE.search(text)


def parse_voice(text: str, *, today: date, config: PlannerConfig) -> list[TaskDraft]:
    # V1 fallback extractor. AI providers can replace this boundary without
    # changing the deterministic scheduling layer.
    chunks = [
        chunk.strip(" ,;")
        for chunk in re.split(
            r"[.!?]+|,\s*(?=(?:а\s+)?(?:также|потом|ещё|еще|утром|днём|днем|вечером|вечер)\b)",
            text,
            flags=re.IGNORECASE,
        )
        if chunk.strip()
    ]

    drafts: list[TaskDraft] = []
    for chunk in chunks:
        day = _date(chunk, today)
        range_match = _RANGE.search(chunk)
        start = end = None

        if range_match:
            start = _minute(range_match.group(1), range_match.group(2), range_match.group(5))
            end = _minute(range_match.group(3), range_match.group(4), range_match.group(7))
            if not (0 <= start < end <= 24 * 60):
                raise ValueError("Временной диапазон задачи некорректен.")

        time_match = None if range_match else _time_match(chunk)
        if time_match:
            start = _minute(time_match.group(1), time_match.group(2), time_match.group(3))
            if not 0 <= start < 24 * 60:
                raise ValueError("Время задачи должно быть от 00:00 до 23:59.")

        duration = config.default_duration_minutes
        duration_match = None if range_match else _DURATION_HOURS_AND_MINUTES.search(chunk)
        if duration_match and not (
            time_match
            and duration_match.start() < time_match.end()
            and time_match.start() < duration_match.end()
        ):
            duration = (
                int(float(duration_match.group(1).replace(",", ".")) * 60)
                + int(duration_match.group(2))
            )
        else:
            duration_match = _DURATION_MINUTES.search(chunk)
            if duration_match and not (
                time_match
                and duration_match.start() < time_match.end()
                and time_match.start() < duration_match.end()
            ):
                duration = int(duration_match.group(1))
            else:
                duration_match = _DURATION.search(chunk)
                if duration_match and not (
                    time_match
                    and duration_match.start() < time_match.end()
                    and time_match.start() < duration_match.end()
                ):
                    duration = int(float(duration_match.group(1).replace(",", ".")) * 60)
                elif "полтора" in chunk.casefold():
                    duration = 90

        period = _period(chunk)

        preferred = None
        preferred_match = re.search(
            r"(?:ближе|примерно|около)\s+(?:в\s+)?(\d{1,2})(?::(\d{2}))?",
            chunk,
            re.IGNORECASE,
        )
        if preferred_match:
            preferred = _minute(
                preferred_match.group(1),
                preferred_match.group(2),
            )

        relation = None
        anchor = None
        after_match = re.search(
            r"после\s+(завтрака|обеда|ужина)",
            chunk,
            re.IGNORECASE,
        )
        before_match = re.search(
            r"до\s+(завтрака|обеда|ужина)",
            chunk,
            re.IGNORECASE,
        )
        if after_match:
            relation, anchor = "after", after_match.group(1).casefold()
        elif before_match:
            relation, anchor = "before", before_match.group(1).casefold()

        cleaned = re.sub(
            r"^(?:сегодня|завтра|послезавтра)\s*",
            "",
            chunk,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"^\s*(?:а|и|также|потом)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:утром|утро|днём|днем|день|вечером|вечер|ночью|ночь)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(_RANGE, "", cleaned)
        cleaned = re.sub(_TIME_CONTEXT, "", cleaned)
        cleaned = re.sub(_TIME_BARE, "", cleaned)
        cleaned = re.sub(_DURATION_HOURS_AND_MINUTES, "", cleaned)
        cleaned = re.sub(
            r"\bполтора\s+час(?:а|ов)?\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(_DURATION_MINUTES, "", cleaned)
        cleaned = re.sub(_DURATION, "", cleaned)
        cleaned = re.sub(
            r"\b(?:срочно|желательно|примерно|около|пожалуйста)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:мне надо|мне нужно|надо|нужно|планирую|буду)\b",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        title = re.sub(r"\s{2,}", " ", cleaned).strip(" ,:-")
        why = _why(chunk)
        if why:
            title = re.split(
                r"\b(?:чтобы|для того чтобы|для)\b",
                title,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip(" ,:-")
        if not title:
            continue

        drafts.append(
            TaskDraft(
                title=title,
                day=day,
                start_minute=start,
                end_minute=end,
                duration_minutes=duration,
                period=period,
                preferred_minute=preferred,
                relation=relation,
                anchor=anchor,
                why=why,
                urgent="срочно" in chunk.casefold(),
                source_text=chunk,
            )
        )

    return drafts
