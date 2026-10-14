"""Model providers (spec §8.3). Keys come from the environment; request bodies are not logged.

``AnthropicProvider`` posts to the Messages API and forces JSON with one tool whose ``input_schema``
is the role schema and ``tool_choice`` set to that tool. The stable system text and the evidence
prefix carry ``cache_control`` so a repeated evidence block can be read from the prompt cache.
``OpenAIProvider`` posts to the Responses API (``POST /v1/responses``) and sets
``text.format`` to a JSON schema. Neither provider validates the JSON against the schema; the
role does. Retries cover HTTP 429 and 5xx. ``ANTHROPIC_API_KEY`` and ``OPENAI_API_KEY`` are the
environment variables; pass ``api_key`` only from a test.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger("macrosys.llm.provider")

# Messages API version header. This is not a model id; model ids live in config (spec §8.3).
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL = "https://api.openai.com/v1/responses"
_TOOL_NAME = "emit_result"


class ProviderError(RuntimeError):
    """A provider call failed. The message has no API key and no request body (spec §8.3)."""


class ContentBlock(BaseModel):
    """One piece of a role prompt: instruction, evidence, or the output schema (spec §8.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    text: str = Field(min_length=1)
    doc_id: str | None = None

    @model_validator(mode="after")
    def _evidence_is_labeled(self) -> ContentBlock:
        if self.kind not in {"instruction", "evidence", "schema"}:
            raise ValueError("content block kind must be instruction, evidence, or schema")
        if self.kind == "evidence" and not self.doc_id:
            raise ValueError("evidence blocks require a doc_id (spec §8.3 guardrail a)")
        return self


class CompletionRequest(BaseModel):
    """One completion. ``json_schema`` is the role's output contract; the provider does not check it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str = Field(min_length=1)
    blocks: list[ContentBlock]
    json_schema: dict[str, Any]
    model: str = Field(min_length=1)
    max_tokens: int = Field(ge=1)


class CompletionResult(BaseModel):
    """Provider output. ``parsed`` is JSON for the caller to validate (spec §8.3).

    ``input_tokens`` excludes cache reads. ``cache_read_tokens`` is the cached portion, so cost
    accounting can price the two buckets separately. ``latency_s`` is seconds from send to parse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed: dict[str, Any]
    raw_text: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    latency_s: float = Field(ge=0)


@runtime_checkable
class Provider(Protocol):
    """Something that can complete one role request (spec §8.3)."""

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Return parsed JSON plus token counts. The caller validates ``parsed`` against the role schema."""
        ...


def _body_hash(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def _post_json(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    api_key: str,
    provider_name: str,
    model: str,
    max_retries: int,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    """POST JSON. Log the body hash and status, never the body. Retry 429 and 5xx (spec §8.3)."""
    digest = _body_hash(body)
    delay = 0.25
    last_status: int | None = None
    for attempt in range(max_retries + 1):
        try:
            response = client.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            logger.info(
                "llm timeout provider=%s model=%s body_sha256=%s attempt=%s",
                provider_name,
                model,
                digest,
                attempt,
            )
            if attempt >= max_retries:
                raise ProviderError("provider timeout") from None
            sleeper(delay)
            delay = min(delay * 2, 8.0)
            continue
        except httpx.HTTPError:
            logger.info(
                "llm transport error provider=%s model=%s body_sha256=%s attempt=%s",
                provider_name,
                model,
                digest,
                attempt,
            )
            if attempt >= max_retries:
                raise ProviderError("provider request failed") from None
            sleeper(delay)
            delay = min(delay * 2, 8.0)
            continue
        logger.info(
            "llm response provider=%s model=%s status=%s body_sha256=%s",
            provider_name,
            model,
            response.status_code,
            digest,
        )
        if response.status_code == 429 or response.status_code >= 500:
            last_status = response.status_code
            if attempt >= max_retries:
                raise ProviderError(f"provider HTTP {response.status_code}") from None
            sleeper(delay)
            delay = min(delay * 2, 8.0)
            continue
        if response.status_code >= 400:
            detail = _redact(response.text, api_key)[:500]
            raise ProviderError(f"provider HTTP {response.status_code}: {detail}") from None
        try:
            payload = response.json()
        except json.JSONDecodeError:
            raise ProviderError("provider returned non-JSON") from None
        if not isinstance(payload, dict):
            raise ProviderError("provider JSON was not an object")
        return payload
    raise ProviderError(f"provider HTTP {last_status}")


def _usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key) or 0
    return int(value)


def _anthropic_parsed(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    for block in payload.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        parsed = block.get("input")
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                raise ProviderError("tool output was not JSON") from None
        if not isinstance(parsed, dict):
            raise ProviderError("tool output was not a JSON object")
        return parsed, json.dumps(parsed, sort_keys=True)
    raise ProviderError("response did not contain a tool_use block")


def _openai_parsed(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = str(part.get("text") or "")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                raise ProviderError("model output was not JSON") from None
            if not isinstance(parsed, dict):
                raise ProviderError("model output was not a JSON object")
            return parsed, text
    raise ProviderError("response did not contain output text")


def _anthropic_body(request: CompletionRequest) -> dict[str, Any]:
    """System and the last evidence block are the cache prefix; instructions stay uncached."""
    system = [{"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}]
    evidence = [block for block in request.blocks if block.kind == "evidence"]
    rest = [block for block in request.blocks if block.kind != "evidence"]
    content: list[dict[str, Any]] = []
    for index, block in enumerate(evidence):
        item: dict[str, Any] = {"type": "text", "text": block.text}
        if index == len(evidence) - 1:
            item["cache_control"] = {"type": "ephemeral"}
        content.append(item)
    for block in rest:
        content.append({"type": "text", "text": block.text})
    if not content:
        content.append({"type": "text", "text": "Return the tool result."})
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "tools": [
            {
                "name": _TOOL_NAME,
                "description": "Return the role output as JSON matching the schema.",
                "input_schema": request.json_schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": _TOOL_NAME},
    }


def _openai_body(request: CompletionRequest) -> dict[str, Any]:
    """Responses API body. ``instructions`` is the system text; evidence is user input_text."""
    parts = [{"type": "input_text", "text": block.text} for block in request.blocks]
    if not parts:
        parts.append({"type": "input_text", "text": "Return JSON matching the schema."})
    return {
        "model": request.model,
        "instructions": request.system,
        "input": [{"role": "user", "content": parts}],
        "max_output_tokens": request.max_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "role_output",
                "strict": True,
                "schema": request.json_schema,
            }
        },
    }


class _HttpProvider:
    """Shared timeout, retry, and client ownership for the two HTTP providers."""

    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.Client | None,
        timeout_s: float,
        max_retries: int,
        sleeper: Callable[[float], None] | None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._api_key = api_key
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=httpx.Timeout(timeout_s))
        self._max_retries = max_retries
        self._sleeper = sleeper if sleeper is not None else time.sleep

    def close(self) -> None:
        """Close the client when this provider constructed it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> _HttpProvider:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class AnthropicProvider(_HttpProvider):
    """Anthropic Messages API with a forced tool for JSON (spec §8.3).

    Set ``ANTHROPIC_API_KEY`` or pass ``api_key``. Prompt caching uses ``cache_control`` on the
    system block and on the last evidence block. ``input_tokens`` in the result is Anthropic's
    uncached input; ``cache_read_input_tokens`` is reported separately.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        super().__init__(
            api_key=key, client=client, timeout_s=timeout_s, max_retries=max_retries, sleeper=sleeper
        )

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """POST one Messages request and parse the tool input (spec §8.3 role boundary)."""
        body = _anthropic_body(request)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        started = time.perf_counter()
        payload = _post_json(
            self._client,
            _ANTHROPIC_URL,
            headers,
            body,
            api_key=self._api_key,
            provider_name="anthropic",
            model=request.model,
            max_retries=self._max_retries,
            sleeper=self._sleeper,
        )
        parsed, raw_text = _anthropic_parsed(payload)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        return CompletionResult(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=_usage_int(usage, "input_tokens"),
            output_tokens=_usage_int(usage, "output_tokens"),
            cache_read_tokens=_usage_int(usage, "cache_read_input_tokens"),
            provider="anthropic",
            model=request.model,
            latency_s=time.perf_counter() - started,
        )


class OpenAIProvider(_HttpProvider):
    """OpenAI Responses API with ``text.format`` JSON schema (spec §8.3).

    This is ``POST https://api.openai.com/v1/responses``, not chat completions. Set
    ``OPENAI_API_KEY`` or pass ``api_key``. ``usage.input_tokens`` includes cached tokens;
    those are subtracted into ``cache_read_tokens`` so the ledger does not bill them twice.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        super().__init__(
            api_key=key, client=client, timeout_s=timeout_s, max_retries=max_retries, sleeper=sleeper
        )

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """POST one Responses request and parse the output text as JSON (spec §8.3)."""
        body = _openai_body(request)
        headers = {"authorization": f"Bearer {self._api_key}", "content-type": "application/json"}
        started = time.perf_counter()
        payload = _post_json(
            self._client,
            _OPENAI_URL,
            headers,
            body,
            api_key=self._api_key,
            provider_name="openai",
            model=request.model,
            max_retries=self._max_retries,
            sleeper=self._sleeper,
        )
        parsed, raw_text = _openai_parsed(payload)
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        reported_input = _usage_int(usage, "input_tokens")
        details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        cache_read = _usage_int(details, "cached_tokens")
        if cache_read > reported_input:
            cache_read = reported_input
        return CompletionResult(
            parsed=parsed,
            raw_text=raw_text,
            input_tokens=reported_input - cache_read,
            output_tokens=_usage_int(usage, "output_tokens"),
            cache_read_tokens=cache_read,
            provider="openai",
            model=request.model,
            latency_s=time.perf_counter() - started,
        )
