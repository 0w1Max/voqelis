"""Shared provider HTTP and structured-schema error handling."""

from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from datetime import time as datetime_time

import httpx

from .models import PlannerAIProviderError


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


def _parse_retry_after_value(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().lower()

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", value)
    if match is None:
        return None

    amount = float(match.group(1))
    multiplier = {"s": 1.0, "m": 60.0, "h": 3600.0}[match.group(2)]
    return amount * multiplier


def _retry_after_seconds(response: httpx.Response) -> float | None:
    values = [
        _parse_retry_after_value(response.headers.get("retry-after")),
        _parse_retry_after_value(response.headers.get("x-ratelimit-reset-requests")),
        _parse_retry_after_value(response.headers.get("x-ratelimit-reset-tokens")),
    ]
    available = [value for value in values if value is not None]
    return max(available) if available else None


def _provider_http_error(
    provider: str,
    response: httpx.Response,
) -> PlannerAIProviderError:
    status = response.status_code
    return PlannerAIProviderError(
        f"{provider} API returned HTTP {status}",
        status_code=status,
        retry_after_seconds=_retry_after_seconds(response),
        retryable=status == 429 or status in {408, 409, 425} or status >= 500,
    )


def _seconds_until_next_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    next_midnight = datetime.combine(
        now.date() + timedelta(days=1),
        datetime_time.min,
        tzinfo=timezone.utc,
    )
    return max(1.0, (next_midnight - now).total_seconds())


def _cloudflare_provider_http_error(
    response: httpx.Response,
) -> PlannerAIProviderError:
    base = _provider_http_error("Cloudflare", response)

    if response.status_code != 429:
        return base

    try:
        body = response.json()
    except (TypeError, ValueError):
        return base

    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return base

    daily_quota_exhausted = any(
        isinstance(error, dict) and error.get("code") in {4006, "4006"}
        for error in errors
    )
    if not daily_quota_exhausted:
        return base

    return PlannerAIProviderError(
        "Cloudflare daily free allocation exhausted",
        status_code=429,
        retry_after_seconds=_seconds_until_next_utc_midnight(),
        retryable=True,
    )
