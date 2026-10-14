"""Parsers and registry rate caps for the remaining v0 connectors."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.bea import parse_bea
from macrosys.sources.bls import BlsSource, parse_timeseries
from macrosys.sources.cboe import parse_cboe_csv
from macrosys.sources.census import parse_census
from macrosys.sources.comtrade import parse_comtrade
from macrosys.sources.eia import map_eia_unit, parse_eia
from macrosys.sources.imf import parse_imf_csv
from macrosys.sources.nyfed import parse_markets
from macrosys.sources.oecd import parse_oecd_csv
from macrosys.sources.portwatch import parse_features
from macrosys.sources.registry import TokenBucket, require_source
from macrosys.sources.worldbank import WorldBankSource, parse_worldbank
from tests.sources.fakes import FakeClock, MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


def _load(fixtures_dir: Path, *parts: str) -> bytes:
    return (fixtures_dir / "sources").joinpath(*parts).read_bytes()


def test_bls_index_missing_value_and_release_clock(fixtures_dir: Path):
    content = _load(fixtures_dir, "bls", "cpi.json")
    request = {
        "url": "https://api.bls.gov/publicAPI/v2/timeseries/data/",
        "params": {},
        "series_units": {"CUUR0000SA0": "index"},
        "release_times": {"CUUR0000SA0|2024|M01": "2024-02-13T13:30:00+00:00"},
    }
    _store, payload = stored("BLS", content, request, FETCHED)
    rows = {row.ref_period_start: row for row in parse_timeseries(payload, content)}
    january = rows[date(2024, 1, 1)]
    assert january.series_id == "BLS:CUUR0000SA0"
    assert january.unit == "index"
    assert january.value == pytest.approx(308.417)
    assert january.ref_period_end == date(2024, 1, 31)
    assert january.published_at == datetime(2024, 2, 13, 13, 30, tzinfo=UTC)
    assert january.quality_flag == "ok"
    assert rows[date(2024, 2, 1)].value is None
    assert rows[date(2024, 2, 1)].quality_flag == "reconstructed_pubtime"
    mutated = json.loads(content)
    del mutated["Results"]
    with pytest.raises(ParserChangedError, match="Results"):
        parse_timeseries(payload, json.dumps(mutated).encode())


def test_bls_chunks_series_and_years_and_redacts_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BLS_API_KEY", "bls-secret")
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"Results": {"series": []}})

    store = MemoryRawStore()
    source = BlsSource(store, transport=httpx.MockTransport(handler))
    source.fetch_many([f"S{i}" for i in range(51)], 2024, 2024, store)
    assert [len(body["seriesid"]) for body in bodies] == [50, 1]
    assert bodies[0]["registrationkey"] == "bls-secret"
    assert store.payloads[0].request["json"]["registrationkey"] == "***"
    bodies.clear()
    source.fetch_many(["CUUR0000SA0"], 2000, 2020, store)
    assert [(body["startyear"], body["endyear"]) for body in bodies] == [("2000", "2019"), ("2020", "2020")]
    assert source.cfg.rate_limit.series_per_query == 50
    assert source.cfg.rate_limit.years_per_query == 20


def test_bea_dollars_percent_and_production_time(fixtures_dir: Path):
    content = _load(fixtures_dir, "bea", "nipa_gdp.json")
    _store, payload = stored("BEA", content, {"url": "https://apps.bea.gov/api/data/", "params": {}, "dataset": "NIPA"}, FETCHED)
    rows = {row.series_id: row for row in parse_bea(payload, content)}
    level = rows["BEA:NIPA:T10105:1"]
    assert level.value == pytest.approx(27360.9)
    assert level.unit == "USD_mn"
    assert level.ref_period_start == date(2023, 10, 1)
    assert level.ref_period_end == date(2023, 12, 31)
    assert level.published_at == datetime(2024, 3, 28, 12, 30, tzinfo=UTC)
    assert level.quality_flag == "ok"
    assert rows["BEA:NIPA:T10101:1"].unit == "pct"
    assert rows["BEA:NIPA:T10101:1"].value == pytest.approx(3.4)
    with pytest.raises(ParserChangedError, match="BEAAPI"):
        parse_bea(payload, b"{}")


def test_census_matrix(fixtures_dir: Path):
    content = _load(fixtures_dir, "census", "marts.json")
    request = {"url": "https://api.census.gov/data/timeseries/eits/marts", "params": {}, "dataset": "eits/marts", "unit": "USD_mn"}
    _store, payload = stored("CENSUS", content, request, FETCHED)
    row = parse_census(payload, content)[0]
    assert row.series_id == "CENSUS:eits/marts:44000:SM"
    assert row.unit == "USD_mn"
    assert row.value == pytest.approx(650123)
    assert row.ref_period_start == date(2024, 1, 1)
    assert row.quality_flag == "reconstructed_pubtime"
    with pytest.raises(ParserChangedError):
        parse_census(payload, b"[]")


def test_comtrade_usd_and_tonnes(fixtures_dir: Path):
    content = _load(fixtures_dir, "comtrade", "usa_exports.json")
    _store, payload = stored("COMTRADE", content, {"url": "https://comtradeapi.un.org/data/v1/get/C/A/HS", "params": {}}, FETCHED)
    rows = {row.series_id: row for row in parse_comtrade(payload, content)}
    trade = rows["COMTRADE:USA:W00:X:TOTAL"]
    assert trade.unit == "USD"
    assert trade.value == pytest.approx(2_000_000_000)
    assert trade.ref_period_start == date(2022, 1, 1)
    weight = rows["COMTRADE:USA:W00:X:TOTAL:netWgt"]
    assert weight.unit == "t"
    assert weight.value == pytest.approx(1.5)
    assert weight.quality_flag == "reconstructed_pubtime"
    with pytest.raises(ParserChangedError, match="data"):
        parse_comtrade(payload, b'{"count": 0}')


def test_comtrade_burst_from_registry():
    limit = require_source("COMTRADE").rate_limit
    clock = FakeClock()
    bucket = TokenBucket.from_rate_limit(limit, clock=clock, sleeper=clock.sleep)
    bucket.acquire()
    assert clock.t == 0
    bucket.acquire()
    assert clock.t == pytest.approx(limit.burst.per_seconds / limit.burst.requests)


def test_eia_unit_map(fixtures_dir: Path):
    content = _load(fixtures_dir, "eia", "stocks.json")
    _store, payload = stored("EIA", content, {"url": "https://api.eia.gov/v2/", "params": {}, "route": "petroleum/stoc/wstk"}, FETCHED)
    rows = {row.series_id: row for row in parse_eia(payload, content)}
    assert rows["EIA:petroleum/stoc/wstk:WCESTUS1"].unit == "kbbl"
    assert rows["EIA:petroleum/stoc/wstk:WCRFPUS2"].unit == "kbd"
    assert rows["EIA:petroleum/stoc/wstk:ELEC.GEN.ALL-US-99.M"].unit == "GWh"
    assert rows["EIA:petroleum/stoc/wstk:WCESTUS1"].value == pytest.approx(430000)
    assert map_eia_unit("thousand megawatthours") == "GWh"
    with pytest.raises(ValueError, match="no mapping"):
        map_eia_unit("widgets")
    with pytest.raises(ParserChangedError, match="response"):
        parse_eia(payload, b"{}")


def test_imf_cpi_csv_and_rate(fixtures_dir: Path):
    content = _load(fixtures_dir, "imf", "cpi_usa.csv")
    _store, payload = stored("IMF", content, {"url": "https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/CPI/+/USA.CPI.CP01.IX.M", "params": {}}, FETCHED, content_type="text/csv")
    row = parse_imf_csv(payload, content)[0]
    assert row.series_id == "IMF:CPI:USA.CPI.CP01.IX.M"
    assert row.unit == "index"
    assert row.value == pytest.approx(141.3164521681257)
    assert row.ref_period_start == date(2024, 1, 1)
    assert row.ref_period_end == date(2024, 1, 31)
    assert row.quality_flag == "reconstructed_pubtime"
    with pytest.raises(ParserChangedError, match="TIME_PERIOD"):
        parse_imf_csv(payload, b"STRUCTURE_ID\n")
    limit = require_source("IMF").rate_limit
    clock = FakeClock()
    bucket = TokenBucket.from_rate_limit(limit, clock=clock, sleeper=clock.sleep)
    for _ in range(limit.requests):
        bucket.acquire()
    bucket.acquire()
    assert clock.t == pytest.approx(limit.per_seconds / limit.requests)


def test_oecd_csv_and_hourly_cap(fixtures_dir: Path):
    content = _load(fixtures_dir, "oecd", "prices_usa_cpi.csv")
    _store, payload = stored("OECD", content, {"url": "https://sdmx.oecd.org/public/rest/data/", "params": {}}, FETCHED, content_type="text/csv")
    rows = parse_oecd_csv(payload, content)
    march = next(row for row in rows if row.ref_period_start == date(2024, 3, 1))
    assert march.series_id.startswith("OECD:DSD_PRICES@DF_PRICES_ALL:")
    assert "USA.M.N.CPI.PA" in march.series_id
    assert march.unit == "pct"
    assert march.value == pytest.approx(3.477385)
    assert march.quality_flag == "reconstructed_pubtime"
    with pytest.raises(ParserChangedError, match="TIME_PERIOD"):
        parse_oecd_csv(payload, b"STRUCTURE_ID\nx\n")
    limit = require_source("OECD").rate_limit
    clock = FakeClock()
    bucket = TokenBucket.from_rate_limit(limit, clock=clock, sleeper=clock.sleep)
    for _ in range(limit.requests):
        bucket.acquire()
    bucket.acquire()
    assert clock.t == pytest.approx(limit.per_seconds / limit.requests)


def test_portwatch_snapshot(fixtures_dir: Path):
    content = _load(fixtures_dir, "portwatch", "ports_sample.json")
    _store, payload = stored("PORTWATCH", content, {"url": "https://services9.arcgis.com/PortWatch_ports_database/FeatureServer/0/query", "params": {}}, FETCHED)
    rows = {row.series_id: row for row in parse_features(payload, content)}
    calls = rows["PORTWATCH:port1325:vessel_count_total"]
    assert calls.value == pytest.approx(529)
    assert calls.unit == "count"
    assert calls.published_at == FETCHED
    assert calls.ref_period_start == FETCHED.date()
    assert calls.quality_flag == "reconstructed_pubtime"
    share = rows["PORTWATCH:port1325:share_country_maritime_import"]
    assert share.unit == "share"
    assert share.value == pytest.approx(0.42)
    with pytest.raises(ParserChangedError, match="features"):
        parse_features(payload, b'{"fields": []}')


def test_nyfed_sofr(fixtures_dir: Path):
    content = _load(fixtures_dir, "nyfed", "sofr.json")
    _store, payload = stored("NYFED", content, {"url": "https://markets.newyorkfed.org/api/rates/secured/sofr/last/2.json", "params": {}}, FETCHED)
    rows = {row.series_id: row for row in parse_markets(payload, content)}
    rate = rows["NYFED:SOFR:percentRate"]
    assert rate.unit == "pct"
    assert rate.value == pytest.approx(3.89)
    assert rate.ref_period_start == date(2026, 10, 13)
    assert rate.quality_flag == "reconstructed_pubtime"
    assert rate.published_at.tzinfo is not None
    volume = rows["NYFED:SOFR:volumeInBillions"]
    assert volume.unit == "USD_bn"
    assert volume.value == pytest.approx(3000)
    with pytest.raises(ParserChangedError, match="refRates"):
        parse_markets(payload, b"{}")


def test_worldbank_pagination_and_parse(fixtures_dir: Path):
    content = _load(fixtures_dir, "worldbank", "us_gdp.json")
    _store, payload = stored("WORLDBANK", content, {"url": "https://api.worldbank.org/v2/country/US/indicator/NY.GDP.MKTP.CD", "params": {}}, FETCHED)
    rows = {row.ref_period_start: row for row in parse_worldbank(payload, content)}
    gdp = rows[date(2023, 1, 1)]
    assert gdp.series_id == "WORLDBANK:USA:NY.GDP.MKTP.CD"
    assert gdp.unit == "USD"
    assert gdp.value == pytest.approx(27_811_517_000_000)
    assert gdp.published_at == datetime(2026, 10, 8, tzinfo=UTC)
    assert gdp.quality_flag == "reconstructed_pubtime"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["page"])
        page = int(request.url.params["page"])
        body = [{"page": page, "pages": 2, "lastupdated": "2026-10-08"}, []]
        return httpx.Response(200, json=body)

    store = MemoryRawStore()
    source = WorldBankSource(store, transport=httpx.MockTransport(handler), per_page=1)
    source.fetch("WORLDBANK:USA:NY.GDP.MKTP.CD", None, store)
    assert calls == ["1", "2"]
    with pytest.raises(ParserChangedError, match="metadata"):
        parse_worldbank(payload, b"{}")


def test_cboe_vix_and_ovx(fixtures_dir: Path):
    vix = _load(fixtures_dir, "cboe", "VIX_History.csv")
    _store, payload = stored("CBOE", vix, {"url": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", "params": {}, "index": "VIX"}, FETCHED, content_type="text/csv")
    close = next(row for row in parse_cboe_csv(payload, vix) if row.series_id == "CBOE:VIX:CLOSE" and row.ref_period_start == date(1990, 1, 2))
    assert close.value == pytest.approx(17.24)
    assert close.unit == "index"
    assert close.quality_flag == "reconstructed_pubtime"
    ovx = _load(fixtures_dir, "cboe", "OVX_History.csv")
    _store, ovx_payload = stored("CBOE", ovx, {"url": "https://cdn.cboe.com/api/global/us_indices/daily_prices/OVX_History.csv", "index": "OVX"}, FETCHED, content_type="text/csv")
    row = parse_cboe_csv(ovx_payload, ovx)[0]
    assert row.series_id == "CBOE:OVX:OVX"
    assert row.value == pytest.approx(41.67)
    with pytest.raises(ParserChangedError, match="DATE"):
        parse_cboe_csv(payload, b"DAY,CLOSE\n")
