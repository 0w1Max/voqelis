from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Protocol

import httpx

from .config import PlannerConfig
from .models import PlannerAIInvalidResponse, PlannerAIUnavailable, TaskDraft


TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "day": {"type": "string", "format": "date"},
                    "start_time": {"type": ["string", "null"], "format": "time"},
                    "end_time": {"type": ["string", "null"], "format": "time"},
                    "duration_minutes": {"type": ["integer", "null"]},
                    "period": {"type": ["string", "null"], "enum": ["morning", "day", "evening", "night"]},
                    "preferred_time": {"type": ["string", "null"], "format": "time"},
                    "relation": {"type": ["string", "null"], "enum": ["before", "after"]},
                    "anchor": {"type": ["string", "null"], "enum": ["breakfast", "lunch", "dinner"]},
                    "why": {"type": ["string", "null"]},
                    "urgent": {"type": "boolean"},
                    "source_text": {"type": "string"},
                },
                "required": [
                    "title", "day", "start_time", "end_time", "duration_minutes",
                    "period", "preferred_time", "relation", "anchor", "why", "urgent", "source_text",
                ],
            },
        }
    },
    "required": ["tasks"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "activity": {"type": ["string", "null"]},
        "feelings": {"type": "array", "items": {"type": "string"}},
        "missed_reason": {"type": ["string", "null"]},
    },
    "required": ["activity", "feelings", "missed_reason"],
}

FULL_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "plan_item_id": {"type": "integer"},
                    "status": {"type": ["string", "null"], "enum": ["+", "-", "+-", None]},
                    "activity": {"type": ["string", "null"]},
                    "feelings": {"type": "array", "items": {"type": "string"}},
                    "missed_reason": {"type": ["string", "null"]},
                },
                "required": ["plan_item_id", "status", "activity", "feelings", "missed_reason"],
            },
        }
    },
    "required": ["items"],
}


class PlannerAI(Protocol):
    async def extract_tasks(self, text: str, *, today: date, target_day: date, config: PlannerConfig) -> list[TaskDraft]:
        ...

    async def extract_review(self, text: str, *, task_title: str) -> tuple[str | None, tuple[str, ...], str | None]:
        ...

    async def extract_full_review(self, text: str, *, items: list[dict]) -> list[dict]:
        ...


def _parse_time(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split(":", 1)
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) == 2 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("invalid time")
    return hour * 60 + minute


def _validate_task_draft(draft: TaskDraft, *, today: date) -> None:
    if not draft.title.strip():
        raise PlannerAIInvalidResponse("AI returned an empty task title")
    if not today <= draft.day <= today + timedelta(days=2):
        raise PlannerAIInvalidResponse("AI returned a task day outside V1 planning horizon")
    if draft.start_minute is None and draft.end_minute is not None:
        raise PlannerAIInvalidResponse("AI returned end_time without start_time")
    if draft.start_minute is not None and not 0 <= draft.start_minute < 24 * 60:
        raise PlannerAIInvalidResponse("AI returned an invalid start time")
    if draft.end_minute is not None and not 0 <= draft.end_minute <= 24 * 60:
        raise PlannerAIInvalidResponse("AI returned an invalid end time")
    if draft.start_minute is not None and draft.end_minute is not None:
        if draft.start_minute >= draft.end_minute:
            raise PlannerAIInvalidResponse("AI returned an invalid time range")
    if draft.duration_minutes is not None and draft.duration_minutes <= 0:
        raise PlannerAIInvalidResponse("AI returned an invalid duration")
    if draft.period not in {None, "morning", "day", "evening", "night"}:
        raise PlannerAIInvalidResponse("AI returned an invalid period")
    if (draft.relation is None) != (draft.anchor is None):
        raise PlannerAIInvalidResponse("AI returned an incomplete relation/anchor pair")
    if draft.relation not in {None, "before", "after"}:
        raise PlannerAIInvalidResponse("AI returned an invalid relation")
    if draft.anchor not in {None, "breakfast", "lunch", "dinner"}:
        raise PlannerAIInvalidResponse("AI returned an invalid anchor")


