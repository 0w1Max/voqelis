from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from voqelis.planner.ai import (
    TASK_SCHEMA,
    GeminiPlannerAI,
    GroqPlannerAI,
    _parse_tasks_result,
    _strict_schema,
    _task_prompt,
)
from voqelis.planner.config import PlannerConfig
from voqelis.planner.intent_validation import validate_task_intents
from voqelis.planner.models import PlannerAIError

TODAY = date(2026, 10, 1)
TARGET_DAY = date(2026, 10, 2)
CONFIG = PlannerConfig()


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    text: str
    expected: list[dict[str, Any]]


CASES = (
    Case(
        "exact_evening",
        "завтра в 8 вечера читать книгу для здоровья, для настроения, для отдыха",
        [{"title": "читать книгу", "day": TARGET_DAY.isoformat(), "start_minute": 1200, "end_minute": None, "duration_minutes": 60, "why": "для здоровья, для настроения, для отдыха", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "explicit_range",
        "завтра с 12 до 15 заниматься проектом для опыта и для резюме",
        [{"title": "заниматься проектом", "day": TARGET_DAY.isoformat(), "start_minute": 720, "end_minute": 900, "duration_minutes": 180, "why": "для опыта и для резюме", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "morning_period",
        "завтра утром сделать зарядку для здоровья",
        [{"title": "сделать зарядку", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для здоровья", "period": "morning", "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "evening_period",
        "завтра вечером почитать книгу для отдыха",
        [{"title": "почитать книгу", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для отдыха", "period": "evening", "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "after_breakfast",
        "завтра после завтрака позвонить маме чтобы узнать как она",
        [{"title": "позвонить маме", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "чтобы узнать как она", "period": None, "relation": "after", "anchor": "breakfast", "urgent": False}],
    ),
    Case(
        "before_lunch",
        "завтра перед обедом проверить почту для работы",
        [{"title": "проверить почту", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для работы", "period": None, "relation": "before", "anchor": "lunch", "urgent": False}],
    ),
    Case(
        "after_lunch",
        "завтра после обеда прогуляться чтобы отдохнуть",
        [{"title": "прогуляться", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "чтобы отдохнуть", "period": None, "relation": "after", "anchor": "lunch", "urgent": False}],
    ),
    Case(
        "before_dinner",
        "завтра перед ужином сделать домашку по шагам для программы",
        [{"title": "сделать домашку по шагам", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для программы", "period": None, "relation": "before", "anchor": "dinner", "urgent": False}],
    ),
    Case(
        "after_dinner",
        "завтра после ужина подготовить вещи ко сну",
        [{"title": "подготовить вещи ко сну", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "why": None, "duration_minutes": None, "period": None, "relation": "after", "anchor": "dinner", "urgent": False}],
    ),
    Case(
        "explicit_duration",
        "завтра сделать резюме 90 минут для поиска работы",
        [{"title": "сделать резюме", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": 90, "why": "для поиска работы", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "exact_morning",
        "завтра в 8 утра помолиться для спокойствия",
        [{"title": "помолиться", "day": TARGET_DAY.isoformat(), "start_minute": 480, "end_minute": None, "duration_minutes": 60, "why": "для спокойствия", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "exact_night",
        "завтра в 12 ночи подготовка ко сну",
        [{"title": "подготовка ко сну", "day": TARGET_DAY.isoformat(), "start_minute": 0, "end_minute": None, "duration_minutes": 60, "why": None, "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "urgent",
        "завтра срочно в 14 часов позвонить в сервис чтобы решить проблему",
        [{"title": "позвонить в сервис", "day": TARGET_DAY.isoformat(), "start_minute": 840, "end_minute": None, "duration_minutes": 60, "why": "чтобы решить проблему", "period": None, "relation": None, "anchor": None, "urgent": True}],
    ),
    Case(
        "today",
        "сегодня в 18:00 почитать документацию для проекта",
        [{"title": "почитать документацию", "day": TODAY.isoformat(), "start_minute": 1080, "end_minute": None, "duration_minutes": 60, "why": "для проекта", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "day_after_tomorrow",
        "послезавтра в 9 утра переделать резюме для поиска работы",
        [{"title": "переделать резюме", "day": "2026-10-03", "start_minute": 540, "end_minute": None, "duration_minutes": 60, "why": "для поиска работы", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "why_pause_list",
        "завтра в 19 читать духовную книгу для здоровья, для настроения, для духовного опыта",
        [{"title": "читать духовную книгу", "day": TARGET_DAY.isoformat(), "start_minute": 1140, "end_minute": None, "duration_minutes": 60, "why": "для здоровья, для настроения, для духовного опыта", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "two_tasks",
        "завтра утром сделать зарядку для здоровья и вечером почитать книгу для отдыха",
        [
            {"title": "сделать зарядку", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для здоровья", "period": "morning", "relation": None, "anchor": None, "urgent": False},
            {"title": "почитать книгу", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для отдыха", "period": "evening", "relation": None, "anchor": None, "urgent": False},
        ],
    ),
    Case(
        "three_tasks",
        "завтра утром зарядка, днём заниматься проектом, вечером прогулка для отдыха",
        [
            {"title": "зарядка", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": None, "period": "morning", "relation": None, "anchor": None, "urgent": False},
            {"title": "заниматься проектом", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": None, "period": "day", "relation": None, "anchor": None, "urgent": False},
            {"title": "прогулка", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для отдыха", "period": "evening", "relation": None, "anchor": None, "urgent": False},
        ],
    ),
    Case(
        "ninety_minutes",
        "завтра в 20:00 заниматься проектом полтора часа для практики",
        [{"title": "заниматься проектом", "day": TARGET_DAY.isoformat(), "start_minute": 1200, "end_minute": None, "duration_minutes": 90, "why": "для практики", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "mixed_duration",
        "завтра в 16 часов работать над портфолио 1 час 30 минут для резюме",
        [{"title": "работать над портфолио", "day": TARGET_DAY.isoformat(), "start_minute": 960, "end_minute": None, "duration_minutes": 90, "why": "для резюме", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "colloquial",
        "мне завтра надо в восемь вечера почитать книгу чтобы расслабиться",
        [{"title": "почитать книгу", "day": TARGET_DAY.isoformat(), "start_minute": 1200, "end_minute": None, "duration_minutes": 60, "why": "чтобы расслабиться", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "filler",
        "ну короче завтра после ужина я хочу ну это подготовить план на завтра для порядка",
        [{"title": "подготовить план на завтра", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для порядка", "period": None, "relation": "after", "anchor": "dinner", "urgent": False}],
    ),
    Case(
        "anchor_and_why",
        "завтра после завтрака почитать статьи для работы и для развития",
        [{"title": "почитать статьи", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для работы и для развития", "period": None, "relation": "after", "anchor": "breakfast", "urgent": False}],
    ),
    Case(
        "night_period",
        "завтра ночью написать дневник успеха для спокойствия",
        [{"title": "написать дневник успеха", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для спокойствия", "period": "night", "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "no_why",
        "завтра в 11 проверить документы",
        [{"title": "проверить документы", "day": TARGET_DAY.isoformat(), "start_minute": 660, "end_minute": None, "duration_minutes": 60, "why": None, "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "spiritual",
        "завтра в 21 помолиться и поблагодарить за день для спокойствия",
        [{"title": "помолиться и поблагодарить за день", "day": TARGET_DAY.isoformat(), "start_minute": 1260, "end_minute": None, "duration_minutes": 60, "why": "для спокойствия", "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "health_mood",
        "завтра вечером прогуляться для здоровья, настроения и хорошего сна",
        [{"title": "прогуляться", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для здоровья, настроения и хорошего сна", "period": "evening", "relation": None, "anchor": None, "urgent": False}],
    ),
    Case(
        "relation_window",
        "завтра после обеда и перед ужином поработать над проектом для опыта",
        [{"title": "поработать над проектом", "day": TARGET_DAY.isoformat(), "start_minute": None, "end_minute": None, "duration_minutes": None, "why": "для опыта", "period": None, "relation": "after", "anchor": "lunch", "relation_end": "before", "anchor_end": "dinner", "urgent": False}],
    ),
    Case(
        "explicit_interval_colloquial",
        "завтра с восьми вечера до девяти вечера читать книгу",
        [{"title": "читать книгу", "day": TARGET_DAY.isoformat(), "start_minute": 1200, "end_minute": 1260, "duration_minutes": 60, "why": None, "period": None, "relation": None, "anchor": None, "urgent": False}],
    ),
)


def _norm(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return " ".join(value.casefold().replace("ё", "е").split()).strip(" ,.;:-")
    return value


def _semantic_value(field: str, value: Any) -> Any:
    value = _norm(value)
    if field == "duration_minutes" and value == CONFIG.default_duration_minutes:
        # The scheduler applies the configured default when duration is omitted.
        return None
    if field == "why" and isinstance(value, str):
        value = re.sub(r"^(?:для\s+того\s+чтобы|для|чтобы)\s+", "", value)
        parts = re.split(r"\s*,\s*|\s+и\s+", value)
        if len(parts) > 1:
            parts = [
                re.sub(r"^(?:для\s+того\s+чтобы|для|чтобы)\s+", "", part).strip()
                for part in parts
            ]
            value = tuple(parts)
    if field == "anchor" and isinstance(value, str):
        value = {
            "завтрак": "breakfast",
            "завтрака": "breakfast",
            "обед": "lunch",
            "обеда": "lunch",
            "ужин": "dinner",
            "ужина": "dinner",
        }.get(value, value)
    return value


def _field_equal(field: str, actual: Any, expected: Any) -> bool:
    return _semantic_value(field, actual) == _semantic_value(field, expected)


def _projected_task(task: Any) -> dict[str, Any]:
    period_map = {
        "утро": "morning",
        "утром": "morning",
        "день": "day",
        "днем": "day",
        "днём": "day",
        "вечер": "evening",
        "вечером": "evening",
        "ночь": "night",
        "ночью": "night",
    }
    return {
        "title": task.title,
        "day": task.day.isoformat(),
        "start_minute": task.start_minute,
        "end_minute": task.end_minute,
        "duration_minutes": task.duration_minutes,
        "why": task.why,
        "period": period_map.get(task.period, task.period),
        "relation": task.relation,
        "anchor": task.anchor,
        "relation_end": task.relation_end,
        "anchor_end": task.anchor_end,
        "urgent": task.urgent,
    }


FIELD_WEIGHTS = {
    "title": 2,
    "day": 1,
    "start_minute": 1,
    "end_minute": 1,
    "duration_minutes": 1,
    "why": 2,
    "period": 1,
    "relation": 1,
    "anchor": 1,
    "relation_end": 1,
    "anchor_end": 1,
    "urgent": 1,
}


def _task_score(actual: dict[str, Any], expected: dict[str, Any]) -> tuple[int, int]:
    earned = 0
    for field, weight in FIELD_WEIGHTS.items():
        if field == "end_minute" and expected.get(field) is None:
            start = actual.get("start_minute")
            duration = actual.get("duration_minutes")
            end = actual.get("end_minute")
            equivalent_implied_end = (
                start is not None
                and duration is not None
                and end == start + duration
            )
            if end is None or equivalent_implied_end:
                earned += weight
            continue
        if _field_equal(field, actual.get(field), expected.get(field)):
            earned += weight
    return earned, sum(FIELD_WEIGHTS.values())


def _score_case(actual: list[Any] | None, expected: list[dict[str, Any]]) -> tuple[int, int]:
    possible = 1 + sum(FIELD_WEIGHTS.values()) * len(expected)
    if actual is None:
        return 0, possible
    earned = 1 if len(actual) == len(expected) else 0
    actual_tasks = [_projected_task(x) for x in actual]
    unused = set(range(len(actual_tasks)))
    for expected_task in expected:
        best_index = None
        best_score = -1
        for index in unused:
            score, _ = _task_score(actual_tasks[index], expected_task)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index is not None:
            unused.remove(best_index)
            earned += best_score
    return earned, possible


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, math.ceil(0.95 * len(values)) - 1)]


def _normalize_cloudflare_result(result: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    raw_tasks = result.get("tasks")
    if not isinstance(raw_tasks, list):
        return normalized

    tasks: list[Any] = []
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


async def _cloudflare_call(
    *,
    token: str,
    account_id: str,
    model: str,
    prompt: str,
    timeout: float,
) -> tuple[dict[str, Any], float, int]:
    started = time.perf_counter()
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 512,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": _strict_schema(TASK_SCHEMA),
                },
            },
            timeout=timeout,
        )
    latency = time.perf_counter() - started
    response.raise_for_status()
    body = response.json()
    result = body.get("result")
    if not isinstance(result, dict):
        raise TypeError("Cloudflare response.result is not an object")

    raw = result.get("response")
    if isinstance(raw, dict):
        return raw, latency, response.status_code
    if isinstance(raw, str):
        return json.loads(raw), latency, response.status_code

    choices = result.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, dict):
                    return content, latency, response.status_code
                if isinstance(content, str):
                    return json.loads(content), latency, response.status_code

    raise ValueError("Cloudflare returned no structured response")


async def _run_case(
    name: str,
    provider: Any,
    case: Case,
    *,
    timeout: float,
) -> tuple[list[Any] | None, dict[str, Any]]:
    started = time.perf_counter()
    try:
        prompt = (
            _cloudflare_task_prompt(case.text, today=TODAY, target_day=TARGET_DAY)
            if name == "cloudflare"
            else _task_prompt(case.text, today=TODAY, target_day=TARGET_DAY)
        )
        if name == "cloudflare":
            operation = _cloudflare_call(
                token=provider["token"],
                account_id=provider["account"],
                model=provider["model"],
                prompt=prompt,
                timeout=timeout,
            )
            raw, latency, status = await asyncio.wait_for(operation, timeout=timeout)
            tasks = _parse_tasks_result(
                _normalize_cloudflare_result(raw),
                provider_name="Cloudflare",
                source_text=case.text,
                today=TODAY,
            )
            validate_task_intents(
                tasks,
                source_text=case.text,
                today=TODAY,
                config=CONFIG,
            )
            return tasks, {
                "ok": True,
                "latency_ms": round(latency * 1000, 1),
                "status": status,
                "error": None,
            }

        operation = provider.extract_tasks(
            case.text,
            today=TODAY,
            target_day=TARGET_DAY,
            config=CONFIG,
        )
        tasks = await asyncio.wait_for(operation, timeout=timeout)
        validate_task_intents(
            tasks,
            source_text=case.text,
            today=TODAY,
            config=CONFIG,
        )
        return tasks, {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "status": None,
            "error": None,
        }
    except asyncio.TimeoutError:
        return None, {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "status": "timeout",
            "error": f"hard timeout after {timeout:.1f}s",
        }
    except PlannerAIError as exc:
        return None, {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "status": getattr(exc, "status_code", None),
            "error": str(exc),
        }
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as exc:
        response = getattr(exc, "response", None)
        return None, {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "status": response.status_code if response is not None else None,
            "error": str(exc),
        }


def _cloudflare_task_prompt(
    text: str,
    *,
    today: date,
    target_day: date,
) -> str:
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
        "relation='after', anchor='dinner'. "
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


def _providers(env_file: Path) -> dict[str, Any]:
    load_dotenv(env_file, override=False)
    result: dict[str, Any] = {}
    if os.getenv("GROQ_API_KEY"):
        result["groq"] = GroqPlannerAI(
            os.environ["GROQ_API_KEY"],
            model=os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b"),
            timeout_seconds=35,
        )
    if os.getenv("GEMINI_API_KEY"):
        result["gemini_current"] = GeminiPlannerAI(
            os.environ["GEMINI_API_KEY"],
            model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            timeout_seconds=35,
        )
        result["gemini_flash_lite"] = GeminiPlannerAI(
            os.environ["GEMINI_API_KEY"],
            model=os.getenv("GEMINI_BENCHMARK_LITE_MODEL", "gemini-3.5-flash-lite"),
            timeout_seconds=35,
        )
    if os.getenv("CLOUDFLARE_API_TOKEN") and os.getenv("CLOUDFLARE_ACCOUNT_ID"):
        result["cloudflare"] = {
            "token": os.environ["CLOUDFLARE_API_TOKEN"],
            "account": os.environ["CLOUDFLARE_ACCOUNT_ID"],
            "model": os.getenv(
                "CLOUDFLARE_MODEL",
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            ),
        }
    return result


async def main_async(args: argparse.Namespace) -> None:
    available = _providers(args.env_file)
    if args.provider == "all":
        providers = available
    else:
        if args.provider not in available:
            configured = ", ".join(available) or "none"
            raise SystemExit(
                f"Provider {args.provider!r} не настроен. Доступны: {configured}"
            )
        providers = {args.provider: available[args.provider]}

    print(f"cases={len(CASES)} repeat={args.repeat} timeout={args.timeout:.1f}s")
    print("providers=" + ", ".join(providers))

    all_results: dict[str, list[dict[str, Any]]] = {name: [] for name in providers}

    selected_cases = CASES[args.start : args.start + args.limit]
    if not selected_cases:
        raise SystemExit("Диапазон benchmark cases пуст.")
    print(
        f"cases_selected={len(selected_cases)} "
        f"range={args.start + 1}-{args.start + len(selected_cases)}"
    )

    for round_number in range(1, args.repeat + 1):
        print(f"\nROUND {round_number}/{args.repeat}")
        for case_number, case in enumerate(selected_cases, args.start + 1):
            for name, provider in providers.items():
                tasks, meta = await _run_case(name, provider, case, timeout=args.timeout)
                earned, possible = _score_case(tasks, case.expected)
                record = {
                    "round": round_number,
                    "case": case.name,
                    "ok": meta["ok"],
                    "latency_ms": meta["latency_ms"],
                    "status": meta["status"],
                    "error": meta["error"],
                    "score": earned,
                    "score_max": possible,
                    "tasks": [_projected_task(x) for x in tasks] if tasks is not None else None,
                }
                all_results[name].append(record)
            print(f"[{case_number:02d}/{len(CASES)}] {case.name}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "today": TODAY.isoformat(),
                "target_day": TARGET_DAY.isoformat(),
                "case_count": len(selected_cases),
                "case_start": args.start,
                "repeat": args.repeat,
                "providers": {
                    name: (
                        {
                            "provider": provider.provider_name,
                            "model": provider.model,
                        }
                        if name != "cloudflare"
                        else {
                            "provider": "Cloudflare",
                            "model": provider["model"],
                        }
                    )
                    for name, provider in providers.items()
                },
                "results": all_results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\nSUMMARY")
    for name, records in all_results.items():
        successful = [record for record in records if record["ok"]]
        exact_cases = [
            record for record in successful
            if record["score"] == record["score_max"]
        ]
        latencies = [record["latency_ms"] for record in successful]
        score = sum(record["score"] for record in successful)
        possible = sum(record["score_max"] for record in successful)
        error_counts: dict[str, int] = {}
        for record in records:
            key = str(record["status"]) if record["status"] else ("error" if record["error"] else "ok")
            error_counts[key] = error_counts.get(key, 0) + 1
        print(
            f"{name:<20} quality={score / possible * 100 if possible else 0:6.1f}% "
            f"exact={len(exact_cases) / len(successful) * 100 if successful else 0:6.1f}% "
            f"success={len(successful) / len(records) * 100:6.1f}% "
            f"p50={statistics.median(latencies) if latencies else 0:7.0f}ms "
            f"p95={_p95(latencies):7.0f}ms "
            f"errors={error_counts}"
        )
        for record in records:
            if record["score"] != record["score_max"]:
                print(
                    f"  mismatch {record['case']}: "
                    f"{record['score']}/{record['score_max']} "
                    f"tasks={len(record['tasks'] or [])}/"
                    f"{len(next(case.expected for case in CASES if case.name == record['case']))} "
                    f"status={record['status']!r}"
                )
                actual_tasks = record["tasks"] or []
                expected_tasks = next(
                    case.expected for case in selected_cases if case.name == record["case"]
                )
                for index, expected_task in enumerate(expected_tasks):
                    actual_task = actual_tasks[index] if index < len(actual_tasks) else None
                    if actual_task is None:
                        print(f"    task[{index}]: missing actual task")
                        continue
                    mismatches = [
                        field
                        for field in expected_task
                        if not _field_equal(
                            field,
                            actual_task.get(field),
                            expected_task.get(field),
                        )
                    ]
                    if mismatches:
                        details = ", ".join(
                            f"{field}={actual_task.get(field)!r} != {expected_task.get(field)!r}"
                            for field in mismatches
                        )
                        print(f"    task[{index}]: {details}")
    print(f"results={output}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=len(CASES))
    parser.add_argument(
        "--provider",
        choices=(
            "all",
            "groq",
            "gemini_current",
            "gemini_flash_lite",
            "cloudflare",
        ),
        default="local",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--env-file", type=Path, default=Path("/opt/voqelis/.env"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/run/voqelis/planner-ai-benchmark.json"),
    )
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
