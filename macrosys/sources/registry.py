"""Source registry, token-bucket limiter, and HTTP client (spec §9.1, §9.5, §9.6).

Limits, tiers, and env-var names come from `config/source_registry.yaml`. Nothing in this module
hard-codes a provider's rate. Secrets are removed from the stored request before it is logged or
written to the raw store.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from macrosys import __version__
from macrosys.core.contracts import RawPayload, RawStore, SourceTier

log = logging.getLogger("macrosys.sources")

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "token",
    "password",
    "signature",
    "x-app-token",
    "registrationkey",
    "tv_session",
    "tv_signature",
    "session",
    "ocp-apim-subscription-key",
}


def registry_path() -> Path:
    """Path to `config/source_registry.yaml` relative to this package (spec §9.6)."""
    return Path(__file__).resolve().parents[2] / "config" / "source_registry.yaml"


class AuthConfig(BaseModel):
    """How a source authenticates. `env` is an environment variable name, never the secret itself."""

    model_config = ConfigDict(extra="allow", frozen=True)

    env: str | list[str] | None = None
    how: str | None = None
    optional: bool = False


class AccessConfig(BaseModel):
    """Access block from the registry: kind, base URL, and optional auth (spec §9.6)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    kind: str
    base_url: str | None = None
    url: str | None = None
    auth: AuthConfig | None = None
    user_agent_env: str | None = None
    package: str | None = None
    requires: str | None = None


class BurstLimit(BaseModel):
    """A short-window cap that sits on top of the sustained rate (spec §9.6)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    requests: int
    per_seconds: float


class RateLimit(BaseModel):
    """Sustained rate plus the optional burst, daily, and symbol caps declared in the registry."""

    model_config = ConfigDict(extra="allow", frozen=True)

    requests: int | None = None
    per_seconds: float | None = None
    burst: BurstLimit | None = None
    daily: int | None = None
    unique_symbols_per_month: int | None = None
    series_per_query: int | None = None
    years_per_query: int | None = None
    records_per_call: int | None = None
    bandwidth_gb_per_month: float | None = None
    note: str | None = None


class ScheduleConfig(BaseModel):
    """A publication schedule stated in local civil time (spec §9.6)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    weekday: str
    time_local: str
    tz: str
    data_as_of: str | None = None


