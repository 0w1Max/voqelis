from __future__ import annotations

import json
from datetime import date
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
                    "status": {"type": ["string", "null"], "enum": ["+", "-", "+-"]},
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


class GeminiPlannerAI:
    def __init__(self, api_key: str, *, model: str, timeout_seconds: int = 30):
        if not api_key.strip():
            raise PlannerAIUnavailable("GEMINI_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = model.strip()
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
        try:
            for raw in result["tasks"]:
                drafts.append(TaskDraft(
                    title=str(raw["title"]).strip(),
                    day=date.fromisoformat(raw["day"]),
                    start_minute=_parse_time(raw.get("start_time")),
                    end_minute=_parse_time(raw.get("end_time")),
                    duration_minutes=None if raw.get("duration_minutes") is None else int(raw["duration_minutes"]),
                    period=raw.get("period"),
                    preferred_minute=_parse_time(raw.get("preferred_time")),
                    relation=raw.get("relation"),
                    anchor=raw.get("anchor"),
                    why=raw.get("why"),
                    urgent=bool(raw.get("urgent")),
                    source_text=str(raw.get("source_text") or text),
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise PlannerAIInvalidResponse("Invalid task object returned by Gemini") from exc
        return [item for item in drafts if item.title]

    async def extract_review(self, text: str, *, task_title: str) -> tuple[str | None, tuple[str, ...], str | None]:
        prompt = (
            "Extract a personal daily review. Preserve wording closely and do not invent facts. "
            f"Task: {task_title}\nUser text:\n{text}"
        )
        result = await self._json_call(prompt, REVIEW_SCHEMA)
        return (
            result.get("activity"),
            tuple(str(x).strip() for x in result.get("feelings", []) if str(x).strip()),
            result.get("missed_reason"),
        )

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
