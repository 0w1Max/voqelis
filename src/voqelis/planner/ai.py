"""Compatibility facade for Planner AI.

Existing callers keep importing from voqelis.planner.ai; implementations live
in focused modules with acyclic dependencies.
"""

from .ai_contracts import PlannerAI
from .ai_extraction import (
    FULL_REVIEW_SCHEMA,
    REVIEW_SCHEMA,
    TASK_SCHEMA,
    _clean_task_title,
    _extract_full_review_result,
    _extract_review_result,
    _optional_string,
    _parse_tasks_result,
    _parse_time,
    _StructuredPlannerAI,
    _task_prompt,
    _validate_full_review_item,
    _validate_task_draft,
)
from .ai_http import (
    _cloudflare_provider_http_error,
    _parse_retry_after_value,
    _provider_http_error,
    _retry_after_seconds,
    _seconds_until_next_utc_midnight,
    _strict_schema,
)
from .ai_providers import CloudflarePlannerAI, GeminiPlannerAI, GroqPlannerAI
from .ai_providers.cloudflare import (
    _cloudflare_task_prompt,
    _normalize_cloudflare_task_result,
)
from .ai_router import AIProviderRouter

__all__ = [
    "FULL_REVIEW_SCHEMA",
    "REVIEW_SCHEMA",
    "TASK_SCHEMA",
    "AIProviderRouter",
    "CloudflarePlannerAI",
    "GeminiPlannerAI",
    "GroqPlannerAI",
    "PlannerAI",
    "_StructuredPlannerAI",
    "_clean_task_title",
    "_cloudflare_provider_http_error",
    "_cloudflare_task_prompt",
    "_extract_full_review_result",
    "_extract_review_result",
    "_normalize_cloudflare_task_result",
    "_optional_string",
    "_parse_retry_after_value",
    "_parse_tasks_result",
    "_parse_time",
    "_provider_http_error",
    "_retry_after_seconds",
    "_seconds_until_next_utc_midnight",
    "_strict_schema",
    "_task_prompt",
    "_validate_full_review_item",
    "_validate_task_draft",
]
