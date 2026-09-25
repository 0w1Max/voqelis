from __future__ import annotations

from datetime import date

import pytest

from voqelis.planner.ai import TASK_SCHEMA, AIProviderRouter, _strict_schema
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


def test_strict_schema_closes_nested_objects():
    schema = _strict_schema(TASK_SCHEMA)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tasks"]["items"]["additionalProperties"] is False
