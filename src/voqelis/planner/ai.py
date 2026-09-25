from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
from datetime import date, timedelta
from typing import Protocol

import httpx

from .config import PlannerConfig
from .models import (
    PlannerAIError,
    PlannerAIInvalidResponse,
    PlannerAIProviderError,
    PlannerAIUnavailable,
    TaskDraft,
)

logger = logging.getLogger(__name__)


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
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlannerAIInvalidResponse("AI returned non-string time")
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
    if (
        draft.start_minute is not None
        and draft.end_minute is not None
        and draft.start_minute >= draft.end_minute
    ):
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


def _clean_task_title(value: str) -> str:
    title = re.sub(r"\s{2,}", " ", value.strip())
    title = re.sub(
        r"^(?:мне\s+)?(?:на\s+)?(?:сегодня|завтра|послезавтра)\s+"
        r"(?:(?:мне\s+)?(?:надо|нужно|хочу|планирую)\s+)?"
        r"(?:запланировать|запланирую|поставить)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"^(?:мне\s+)?(?:надо|нужно|хочу|планирую|буду)\s+",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s{2,}", " ", title).strip(" ,:-")
    if re.search(r"\b(?:чтобы|для того чтобы)\b", title, re.IGNORECASE):
        title = re.split(
            r"\b(?:чтобы|для того чтобы)\b",
            title,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" ,:-")
    return title


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
            "You are the structured task extractor for Voqelis Planner. "
            "Convert natural Russian speech into clean task records, not a transcript. "
            "Extract every distinct user-intended task and remove conversational filler. "
            "A task title must be a short action phrase such as 'читать книгу' or "
            "'заниматься своими проектами', not 'мне на завтра надо запланировать читать книгу'. "
            "Never put planning instructions, greetings, dictation filler, or the user's reason inside title. "
            "Put the purpose after 'чтобы', 'для', or equivalent into why. "
            "Use duration_minutes only when the user explicitly gives a duration. "
            "For an explicit interval, set both start_time and end_time and set duration_minutes to null. "
            "For an exact start, set only start_time and leave end_time null. "
            "Normalize colloquial Russian clock expressions to 24-hour HH:MM: "
            "'8 вечера' = '20:00', '9 вечера' = '21:00', "
            "'8 утра' = '08:00', '12 часов дня' = '12:00', "
            "'12 ночи' = '00:00'. "
            "For ranges, normalize both endpoints: 'с 8 вечера до 9 вечера' = 20:00–21:00. "
            "Do not interpret a clock expression such as 'в 14 часов' or '12 часов дня' as duration. "
            "Resolve relative dates from the supplied today date. "
            "If no exact time, duration, period, or relation is stated, keep those fields null. "
            "Do not invent a reason, urgency, or schedule. "
            f"Today is {today.isoformat()}; default planning day is {target_day.isoformat()}. "
            "Dates must be YYYY-MM-DD and times HH:MM. "
            "\n\nUser message:\n" + text
        )
        result = await self._json_call(prompt, TASK_SCHEMA)
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
                    title=_clean_task_title(title),
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
        items = result.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise PlannerAIInvalidResponse("Gemini returned invalid full-review items")
        return items



