from __future__ import annotations

import re
from datetime import date, timedelta

from .config import PlannerConfig
from .models import TaskDraft


_TIME = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?(?:\s*(?:час(?:а|ов)?|ч))?")
_RANGE = re.compile(r"с\s+(\d{1,2})(?::(\d{2}))?\s*(?:до|-)\s*(\d{1,2})(?::(\d{2}))?", re.I)
_DURATION = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:час(?:а|ов)?|ч)", re.I)
_DURATION_MINUTES = re.compile(r"(\d+)\s*(?:минут(?:а|ы)?|мин\b)", re.I)
_DURATION_HOURS_AND_MINUTES = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(?:час(?:а|ов)?|ч)\s*(?:и\s*)?(\d+)\s*(?:минут(?:а|ы)?|мин\b)",
    re.I,
)


def _minute(hour: str, minute: str | None = None) -> int:
    return int(hour) * 60 + int(minute or 0)


def _date(text: str, today: date) -> date:
    t = text.lower()
    if "послезавтра" in t:
        return today + timedelta(days=2)
    if "завтра" in t:
        return today + timedelta(days=1)
    return today + timedelta(days=1)


def _period(text: str) -> str | None:
    for x in ("утром", "утро", "днём", "днем", "день", "вечером", "вечер", "ночью", "ночь"):
        if x in text.lower():
            return x
    return None


def _why(text: str) -> str | None:
    m = re.search(r"\b(?:чтобы|для того чтобы|для)\s+(.+)$", text, re.I)
    return f"{m.group(1).strip().rstrip('.')}" if m else None


def parse_voice(text: str, *, today: date, config: PlannerConfig) -> list[TaskDraft]:
    # V1 deliberately keeps extraction deterministic. The parser is a replaceable
    # boundary for a future LLM provider; scheduling remains deterministic.
    chunks = [c.strip(" ,;") for c in re.split(r"\s+(?:также|потом|ещё|еще|а также)\s+|[.!?]+", text, flags=re.I) if c.strip()]
    drafts: list[TaskDraft] = []
    for chunk in chunks:
        day = _date(chunk, today)
        rng = _RANGE.search(chunk)
        start = end = None
        if rng:
            start = _minute(rng.group(1), rng.group(2))
            end = _minute(rng.group(3), rng.group(4))
        else:
            tm = _TIME.search(chunk)
            if tm:
                start = _minute(tm.group(1), tm.group(2))
        duration = config.default_duration_minutes
        dm = _DURATION_HOURS_AND_MINUTES.search(chunk)
        if dm:
            duration = int(float(dm.group(1).replace(",", ".")) * 60) + int(dm.group(2))
        else:
            dm = _DURATION_MINUTES.search(chunk)
            if dm:
                duration = int(dm.group(1))
            else:
                dm = _DURATION.search(chunk)
                if dm:
                    duration = int(float(dm.group(1).replace(",", ".")) * 60)
                if "полтора" in chunk.lower():
                    duration = 90

        period = _period(chunk)
        preferred = None
        m_pref = re.search(r"(?:ближе|примерно|около)\s+(\d{1,2})(?::(\d{2}))?", chunk, re.I)
        if m_pref:
            preferred = _minute(m_pref.group(1), m_pref.group(2))

        relation = None
        anchor = None
        m_after = re.search(r"после\s+(завтрака|обеда|ужина)", chunk, re.I)
        m_before = re.search(r"до\s+(завтрака|обеда|ужина)", chunk, re.I)
        if m_after:
            relation, anchor = "after", m_after.group(1).lower()
        elif m_before:
            relation, anchor = "before", m_before.group(1).lower()

        cleaned = re.sub(r"^(?:завтра|послезавтра)\s*", "", chunk, flags=re.I)
        cleaned = re.sub(r"(?:утром|утро|днём|днем|день|вечером|вечер|ночью|ночь)", "", cleaned, flags=re.I)
        cleaned = re.sub(r"с\s+\d{1,2}(?::\d{2})?\s*(?:до|-)\s*\d{1,2}(?::\d{2})?", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\bв\s+\d{1,2}(?::\d{2})?", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\b(?:срочно|желательно|примерно|около|пожалуйста)\b", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\b(?:на|мне надо|мне нужно|надо|нужно|планирую|буду)\b", "", cleaned, flags=re.I)
        title = cleaned.strip(" ,:-")
        why = _why(chunk)
        if why:
            title = re.split(r"\b(?:чтобы|для того чтобы|для)\b", title, maxsplit=1, flags=re.I)[0].strip(" ,:-")
        if not title:
            continue
        urgent = "срочно" in chunk.lower()
        drafts.append(TaskDraft(
            title=title, day=day, start_minute=start, end_minute=end,
            duration_minutes=duration, period=period, preferred_minute=preferred,
            relation=relation, anchor=anchor, why=why, urgent=urgent, source_text=chunk,
        ))
    return drafts