class SourceConfig(BaseModel):
    """One registry entry. Connectors read limits and tier from this object (spec §9.6)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    source_id: str
    name: str
    tier: SourceTier
    access: AccessConfig
    rate_limit: RateLimit | None = None
    license: str | None = None
    verified: date | None = None
    vintages: bool | None = None
    datasets: dict[str, str] | None = None
    schedule: ScheduleConfig | None = None
    notes: list[str] | None = None
    quality_flag: str | None = None


def load_registry(path: Path | None = None) -> dict[str, SourceConfig]:
    """Load `config/source_registry.yaml` into source-id-keyed configs (spec §9.6)."""
    target = path or registry_path()
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    rows = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{target} has no sources list")
    loaded: dict[str, SourceConfig] = {}
    for row in rows:
        cfg = SourceConfig.model_validate(row)
        loaded[cfg.source_id] = cfg
    return loaded


def require_source(source_id: str, path: Path | None = None) -> SourceConfig:
    """Return one source config, or raise when the id is not in the registry (spec §9.6)."""
    registry = load_registry(path)
    try:
        return registry[source_id]
    except KeyError as exc:
        raise KeyError(f"source {source_id!r} is not in {path or registry_path()}") from exc


class _Window:
    """One token bucket. Capacity is the number of requests allowed in `per_seconds`."""

    def __init__(self, capacity: float, per_seconds: float, clock: Callable[[], float]) -> None:
        if capacity <= 0 or per_seconds <= 0:
            raise ValueError("rate window needs a positive capacity and period")
        self.capacity = float(capacity)
        self.rate = float(capacity) / float(per_seconds)
        self.tokens = float(capacity)
        self.updated = clock()
        self._clock = clock

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now

    def deficit(self) -> float:
        """Seconds until one token is available. Refills from the injected clock first."""
        self._refill()
        if self.tokens >= 1.0:
            return 0.0
        return (1.0 - self.tokens) / self.rate

    def take(self) -> None:
        """Consume one token. Caller must have waited until `deficit` was zero."""
        self._refill()
        if self.tokens + 1e-9 < 1.0:
            raise RuntimeError("token bucket take without a token")
        self.tokens -= 1.0


class TokenBucket:
    """Limiter honouring `requests/per_seconds`, plus optional `burst` and `daily` windows (spec §9.6).

    The burst window is an extra constraint, not a larger capacity. `clock` returns monotonic seconds
    and `sleeper` must advance that clock when the clock is fake. A full bucket allows `requests`
    calls immediately, then refills smoothly.
    """

    def __init__(
        self,
        windows: list[tuple[float, float]],
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._windows = [_Window(capacity, period, self._clock) for capacity, period in windows]

    @classmethod
    def from_rate_limit(
        cls,
        limit: RateLimit | None,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> TokenBucket:
        """Build a bucket from a registry rate limit. No limit means acquire() does not wait."""
        windows: list[tuple[float, float]] = []
        if limit is not None and limit.requests and limit.per_seconds:
            windows.append((limit.requests, limit.per_seconds))
        if limit is not None and limit.burst is not None:
            windows.append((limit.burst.requests, limit.burst.per_seconds))
        if limit is not None and limit.daily:
            windows.append((limit.daily, 86_400))
        return cls(windows, clock=clock, sleeper=sleeper)

    def acquire(self) -> None:
        """Block via `sleeper` until every window can spare one token (spec §9.6)."""
        if not self._windows:
            return
        while True:
            delay = max(window.deficit() for window in self._windows)
            if delay <= 1e-9:
                for window in self._windows:
                    window.take()
                return
            self._sleeper(delay)


def _sensitive(key: str) -> bool:
    return key.lower().replace("_", "-") in _SECRET_KEYS or key.lower() in _SECRET_KEYS


def redact(value: Any, secrets: list[str]) -> Any:
    """Replace secret values and sensitive keys with `***` before logging or storage (spec §9.5)."""
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                value = value.replace(secret, "***")
        return value
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _sensitive(key):
                cleaned[key] = "***"
            else:
                cleaned[key] = redact(item, secrets)
        return cleaned
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def _collect_secrets(*mappings: dict[str, Any], extra: list[str]) -> list[str]:
    found = [secret for secret in extra if secret]
    for mapping in mappings:
        for key, value in mapping.items():
            if isinstance(key, str) and _sensitive(key) and isinstance(value, str) and value:
                found.append(value)
    return sorted(set(found), key=len, reverse=True)


def user_agent(cfg: SourceConfig) -> str:
    """User-Agent for this source (spec §9.5).

    SEC requires `SEC_USER_AGENT` (the registry's `user_agent_env`). Every other source sends
    `macrosys/<version> (contact: $MACROSYS_CONTACT)`.
    """
    env_name = cfg.access.user_agent_env
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            raise RuntimeError(f"{cfg.source_id} requires a User-Agent in env {env_name}")
        return value
    contact = os.environ.get("MACROSYS_CONTACT", "")
    return f"macrosys/{__version__} (contact: {contact})"


def apply_auth(
    cfg: SourceConfig,
    params: dict[str, str],
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> list[str]:
    """Add the registry's auth material from the environment. Returns the secret strings to redact.

    `how: body:<field>` writes the secret into `body` (BLS `registrationkey`). GET calls omit `body`
    and refuse that scheme.
    """
    auth = cfg.access.auth
    if auth is None or auth.env is None or isinstance(auth.env, list) or not auth.how or ":" not in auth.how:
        return []
    kind, name = auth.how.split(":", 1)
    value = os.environ.get(auth.env)
    if not value:
        if auth.optional:
            return []
        raise RuntimeError(f"{cfg.source_id} requires env {auth.env}")
    if kind == "query":
        params[name] = value
    elif kind == "header":
        # Tiingo's Authorization value is "Token <key>". The env holds the key alone.
        if name.lower() == "authorization" and " " not in value:
            value = f"Token {value}"
        headers[name] = value
    elif kind == "body":
        if body is None:
            raise ValueError(f"{cfg.source_id} auth how {auth.how!r} is sent by HttpClient.post")
        body[name] = value
    else:
        raise ValueError(f"{cfg.source_id} auth how {auth.how!r} is not supported")
    return [value]


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait. `Retry-After` wins; otherwise exponential backoff from 0.5s (spec §9.5)."""
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                when = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                when = None
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - datetime.now(UTC)).total_seconds())
    return min(60.0, 0.5 * (2**attempt))


def _cache_key(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    clean = "&".join(f"{key}={value}" for key, value in sorted(params.items()) if not _sensitive(key))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, clean, ""))