def _optional_string(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlannerAIInvalidResponse(f"AI returned non-string {field}")
    return value.strip() or None


class GeminiPlannerAI:
    def __init__(self, api_key: str, *, model: str, timeout_seconds: int = 30):
        if not api_key.strip():
            raise PlannerAIUnavailable("GEMINI_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = model.strip()
        if not self.model:
            raise PlannerAIUnavailable("GEMINI_MODEL is not configured")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def _json_call(self, prompt: str, schema: dict) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
                raw = body["candidates"][0]["content"]["parts"][0]["text"]
                result = json.loads(raw)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PlannerAIInvalidResponse("Gemini API request/response failed") from exc
        if not isinstance(result, dict):
            raise PlannerAIInvalidResponse("Gemini returned non-object JSON")
        return result

    async def extract_tasks(self, text: str, *, today: date, target_day: date, config: PlannerConfig) -> list[TaskDraft]:
        prompt = (
            "You are Voqelis Planner task extractor. Extract every distinct task. "
            "Do not schedule or invent missing details. Preserve wording. "
            "Use duration_minutes only when explicitly stated; otherwise null. "
            "Exact interval fills start_time and end_time. Exact start fills start_time only. "
            "Use period for morning/day/evening/night, preferred_time for approximate time, "
            "and relation+anchor for before/after breakfast/lunch/dinner. "
            f"Today is {today.isoformat()}; default planning day is {target_day.isoformat()}. "
            "Dates must be YYYY-MM-DD and times HH:MM.\n\nUser message:\n" + text
        )
        result = await self._json_call(prompt, TASK_SCHEMA)
        drafts: list[TaskDraft] = []
        raw_tasks = result.get("tasks")
        if not isinstance(raw_tasks, list):
            raise PlannerAIInvalidResponse("Gemini tasks field is not an array")

        drafts: list[TaskDraft] = []
        try:
            for raw in raw_tasks:
                if not isinstance(raw, dict):
                    raise PlannerAIInvalidResponse("Gemini returned a non-object task")
                title = raw.get("title")
                day_value = raw.get("day")
                urgent = raw.get("urgent")
                duration = raw.get("duration_minutes")
                if not isinstance(title, str) or not isinstance(day_value, str):
                    raise PlannerAIInvalidResponse("Gemini returned invalid task identity fields")
                if not isinstance(urgent, bool):
                    raise PlannerAIInvalidResponse("Gemini returned invalid urgent flag")
                if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int)):
                    raise PlannerAIInvalidResponse("Gemini returned invalid duration")
                draft = TaskDraft(
                    title=title.strip(),
                    day=date.fromisoformat(day_value),
                    start_minute=_parse_time(raw.get("start_time")),
                    end_minute=_parse_time(raw.get("end_time")),
                    duration_minutes=duration,
                    period=raw.get("period"),
                    preferred_minute=_parse_time(raw.get("preferred_time")),
                    relation=raw.get("relation"),
                    anchor=raw.get("anchor"),
                    why=_optional_string(raw.get("why"), "why"),
                    urgent=urgent,
                    source_text=raw.get("source_text") if isinstance(raw.get("source_text"), str) else text,
                )
                _validate_task_draft(draft, today=today)
                drafts.append(draft)
        except PlannerAIInvalidResponse:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise PlannerAIInvalidResponse("Invalid task object returned by Gemini") from exc
        return [item for item in drafts if item.title]

    async def extract_review(self, text: str, *, task_title: str) -> tuple[str | None, tuple[str, ...], str | None]:
        prompt = (
            "Extract a personal daily review. Preserve wording closely and do not invent facts. "
            f"Task: {task_title}\nUser text:\n{text}"
        )
        result = await self._json_call(prompt, REVIEW_SCHEMA)
        activity = _optional_string(result.get("activity"), "activity")
        missed_reason = _optional_string(result.get("missed_reason"), "missed_reason")
        feelings_raw = result.get("feelings")
        if not isinstance(feelings_raw, list) or any(not isinstance(value, str) for value in feelings_raw):
            raise PlannerAIInvalidResponse("Gemini returned invalid feelings")
        return activity, tuple(value.strip() for value in feelings_raw if value.strip()), missed_reason

    async def extract_full_review(self, text: str, *, items: list[dict]) -> list[dict]:
        prompt = (
            "Match a user's one-message full-day review to plan items. "
            "Do not invent evidence; use null status when uncertain. Preserve wording closely.\n"
            f"Plan items: {json.dumps(items, ensure_ascii=False)}\nUser review:\n{text}"
        )
        result = await self._json_call(prompt, FULL_REVIEW_SCHEMA)
        return result.get("items", [])


class FallbackPlannerAI:
    """Development fallback. It deliberately does not claim to be an LLM."""

    def __init__(self, parser):
        self.parser = parser

    async def extract_tasks(self, text: str, *, today: date, target_day: date, config: PlannerConfig) -> list[TaskDraft]:
        return self.parser(text, today=today, config=config)

    async def extract_review(self, text: str, *, task_title: str) -> tuple[str | None, tuple[str, ...], str | None]:
        return text.strip() or None, (), None

    async def extract_full_review(self, text: str, *, items: list[dict]) -> list[dict]:
        return []
