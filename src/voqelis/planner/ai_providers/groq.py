"""Groq structured extraction adapter."""

from __future__ import annotations

import json

import httpx

from ..ai_extraction import _StructuredPlannerAI
from ..ai_http import _provider_http_error, _strict_schema
from ..models import PlannerAIInvalidResponse, PlannerAIProviderError, PlannerAIUnavailable


class GroqPlannerAI(_StructuredPlannerAI):
    provider_name = "Groq"

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        timeout_seconds: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not api_key.strip():
            raise PlannerAIUnavailable("GROQ_API_KEY is not configured")
        self.api_key = api_key.strip()
        self.model = model.strip()
        if not self.model:
            raise PlannerAIUnavailable("GROQ_MODEL is not configured")
        self.timeout = httpx.Timeout(timeout_seconds)
        self.transport = transport

    async def _json_call(self, prompt: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "voqelis_structured_output",
                    "strict": True,
                    "schema": _strict_schema(schema),
                },
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise PlannerAIProviderError(
                "Groq request timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise PlannerAIProviderError(
                "Groq request failed",
                retryable=True,
            ) from exc

        if response.status_code >= 400:
            raise _provider_http_error("Groq", response)

        try:
            raw = response.json()["choices"][0]["message"]["content"]
            result = json.loads(raw)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise PlannerAIInvalidResponse(
                "Groq returned invalid structured JSON"
            ) from exc

        if not isinstance(result, dict):
            raise PlannerAIInvalidResponse("Groq returned non-object JSON")
        return result