class HttpClient:
    """httpx wrapper that rate-limits, retries, conditions, redacts, and stores every response (spec §9.1).

    `transport` is an httpx transport (use `httpx.MockTransport` in tests). Conditional requests remember
    `ETag` and `Last-Modified` in memory, keyed by URL with secret query parameters removed. A redirect
    is followed only when the source has no credentials, so an API key is not replayed to another host.
    """

    def __init__(
        self,
        source_cfg: SourceConfig,
        raw_store: RawStore,
        transport: httpx.BaseTransport | None = None,
        *,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        now: Callable[[], datetime] | None = None,
        max_retries: int = 4,
        timeout: float = 60.0,
    ) -> None:
        self.cfg = source_cfg
        self.raw = raw_store
        self.limiter = TokenBucket.from_rate_limit(source_cfg.rate_limit, clock=clock, sleeper=sleeper)
        self.sleeper = sleeper or time.sleep
        self.now = now or (lambda: datetime.now(UTC))
        self.max_retries = max_retries
        self.transport = transport
        self._validators: dict[str, tuple[str | None, str | None]] = {}
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        extra_request: dict[str, Any] | None = None,
    ) -> RawPayload:
        """GET `url`, store every response (including retries), and return the successful payload.

        `extra_request` is merged into the stored request after redaction. Connectors use it for facts
        the response body does not carry, such as a FRED series unit. 429 and 5xx are retried with
        `Retry-After` when present.
        """
        query = {key: "" if value is None else str(value) for key, value in (params or {}).items()}
        hdrs = dict(headers or {})
        secrets = apply_auth(self.cfg, query, hdrs)
        hdrs.setdefault("User-Agent", user_agent(self.cfg))
        hdrs.setdefault("Accept", "*/*")
        key = _cache_key(url, query)
        etag, modified = self._validators.get(key, (None, None))
        if etag:
            hdrs["If-None-Match"] = etag
        if modified:
            hdrs["If-Modified-Since"] = modified
        secrets = _collect_secrets(query, hdrs, extra=secrets)

        attempt = 0
        while True:
            self.limiter.acquire()
            response = self._http.get(url, params=query, headers=hdrs)
            self._remember(key, response)
            payload = self._store("GET", url, query, hdrs, response, secrets, extra_request)
            status = response.status_code
            location = response.headers.get("Location")
            if 300 <= status < 400 and location and self.cfg.access.auth is None and attempt < self.max_retries:
                url = urljoin(url, location)
                query = {}
                attempt += 1
                continue
            if status == 304 or status < 400:
                log.info("%s %s -> %s", self.cfg.source_id, url, status)
                return payload
            if status == 429 or status >= 500:
                if attempt >= self.max_retries:
                    raise RuntimeError(
                        f"{self.cfg.source_id} HTTP {status} for {url} after {attempt + 1} attempts"
                    )
                delay = _retry_delay(response, attempt)
                log.info("%s %s -> %s; retrying in %ss", self.cfg.source_id, url, status, delay)
                self.sleeper(delay)
                attempt += 1
                continue
            detail = response.text[:300].replace("\n", " ")
            for secret in secrets:
                if secret:
                    detail = detail.replace(secret, "***")
            raise RuntimeError(f"{self.cfg.source_id} HTTP {status} for {url}: {detail}")

    def _remember(self, key: str, response: httpx.Response) -> None:
        if response.status_code == 304:
            return
        etag = response.headers.get("ETag")
        modified = response.headers.get("Last-Modified")
        if etag or modified:
            previous = self._validators.get(key, (None, None))
            self._validators[key] = (etag or previous[0], modified or previous[1])

    def post(
        self,
        url: str,
        *,
        json_body: dict[str, Any],
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        extra_request: dict[str, Any] | None = None,
    ) -> RawPayload:
        """POST JSON, store every response, and return the successful payload (spec §9.1, §9.5).

        Body authentication (`how: body:<field>`, used by BLS `registrationkey`) is copied into
        `json_body` from the environment and redacted before the request is stored.
        """
        body = dict(json_body)
        query = {key: "" if value is None else str(value) for key, value in (params or {}).items()}
        hdrs = dict(headers or {})
        secrets = apply_auth(self.cfg, query, hdrs, body)
        hdrs.setdefault("User-Agent", user_agent(self.cfg))
        hdrs.setdefault("Accept", "application/json")
        secrets = _collect_secrets(query, hdrs, body, extra=secrets)
        attempt = 0
        while True:
            self.limiter.acquire()
            response = self._http.post(url, params=query, headers=hdrs, json=body)
            payload = self._store("POST", url, query, hdrs, response, secrets, extra_request, body=body)
            status = response.status_code
            if status < 400:
                log.info("%s POST %s -> %s", self.cfg.source_id, url, status)
                return payload
            if status == 429 or status >= 500:
                if attempt >= self.max_retries:
                    raise RuntimeError(
                        f"{self.cfg.source_id} HTTP {status} for {url} after {attempt + 1} attempts"
                    )
                self.sleeper(_retry_delay(response, attempt))
                attempt += 1
                continue
            detail = response.text[:300].replace("\n", " ")
            for secret in secrets:
                if secret:
                    detail = detail.replace(secret, "***")
            raise RuntimeError(f"{self.cfg.source_id} HTTP {status} for {url}: {detail}")

    def _store(
        self,
        method: str,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
        response: httpx.Response,
        secrets: list[str],
        extra_request: dict[str, Any] | None,
        body: dict[str, Any] | None = None,
    ) -> RawPayload:
        stored: dict[str, Any] = {"method": method, "url": url, "params": dict(params), "headers": dict(headers)}
        if body is not None:
            stored["json"] = body
        request: dict[str, Any] = redact(stored, secrets)
        if extra_request:
            request.update(redact(extra_request, secrets))
        content = response.content
        return self.raw.put(
            self.cfg.source_id,
            request,
            content,
            status=response.status_code,
            content_type=response.headers.get("Content-Type"),
            fetched_at=self.now(),
        )
