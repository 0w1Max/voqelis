"""Gemini structured extraction adapter."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from ..ai_extraction import _StructuredPlannerAI
from ..ai_http import _provider_http_error
from ..models import PlannerAIInvalidResponse, PlannerAIProviderError, PlannerAIUnavailable

logger = logging.getLogger(__name__)


class GeminiPlannerAI(_StructuredPlannerAI):
    provider_name = "Gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_seconds: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key.strip():
            raise PlannerAIUnavailable("GEMINI_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = model.strip()
        if not self.model:
            raise PlannerAIUnavailable("GEMINI_MODEL is not configured")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def _json_call(self, prompt: str, schema: dict) -> dict:
        url = "https://generativelanguage.googleapis.com/v1beta/interactions"
        payload = {
            "model": self.model,
            "input": prompt,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
        }
        response = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    transport=self.transport,
                ) as client:
                    response = await client.post(
                        url,
                        headers={"x-goog-api-key": self.api_key},
                        json=payload,
                    )
            except httpx.TimeoutException as exc:
                raise PlannerAIProviderError(
                    "Gemini request timed out",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise PlannerAIProviderError(
                    "Gemini request failed",
                    retryable=True,
                ) from exc

            if response.status_code not in {502, 503, 504} or attempt == 1:
                break
            logger.warning(
                "PLANNER_AI_GEMINI_RETRY status=%s",
                response.status_code,
            )
            await asyncio.sleep(1.0)

        assert response is not None
        if response.status_code >= 400:
            raise _provider_http_error("Gemini", response)

        try:
            body = response.json()
            raw = body.get("output_text")
            if not isinstance(raw, str) or not raw.strip():
                for step in body.get("steps", []) or []:
                    if not isinstance(step, dict):
                        continue
                    content = step.get("content")
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if isinstance(item, dict) and isinstance(item.get("text"), str):
                            raw = item["text"]
                            break
                    if isinstance(raw, str) and raw.strip():
                        break
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("missing Gemini text output")
            result = json.loads(raw)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PlannerAIInvalidResponse(
                "Gemini returned invalid structured JSON"
            ) from exc

        if not isinstance(result, dict):
            raise PlannerAIInvalidResponse("Gemini returned non-object JSON")
        return result
