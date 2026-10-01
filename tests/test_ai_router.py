from __future__ import annotations

from datetime import date

import httpx
import pytest

from planner_ai_benchmark import (
    _cloudflare_task_prompt,
    _field_equal,
    _normalize_cloudflare_result,
)
from voqelis.planner.ai import (
    TASK_SCHEMA,
    AIProviderRouter,
    CloudflarePlannerAI,
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
async def test_router_uses_tertiary_after_primary_and_fallback_fail():
    primary = FakeProvider(
        error=PlannerAIProviderError("primary down", status_code=429, retry_after_seconds=60)
    )
    fallback = FakeProvider(error=PlannerAIProviderError("fallback down", status_code=503))
    tertiary = FakeProvider([task("tertiary")])
    router = AIProviderRouter(
        primary=primary,
        fallback=fallback,
        tertiary=tertiary,
    )

    result = await router.extract_tasks(
        "завтра тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )

    assert result == [task("tertiary")]
    assert primary.calls == 1
    assert fallback.calls == 1
    assert tertiary.calls == 1


@pytest.mark.asyncio
async def test_router_keeps_tertiary_cooldown_independent():
    primary = FakeProvider([task("primary")])
    fallback = FakeProvider([task("fallback")])
    tertiary = FakeProvider(
        error=PlannerAIProviderError("tertiary quota", status_code=429, retry_after_seconds=600)
    )
    router = AIProviderRouter(
        primary=primary,
        fallback=fallback,
        tertiary=tertiary,
    )

    await router.extract_tasks(
        "завтра тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )
    assert tertiary.calls == 0

    primary.error = PlannerAIProviderError(
        "primary quota", status_code=429, retry_after_seconds=600
    )
    result = await router.extract_tasks(
        "завтра ещё тест",
        today=date(2026, 9, 25),
        target_day=date(2026, 9, 26),
        config=PlannerConfig(),
    )
    assert result == [task("fallback")]
    assert tertiary.calls == 0


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


def test_benchmark_treats_why_list_conjunction_as_equivalent():
    assert _field_equal(
        "why",
        "для работы, для развития",
        "для работы и для развития",
    )
    assert _field_equal(
        "why",
        "для здоровья, для настроения, для духовного опыта",
        "для здоровья и для настроения и для духовного опыта",
    )


def test_cloudflare_normalizes_equal_start_end_without_explicit_duration():
    result = {
        "tasks": [
            {
                "title": "подготовка ко сну",
                "day": "2026-10-02",
                "start_time": "00:00",
                "end_time": "00:00",
                "duration_minutes": None,
            }
        ]
    }

    normalized = _normalize_cloudflare_result(result)

    assert normalized["tasks"][0]["start_time"] == "00:00"
    assert normalized["tasks"][0]["end_time"] is None
    assert normalized["tasks"][0]["duration_minutes"] is None
    assert result["tasks"][0]["end_time"] == "00:00"


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
    assert "A period is not an exact clock time" in prompt
    assert "must have title='сделать домашку по шагам'" in prompt
    assert "do not drop meaningful phrases such as 'на завтра'" in prompt


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
async def test_cloudflare_structured_output_is_parsed_without_network():
    payload = {
        "success": True,
        "result": {
            "response": {
                "tasks": [{
                    "title": "позвонить в сервис",
                    "day": "2026-10-02",
                    "start_time": "14:00",
                    "end_time": None,
                    "duration_minutes": None,
                    "period": None,
                    "preferred_time": None,
                    "relation": None,
                    "anchor": None,
                    "why": "чтобы решить проблему",
                    "urgent": True,
                }]
            }
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.cloudflare.com"
        assert "/accounts/test-account/ai/run/" in str(request.url)
        assert request.headers["authorization"] == "Bearer test-token"
        body = request.read()
        assert b"response_format" in body
        return httpx.Response(200, json=payload)

    ai = CloudflarePlannerAI(
        "test-token",
        account_id="test-account",
        model="@cf/meta/test",
        transport=httpx.MockTransport(handler),
    )
    result = await ai.extract_tasks(
        "завтра срочно в 14 часов позвонить в сервис чтобы решить проблему",
        today=date(2026, 10, 1),
        target_day=date(2026, 10, 2),
        config=PlannerConfig(),
    )

    assert result[0].start_minute == 14 * 60
    assert result[0].duration_minutes == 60
    assert result[0].urgent is True
    assert result[0].why == "чтобы решить проблему"


@pytest.mark.asyncio
async def test_cloudflare_daily_quota_429_sets_cooldown_until_utc_midnight():
    payload = {
        "errors": [{
            "message": "daily free allocation exhausted",
            "code": 4006,
        }],
        "success": False,
        "result": {},
        "messages": [],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, json=payload)

    ai = CloudflarePlannerAI(
        "test-token",
        account_id="test-account",
        model="@cf/meta/test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PlannerAIProviderError) as exc_info:
        await ai.extract_tasks(
            "завтра купить продукты",
            today=date(2026, 10, 1),
            target_day=date(2026, 10, 2),
            config=PlannerConfig(),
        )

    error = exc_info.value
    assert error.status_code == 429
    assert error.retryable is True
    assert error.retry_after_seconds is not None
    assert 0 < error.retry_after_seconds <= 24 * 60 * 60


@pytest.mark.asyncio
async def test_cloudflare_normalizes_zero_length_exact_time():
    payload = {
        "result": {
            "choices": [{
                "message": {
                    "content": (
                        '{"tasks":[{"title":"подготовка ко сну","day":"2026-10-02",'
                        '"start_time":"00:00","end_time":"00:00","duration_minutes":null,'
                        '"period":null,"preferred_time":null,"relation":null,'
                        '"anchor":null,"why":null,"urgent":false}]}'
                    )
                }
            }]
        }
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    ai = CloudflarePlannerAI(
        "test-token",
        account_id="test-account",
        model="@cf/meta/test",
        transport=httpx.MockTransport(handler),
    )
    result = await ai.extract_tasks(
        "завтра в 12 ночи подготовка ко сну",
        today=date(2026, 10, 1),
        target_day=date(2026, 10, 2),
        config=PlannerConfig(),
    )

    assert result[0].start_minute == 0
    assert result[0].end_minute is None
    assert result[0].duration_minutes == 60


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
