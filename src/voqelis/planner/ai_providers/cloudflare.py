"""Cloudflare structured extraction adapter and provider-specific prompts."""

from __future__ import annotations

import json
from datetime import date

import httpx

from ..ai_extraction import (
    TASK_SCHEMA,
    _parse_tasks_result,
    _task_prompt,
    _StructuredPlannerAI,
)
from ..ai_http import _cloudflare_provider_http_error, _strict_schema
from ..config import PlannerConfig
from ..models import (
    PlannerAIInvalidResponse,
    PlannerAIProviderError,
    PlannerAIUnavailable,
    TaskDraft,
)


def _cloudflare_task_prompt(text: str, *, today: date, target_day: date) -> str:
    return _task_prompt(text, today=today, target_day=target_day) + (
        "\n\nCLOUDFLARE OUTPUT RULES:\n"
        "The following field meanings are strict. Use only the values described below. "
        "preferred_time is ONLY an optional clock preference and must be an HH:MM time "
        "string or null; never put words, periods, meals, relations, or phrases there. "
        "Use period for explicit time-of-day words: 'утром' -> 'morning', "
        "'днём/днем' -> 'day', 'вечером' -> 'evening', 'ночью' -> 'night'. "
        "Use relation and anchor for explicit meal relations: 'после завтрака' -> "
        "relation='after', anchor='breakfast'; 'перед/до обеда' -> "
        "relation='before', anchor='lunch'; 'после обеда' -> "
        "relation='after', anchor='lunch'; 'перед/до ужина' -> "
        "relation='before', anchor='dinner'; 'после ужина' -> "
        "relation='after', anchor='dinner'. For a bounded window such as "
        "'после обеда и перед ужином', set relation='after', anchor='lunch', "
        "relation_end='before', anchor_end='dinner'. "
        "Do not invent an anchor. Never use breakfast as a generic default for morning, "
        "afternoon, or an unrelated task. If a task has a relation but no explicit clock, "
        "leave start_time, end_time, and preferred_time null. "
        "Never return end_time unless start_time is also present. "
        "For an exact interval, return both start_time and end_time. "
        "For an exact start without an explicit interval, return start_time only and "
        "leave end_time null. "
        "Do not put natural-language descriptions such as 'утром', 'after breakfast', "
        "or 'afternoon' into preferred_time. "
        "If the user says only a period such as 'вечером' or 'ночью', set period to the "
        "matching value and keep start_time, end_time, and preferred_time null. "
        "A period is not an exact clock time. Only set start_time when the user explicitly "
        "gives a clock or an explicit interval. "
        "When a purpose clause begins with 'для', 'чтобы', or 'для того чтобы', keep the "
        "purpose in why and out of title. For example, 'перед ужином сделать домашку "
        "по шагам для программы' must have title='сделать домашку по шагам' and "
        "why='для программы'. "
        "Preserve meaningful title wording from the user's task. Remove conversational "
        "filler such as 'ну', 'короче', 'я хочу', or 'это', but do not drop meaningful "
        "phrases such as 'на завтра' when they are part of the requested action. "
        "Preserve the user's why wording closely; do not rewrite prepositions or conjunctions "
        "when the meaning is already clear."
    )


def _normalize_cloudflare_task_result(result: dict) -> dict:
    normalized = dict(result)
    raw_tasks = result.get("tasks")
    if not isinstance(raw_tasks, list):
        return normalized

    tasks: list[object] = []
    for raw_task in raw_tasks:
        if not isinstance(raw_task, dict):
            tasks.append(raw_task)
            continue

        task = dict(raw_task)
        start = task.get("start_time")
        end = task.get("end_time")
        duration = task.get("duration_minutes")
        if (
            isinstance(start, str)
            and isinstance(end, str)
            and start.strip() == end.strip()
            and duration is None
        ):
            task["end_time"] = None
        tasks.append(task)

    normalized["tasks"] = tasks
    return normalized


class CloudflarePlannerAI(_StructuredPlannerAI):
    provider_name = "Cloudflare"

    def __init__(
        self,
        api_token: str,
        *,
        account_id: str,
        model: str,
        timeout_seconds: int = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_token.strip():
            raise PlannerAIUnavailable("CLOUDFLARE_API_TOKEN is not configured")
        if not account_id.strip():
            raise PlannerAIUnavailable("CLOUDFLARE_ACCOUNT_ID is not configured")
        self.api_token = api_token.strip()
        self.account_id = account_id.strip()
        self.model = model.strip()
        if not self.model:
            raise PlannerAIUnavailable("CLOUDFLARE_MODEL is not configured")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def _json_call(self, prompt: str, schema: dict) -> dict:
        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.model}"
        )
        payload = {
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 512,
            "response_format": {
                "type": "json_schema",
                "json_schema": _strict_schema(schema),
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.api_token}"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise PlannerAIProviderError(
                "Cloudflare request timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise PlannerAIProviderError(
                "Cloudflare request failed",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise _cloudflare_provider_http_error(response)

        try:
            body = response.json()
            result = body.get("result")
            if not isinstance(result, dict):
                raise TypeError("Cloudflare response.result is not an object")

            raw = result.get("response")
            if isinstance(raw, dict):
                parsed = raw
            elif isinstance(raw, str):
                parsed = json.loads(raw)
            else:
                choices = result.get("choices")
                if isinstance(choices, list) and choices:
                    first = choices[0]
                    message = first.get("message") if isinstance(first, dict) else None
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, dict):
                        parsed = content
                    elif isinstance(content, str):
                        parsed = json.loads(content)
                    else:
                        raise ValueError("missing Cloudflare structured response")
                else:
                    raise ValueError("missing Cloudflare structured response")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlannerAIInvalidResponse(
                "Cloudflare returned invalid structured JSON"
            ) from exc

        if not isinstance(parsed, dict):
            raise PlannerAIInvalidResponse("Cloudflare returned non-object JSON")
        return parsed

    async def extract_tasks(
        self,
        text: str,
        *,
        today: date,
        target_day: date,
        config: PlannerConfig,
    ) -> list[TaskDraft]:
        result = await self._json_call(
            _cloudflare_task_prompt(text, today=today, target_day=target_day),
            TASK_SCHEMA,
        )
        result = _normalize_cloudflare_task_result(result)
        return _parse_tasks_result(
            result,
            provider_name=self.provider_name,
            source_text=text,
            today=today,
        )
