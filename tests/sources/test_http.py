"""Registry loader, token bucket, and HTTP client (spec §9.5, §9.6)."""

from datetime import UTC, datetime

import httpx
import pytest

from macrosys.sources import SOURCES
from macrosys.sources.registry import HttpClient, TokenBucket, load_registry, require_source, user_agent
from tests.sources.fakes import FakeClock, MemoryRawStore

FETCHED = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def test_registry_exposes_auth_and_limits_from_yaml():
    registry = load_registry()
    assert require_source("FRED").access.auth is not None
    assert require_source("FRED").access.auth.env == "FRED_API_KEY"
    assert require_source("FRED").access.auth.how == "query:api_key"
    assert require_source("SEC").access.user_agent_env == "SEC_USER_AGENT"
    assert require_source("CFTC").datasets["tff_futures"] == "gpe5-46if"
    assert require_source("CFTC").access.auth.optional is True
    assert require_source("TIINGO").rate_limit.unique_symbols_per_month == 500
    assert require_source("TRADINGVIEW_UNOFFICIAL").tier == "research"
    assert require_source("MT5").tier == "cross_check"
    assert require_source("FEDGSW").access.url.endswith("feds200628.csv")
    assert "FRED" in registry


def test_sources_map_matches_connector_ids():
    assert set(SOURCES) >= {
        "FRED",
        "BLS",
        "BEA",
        "CENSUS",
        "ECB",
        "IMF",
        "OECD",
        "COMTRADE",
        "CFTC",
        "SEC",
        "TREASURY",
        "FEDGSW",
        "PORTWATCH",
        "EIA",
        "NYFED",
        "WORLDBANK",
        "CBOE",
        "TIINGO",
        "MT5",
        "TRADINGVIEW_UNOFFICIAL",
    }
    for source_id, cls in SOURCES.items():
        assert cls.source_id == source_id
        assert cls.PARSER_VERSION


def test_token_bucket_enforces_registry_rate():
    limit = require_source("FRED").rate_limit
    clock = FakeClock()
    bucket = TokenBucket.from_rate_limit(limit, clock=clock, sleeper=clock.sleep)
    for _ in range(limit.requests):
        bucket.acquire()
    assert clock.t == 0
    bucket.acquire()
    assert clock.t == pytest.approx(limit.per_seconds / limit.requests)


def test_token_bucket_enforces_registry_burst():
    limit = require_source("BLS").rate_limit
    clock = FakeClock()
    bucket = TokenBucket.from_rate_limit(limit, clock=clock, sleeper=clock.sleep)
    for _ in range(limit.burst.requests):
        bucket.acquire()
    assert clock.t == 0
    bucket.acquire()
    assert clock.t == pytest.approx(limit.burst.per_seconds / limit.burst.requests)


def test_http_client_retries_retry_after_redacts_and_stores(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FRED_API_KEY", "supersecret")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.url.params["api_key"] == "supersecret"
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, content=b"slow")
        return httpx.Response(200, content=b"ok", headers={"Content-Type": "application/json"})

    clock = FakeClock()
    store = MemoryRawStore()
    client = HttpClient(
        require_source("FRED"),
        store,
        transport=httpx.MockTransport(handler),
        sleeper=clock.sleep,
        clock=clock,
        now=lambda: FETCHED,
    )
    payload = client.get("https://api.stlouisfed.org/fred/series/observations", params={"series_id": "GDPC1"})
    assert payload.status == 200
    assert store.get(payload) == b"ok"
    assert [item.status for item in store.payloads] == [429, 200]
    assert clock.t == pytest.approx(3.0)
    for item in store.payloads:
        dumped = str(item.request)
        assert "supersecret" not in dumped
        assert item.request["params"]["api_key"] == "***"
    client.close()


def test_http_client_sends_conditional_headers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MACROSYS_CONTACT", "desk@example.com")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, content=b"v1", headers={"ETag": '"abc"', "Last-Modified": "Wed, 14 Oct 2026 12:00:00 GMT"})
        return httpx.Response(304, content=b"", headers={"ETag": '"abc"'})

    store = MemoryRawStore()
    client = HttpClient(
        require_source("ECB"),
        store,
        transport=httpx.MockTransport(handler),
        now=lambda: FETCHED,
    )
    first = client.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A", params={"format": "csvdata"})
    second = client.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A", params={"format": "csvdata"})
    assert first.status == 200 and second.status == 304
    assert calls[1].headers["If-None-Match"] == '"abc"'
    assert calls[1].headers["If-Modified-Since"] == "Wed, 14 Oct 2026 12:00:00 GMT"
    assert calls[0].headers["User-Agent"] == user_agent(require_source("ECB"))
    assert "desk@example.com" in calls[0].headers["User-Agent"]
    assert len(store.payloads) == 2
    client.close()


def test_sec_user_agent_comes_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    store = MemoryRawStore()
    client = HttpClient(require_source("SEC"), store, transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        client.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
    monkeypatch.setenv("SEC_USER_AGENT", "Example Research desk@example.com")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, content=b"{}")

    client = HttpClient(require_source("SEC"), store, transport=httpx.MockTransport(handler), now=lambda: FETCHED)
    client.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
    assert seen == ["Example Research desk@example.com"]
    client.close()
