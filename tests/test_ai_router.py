from __future__ import annotations

from datetime import date

import httpx
import pytest

from planner_ai_benchmark import _cloudflare_task_prompt
from voqelis.planner.ai import (
    TASK_SCHEMA,
    AIProviderRouter,
    GeminiPlannerAI,
    GroqPlannerAI,
    _parse_time,
    _strict_schema,
)
from voqelis.planner.config import PlannerConfig
from voqelis.planner.models import (
    PlannerAIInvalidResponse,
    PlannerAIProviderError,
    TaskDraft,
)


class FakeProvider:
    provider_name = "fake"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    async def extract_tasks(self, text, *, today, target_day, config):
        del text, today, target_day, config
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or []

    async def extract_review(self, text, *, task_title):
        del text, task_title
        self.calls += 1
        if self.error:
            raise self.error
        return None, (), None

    async def extract_full_review(self, text, *, items):
        del text, items
        self.calls += 1
        if self.error:
            raise self.error
        return []


def task(title="test"):
    return TaskDraft(title=title, day=date(2026, 9, 26))


@pytest.mark.asyncio
async def test_router_prefers_primary():
    primary = FakeProvider([task("primary")])
    fallback = FakeProvider([task("fallback")])
    router = AIProviderRouter(primary=primary, fallback=fallback)

    result = await router.extract_tasks(
        "завтра тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )

    assert result == [task("primary")]
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_router_switches_after_429_and_keeps_primary_blocked():
    primary = FakeProvider(
        error=PlannerAIProviderError(
            "quota", status_code=429, retry_after_seconds=600, retryable=True
        )
    )
    fallback = FakeProvider([task("fallback")])
    router = AIProviderRouter(primary=primary, fallback=fallback)

    await router.extract_tasks(
        "завтра тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )
    await router.extract_tasks(
        "завтра ещё тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )

    assert primary.calls == 1
    assert fallback.calls == 2


@pytest.mark.asyncio
async def test_router_switches_after_invalid_response():
    primary = FakeProvider(error=PlannerAIInvalidResponse("bad json"))
    fallback = FakeProvider([task("fallback")])
    router = AIProviderRouter(primary=primary, fallback=fallback)

    result = await router.extract_tasks(
        "завтра тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )

    assert result == [task("fallback")]
    assert primary.calls == 1
    assert fallback.calls == 1


def test_cloudflare_prompt_defines_strict_time_semantics():
    prompt = _cloudflare_task_prompt(
        "завтра после обеда прогуляться чтобы отдохнуть",
        today=date(2026, 10, 1),
        target_day=date(2026, 10, 2),
    )

    assert "preferred_time is ONLY an optional clock preference" in prompt
    assert "'после обеда' -> relation='after', anchor='lunch'" in prompt
    assert "Do not invent an anchor" in prompt
    assert "Never return end_time unless start_time is also present" in prompt


def test_strict_schema_closes_nested_objects():
    schema = _strict_schema(TASK_SCHEMA)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tasks"]["items"]["additionalProperties"] is False


def test_parse_time_accepts_provider_time_formats():
    assert _parse_time("20:00") == 20 * 60
    assert _parse_time("20:00:00") == 20 * 60
    assert _parse_time("20:00:00.123+03:00") == 20 * 60
    assert _parse_time("20:00+03:00") == 20 * 60
    assert _parse_time(None) is None


@pytest.mark.asyncio
async def test_groq_structured_output_is_parsed_without_network():
    payload = {
        "choices": [{
            "message": {
                "content": '{"tasks":[{"title":"сходить в магазин","day":"2026-09-26",'
                '"start_time":"20:00:00+03:00","end_time":null,"duration_minutes":60,'
                '"period":null,"preferred_time":null,"relation":null,"anchor":null,'
                '"why":"купить продукты","urgent":false}]}'
            }
        }]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.groq.com"
        body = request.read()
        assert b"response_format" in body
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    ai = GroqPlannerAI(
        "test-key",
        model="qwen/qwen3.8-27b",
        transport=transport,
    )
    result = await ai.extract_tasks(
        "завтра в 8 вечера сходить в магазин на один час, чтобы купить продукты",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )
    assert len(result) == 1
    assert result[0].start_minute == 20 * 60
    assert result[0].duration_minutes == 60
    assert result[0].title == "сходить в магазин"
    assert result[0].why == "купить продукты"


@pytest.mark.asyncio
async def test_gemini_interactions_response_is_parsed():
    payload = {
        "id": "test-interaction",
        "status": "completed",
        "steps": [{
            "type": "model_output",
            "content": [{
                "type": "text",
                "text": "{\"tasks\":[{\"title\":\"сходить в магазин\",\"day\":\"2026-09-26\",\"start_time\":\"20:00\",\"end_time\":null,\"duration_minutes\":60,\"period\":null,\"preferred_time\":null,\"relation\":null,\"anchor\":null,\"why\":\"купить продукты\",\"urgent\":false}]}"
            }]
        }],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path == "/v1beta/interactions"
        body = request.read()
        assert b"\"response_format\"" in body
        assert b"\"mime_type\":\"application/json\"" in body
        return httpx.Response(200, json=payload)

    ai = GeminiPlannerAI(
        "test-key",
        model="gemini-3.8-flash",
        transport=httpx.MockTransport(handler),
    )
    result = await ai.extract_tasks(
        "завтра в 8 вечера сходить в магазин на один час, чтобы купить продукты",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )
    assert result[0].start_minute == 20 * 60
    assert result[0].duration_minutes == 60
    assert result[0].title == "сходить в магазин"


@pytest.mark.asyncio
async def test_gemini_retries_transient_503_once():
    payload = {
        "steps": [{
            "type": "model_output",
            "content": [{
                "type": "text",
                "text": "{\"tasks\":[{\"title\":\"читать книгу\",\"day\":\"2026-09-26\",\"start_time\":\"21:00\",\"end_time\":null,\"duration_minutes\":null,\"period\":null,\"preferred_time\":null,\"relation\":null,\"anchor\":null,\"why\":\"для отдыха\",\"urgent\":false}]}"
            }]
        }],
    }
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == "generativelanguage.googleapis.com"
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=payload)

    ai = GeminiPlannerAI(
        "test-key",
        model="gemini-3.8-flash",
        transport=httpx.MockTransport(handler),
    )
    result = await ai.extract_tasks(
        "завтра в 9 вечера читать книгу для отдыха",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )

    assert calls == 2
    assert result[0].start_minute == 21 * 60
    assert result[0].duration_minutes == 60
    assert result[0].why == "для отдыха"


@pytest.mark.asyncio
async def test_gemini_http_429_is_provider_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "12"})

    ai = GeminiPlannerAI(
        "test-key",
        model="gemini-3.8-flash",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(PlannerAIProviderError) as exc_info:
        await ai.extract_tasks(
            "завтра купить продукты",
            today=date(2026, 9, 25),
            target_day=date(2026, 9, 26),
            config=PlannerConfig(),
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 12
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_router_uses_fallback_when_primary_provider_returns_http_error():
    primary = FakeProvider(
        error=PlannerAIProviderError(
            "server error",
            status_code=503,
            retryable=True,
        )
    )
    fallback = FakeProvider([task("fallback")])
    router = AIProviderRouter(primary=primary, fallback=fallback)
    result = await router.extract_tasks(
        "завтра тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )
    assert result == [task("fallback")]
    assert primary.calls == 1
    assert fallback.calls == 1