def _strict_schema(schema: dict) -> dict:
    result = copy.deepcopy(schema)

    def visit(node: object) -> object:
        if isinstance(node, dict):
            for key, value in list(node.items()):
                node[key] = visit(value)
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
            return node
        if isinstance(node, list):
            return [visit(value) for value in node]
        return node

    return visit(result)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class GroqPlannerAI:
    provider_name = "Groq"

    def __init__(self, api_key: str, *, model: str, timeout_seconds: int = 30):
        if not api_key.strip():
            raise PlannerAIUnavailable("GROQ_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = model.strip()
        if not self.model:
            raise PlannerAIUnavailable("GROQ_MODEL is not configured")
        self.timeout = httpx.Timeout(timeout_seconds)

    async def _json_call(self, prompt: str, schema: dict, *, schema_name: str) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": _strict_schema(schema),
                },
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise PlannerAIProviderError("Groq request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise PlannerAIProviderError("Groq request failed", retryable=True) from exc

        if response.status_code >= 400:
            raise PlannerAIProviderError(
                f"Groq API returned HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after_seconds=_retry_after_seconds(response),
                retryable=response.status_code == 429 or response.status_code >= 500,
            )

        try:
            raw = response.json()["choices"][0]["message"]["content"]
            result = json.loads(raw)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PlannerAIInvalidResponse("Groq returned invalid structured JSON") from exc
        if not isinstance(result, dict):
            raise PlannerAIInvalidResponse("Groq returned non-object JSON")
        return result

    async def extract_tasks(self, text: str, *, today: date, target_day: date, config: PlannerConfig) -> list[TaskDraft]:
        # Reuse Gemini's already-tested extraction/validation implementation.
        result = await self._json_call(
            (
                "You are the structured task extractor for Voqelis Planner. "
                "Convert natural Russian speech into clean task records, not a transcript. "
                "Extract every distinct intended task. Keep title short and put purpose into why. "
                "Use explicit intervals as start_time/end_time; use duration_minutes only for an "
                "explicit duration. Normalize Russian clock expressions to HH:MM. Do not invent "
                "reasons, urgency or schedule. Resolve relative dates from today. "
                f"Today is {today.isoformat()}; default planning day is {target_day.isoformat()}. "
                "Dates must be YYYY-MM-DD and times HH:MM.\n\nUser message:\n" + text
            ),
            TASK_SCHEMA,
            schema_name="voqelis_tasks",
        )
        raw_tasks = result.get("tasks")
        if not isinstance(raw_tasks, list):
            raise PlannerAIInvalidResponse("Groq tasks field is not an array")
        drafts=[]
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                raise PlannerAIInvalidResponse("Groq returned a non-object task")
            try:
                draft=TaskDraft(
                    title=_clean_task_title(raw["title"]),
                    day=date.fromisoformat(raw["day"]),
                    start_minute=_parse_time(raw.get("start_time")),
                    end_minute=_parse_time(raw.get("end_time")),
                    duration_minutes=raw.get("duration_minutes"),
                    period=raw.get("period"),
                    preferred_minute=_parse_time(raw.get("preferred_time")),
                    relation=raw.get("relation"),
                    anchor=raw.get("anchor"),
                    why=_optional_string(raw.get("why"), "why"),
                    urgent=raw["urgent"],
                    source_text=raw.get("source_text") or text,
                )
                _validate_task_draft(draft, today=today)
            except PlannerAIInvalidResponse:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise PlannerAIInvalidResponse("Groq returned invalid task data") from exc
            drafts.append(draft)
        return [draft for draft in drafts if draft.title]

    async def extract_review(self, text: str, *, task_title: str):
        raise PlannerAIInvalidResponse("Groq review extraction is not enabled yet")

    async def extract_full_review(self, text: str, *, items: list[dict]):
        raise PlannerAIInvalidResponse("Groq full-review extraction is not enabled yet")


class AIProviderRouter:
    def __init__(self, *, primary: PlannerAI | None, fallback: PlannerAI | None):
        if primary is None and fallback is None:
            raise PlannerAIUnavailable("No planner AI provider is configured")
        self.primary = primary
        self.fallback = fallback
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def _available(self) -> bool:
        async with self._lock:
            return self.primary is not None and time.monotonic() >= self._blocked_until

    async def _block(self, error: PlannerAIError) -> None:
        cooldown = 60.0 if isinstance(error, PlannerAIProviderError) and error.status_code == 429 else 15.0
        if isinstance(error, PlannerAIProviderError) and error.retry_after_seconds is not None:
            cooldown = max(1.0, error.retry_after_seconds)
        async with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + cooldown)

    async def _call(self, method: str, *args, **kwargs):
        if await self._available():
            try:
                return await getattr(self.primary, method)(*args, **kwargs)
            except (PlannerAIProviderError, PlannerAIInvalidResponse, PlannerAIUnavailable) as exc:
                await self._block(exc)
                logger.warning("PLANNER_AI_FAILOVER primary=Groq fallback=Gemini reason=%s", exc)
        if self.fallback is None:
            raise PlannerAIUnavailable("Planner AI primary provider is unavailable")
        result = await getattr(self.fallback, method)(*args, **kwargs)
        logger.info("PLANNER_AI_PROVIDER provider=%s", getattr(self.fallback, "provider_name", "fallback"))
        return result

    async def extract_tasks(self, text: str, *, today: date, target_day: date, config: PlannerConfig):
        return await self._call("extract_tasks", text, today=today, target_day=target_day, config=config)

    async def extract_review(self, text: str, *, task_title: str):
        return await self._call("extract_review", text, task_title=task_title)

    async def extract_full_review(self, text: str, *, items: list[dict]):
        return await self._call("extract_full_review", text, items=items)


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
