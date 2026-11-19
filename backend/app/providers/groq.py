"""Groq LLM adapter (OpenAI-compatible chat completions over HTTPS).

Facts verified on the owner's account on 2026-11-19 (docs/PROVIDERS.md §7): 1,000 requests/day and 8,000 tokens/minute
**per model**, strict JSON-schema output works, remaining quota arrives in ``x-ratelimit-*`` headers. Because the quotas
are per model, the two models are registered as two providers on the same single account/key:

* ``groq-smart`` — drafting and other quality-sensitive work (``groq_model``)
* ``groq-fast``  — classification / extraction (``groq_fast_model``)

That is not quota-dodging: they are separate, documented allowances of one account, and a call asking for a tier is
never served by the other tier.

Data handling: Groq's Services Agreement §4.2 forbids using Inputs or Outputs for training, and inference data is not
retained by default, so these providers are cleared for prospect personal data. Prompts and completions are never logged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from pydantic import SecretStr

from app.core.clock import Clock
from app.core.redact import scrub_error
from app.domain.enums import Capability
from app.providers.errors import (
    AuthError,
    ForbiddenReason,
    InvalidResponseError,
    PermissionDeniedError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitedError,
    TransientProviderError,
    error_from_status,
    parse_retry_after,
)
from app.providers.llm import LLMProvider, LLMRequest, LLMResponse
from app.providers.spec import ProviderResult, ProviderSpec

log = structlog.get_logger("providers.groq")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
_DAILY_QUOTA = re.compile(r"per day|\((?:RPD|TPD)\)", re.IGNORECASE)
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
# "...Please try again in 7.66s." — only whole duration tokens, so the sentence's closing period is not swallowed.
_TRY_AGAIN = re.compile(r"try again in ((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
# Error codes that mean "this key/plan cannot use this model" — a configuration problem, not a bad request.
_MODEL_UNAVAILABLE_CODES = frozenset(
    {
        "model_not_found",
        "model_decommissioned",
        "model_permission_blocked_project",
        "model_terms_required",
    }
)


def parse_duration_seconds(value: str | None) -> float | None:
    """Go-style durations as sent in rate-limit headers: ``7.66s``, ``2m59.56s``, ``1h2m``, ``150ms``."""
    if not value:
        return None
    text = value.strip()
    total, position = 0.0, 0
    for match in _DURATION_PART.finditer(text):
        if match.start() != position:
            return None
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        position = match.end()
    return total if position == len(text) and position > 0 else None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:
        return None


class GroqProvider(LLMProvider):
    def __init__(
        self,
        *,
        key: str,
        model: str,
        tier: str,
        display_name: str,
        api_key: SecretStr | None,
        client: httpx.AsyncClient,
        clock: Clock,
        default_priority: int = 10,
        daily_limit: int = 1000,
        tokens_per_minute: int = 8000,
        base_url: str = GROQ_BASE_URL,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Groq base URL must be HTTPS")
        self.spec = ProviderSpec(
            key,
            Capability.LLM,
            display_name,
            free_tier=True,
            default_priority=default_priority,
            daily_limit=daily_limit,
            rate_limit_per_minute=tokens_per_minute,
            pacing_unit="tokens",
            may_receive_personal_data=True,
            tier=tier,
        )
        self._model = model
        self._api_key = api_key
        self._client = client
        self._clock = clock
        self._url = f"{base_url.rstrip('/')}/chat/completions"

    @property
    def configured(self) -> bool:
        return self._api_key is not None and bool(self._api_key.get_secret_value())

    @property
    def model(self) -> str:
        return self._model

    # ───────────────────────────── the call ─────────────────────────────

    async def complete(self, request: LLMRequest) -> ProviderResult[LLMResponse]:
        if self._api_key is None or not self._api_key.get_secret_value():
            raise AuthError("GROQ_API_KEY is not configured")
        secret = self._api_key.get_secret_value()
        try:
            response = await self._client.post(
                self._url,
                json=self._body(request),
                headers={"Authorization": f"Bearer {secret}"},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # Failed before anything was sent: always safe to try again.
            raise TransientProviderError(f"cannot reach Groq: {type(exc).__name__}") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(
                f"connection to Groq failed: {type(exc).__name__}",
                request_may_have_been_processed=True,
            ) from exc

        if response.status_code != 200:
            raise self._error_for(response, secret)
        return self._result_for(response, request)

    def _body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_completion_tokens": request.max_tokens,
            "reasoning_effort": request.reasoning_effort,
            "include_reasoning": False,  # never receive (or store) the model's chain of thought
        }
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": dict(request.json_schema),
                },
            }
        return body

    # ───────────────────────────── success path ─────────────────────────────

    def _result_for(
        self, response: httpx.Response, request: LLMRequest
    ) -> ProviderResult[LLMResponse]:
        try:
            data = response.json()
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish_reason = str(choice.get("finish_reason") or "")
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise InvalidResponseError(
                "Groq returned a response without a usable completion"
            ) from exc
        if not isinstance(text, str) or not text.strip():
            # gpt-oss spends its budget on hidden reasoning when max tokens is too small → empty content.
            raise InvalidResponseError(
                f"Groq returned an empty completion (finish_reason={finish_reason!r})"
            )

        parsed: Any | None = None
        if request.json_schema is not None:
            if finish_reason == "length":
                raise InvalidResponseError("completion was cut off before the JSON was complete")
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise InvalidResponseError(
                    "completion was not valid JSON despite a strict schema"
                ) from exc

        prompt_tokens, completion_tokens = (
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )
        headers = response.headers
        reset_seconds = parse_duration_seconds(headers.get("x-ratelimit-reset-requests"))
        value = LLMResponse(
            text=text,
            parsed=parsed,
            model=str(data.get("model") or self._model),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )
        log.info(
            "groq.completed",
            provider=self.spec.key,
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason=finish_reason,
        )
        return ProviderResult(
            value=value,
            units_used=Decimal(prompt_tokens + completion_tokens),
            # Groq's *request* headers describe the daily request allowance (RPD): real, provider-reported figures.
            remaining=_decimal(headers.get("x-ratelimit-remaining-requests")),
            limit=_decimal(headers.get("x-ratelimit-limit-requests")),
            reset_at=None
            if reset_seconds is None
            else self._clock.now() + timedelta(seconds=reset_seconds),
        )

    # ───────────────────────────── error path ─────────────────────────────

    def _error_for(self, response: httpx.Response, secret: str) -> ProviderError:
        status = response.status_code
        message, code = self._error_details(response)
        message = scrub_error(message, known_secrets=(secret,))[:300]
        now = self._clock.now()
        header_wait = parse_retry_after(response.headers.get("retry-after"), now=now)

        if status == 429:
            hint = _TRY_AGAIN.search(message)
            wait = (
                header_wait
                if header_wait is not None
                else parse_duration_seconds(hint.group(1) if hint else None)
            )
            if _DAILY_QUOTA.search(message):
                reset = None if wait is None else now + timedelta(seconds=wait)
                return QuotaExhaustedError(message, reset_at=reset, http_status=status)
            return RateLimitedError(message, retry_after=wait, http_status=status)
        if code in _MODEL_UNAVAILABLE_CODES:
            return PermissionDeniedError(
                message, reason=ForbiddenReason.PERMISSIONS, http_status=status
            )
        return error_from_status(status, message, retry_after=header_wait)

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[str, str | None]:
        try:
            body: Mapping[str, Any] = response.json()
            error = body.get("error")
            if isinstance(error, Mapping):
                return str(error.get("message") or f"HTTP {response.status_code}"), (
                    str(error["code"]) if error.get("code") else None
                )
        except ValueError:
            pass
        return f"HTTP {response.status_code}", None


def build_groq_providers(
    *,
    api_key: SecretStr | None,
    smart_model: str,
    fast_model: str,
    client: httpx.AsyncClient,
    clock: Clock,
) -> list[GroqProvider]:
    """The two Groq providers (separate per-model quotas, one account/key)."""
    return [
        GroqProvider(
            key="groq-smart",
            model=smart_model,
            tier="smart",
            display_name="Groq (smart)",
            api_key=api_key,
            client=client,
            clock=clock,
        ),
        GroqProvider(
            key="groq-fast",
            model=fast_model,
            tier="fast",
            display_name="Groq (fast)",
            api_key=api_key,
            client=client,
            clock=clock,
        ),
    ]
