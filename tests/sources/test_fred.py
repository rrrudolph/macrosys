"""FRED / ALFRED parsers against hand-written fixtures in the documented JSON shape (spec §9.2)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.fred import (
    FredSource,
    _vintage_windows,
    map_fred_unit,
    parse_observations,
    parse_release_dates,
    parse_series,
    parse_vintage_dates,
)
from tests.sources.fakes import MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)


def _obs_request(**extra: object) -> dict:
    request = {
        "method": "GET",
        "url": "https://api.stlouisfed.org/fred/series/observations",
        "params": {"series_id": "GDPC1", "file_type": "json"},
        "native_id": "GDPC1",
        "series_units": "Billions of Chained 2017 Dollars",
        "frequency": "Q",
    }
    request.update(extra)
    return request


def test_gdpc1_vintages_units_and_reconstructed_publication(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "fred" / "gdpc1_observations.json").read_bytes()
    _store, payload = stored("FRED", content, _obs_request(), FETCHED)
    source = FredSource(MemoryRawStore())
    assert source.tier == "record"
    rows = source.parse(payload, content)
    by_vintage = {(row.ref_period_start, row.vintage_id): row for row in rows}
    first = by_vintage[(date(2023, 10, 1), "2024-01-25")]
    assert first.series_id == "FRED:GDPC1"
    assert first.ref_period_end == date(2023, 12, 31)
    assert first.value == pytest.approx(22679.255)
    assert first.unit == "USD_bn_real2017"
    assert first.published_at == datetime(2024, 1, 25, tzinfo=UTC)
    assert first.quality_flag == "reconstructed_pubtime"
    assert first.ingested_at == FETCHED
    assert first.source_hash == payload.sha256
    revised = by_vintage[(date(2023, 10, 1), "2024-02-28")]
    assert revised.value == pytest.approx(22769.123)
    missing = by_vintage[(date(2024, 1, 1), "2024-02-28")]
    assert missing.value is None
    assert missing.ref_period_end == date(2024, 3, 31)


def test_release_clock_time_is_used_when_supplied(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "fred" / "gdpc1_observations.json").read_bytes()
    request = _obs_request(release_times={"2024-01-25": "2024-01-25T13:30:00+00:00"})
    _store, payload = stored("FRED", content, request, FETCHED)
    rows = parse_observations(payload, content)
    dated = [row for row in rows if row.vintage_id == "2024-01-25"]
    assert dated[0].published_at == datetime(2024, 1, 25, 13, 30, tzinfo=UTC)
    assert dated[0].quality_flag == "ok"


def test_percent_and_index_unit_map(fixtures_dir: Path):
    assert map_fred_unit("Percent") == "pct"
    assert map_fred_unit("Index 1982-1984=100") == "index"
    assert map_fred_unit("Index Jan 2006=100") == "index"
    assert map_fred_unit("Millions of U.S. Dollars") == "USD_mn"
    assert map_fred_unit("Thousands of Persons") == "persons_k"  # PAYEMS
    with pytest.raises(ValueError, match="no mapping"):
        map_fred_unit("Billions of Widgets")
    content = (fixtures_dir / "sources" / "fred" / "dgs10_observations.json").read_bytes()
    request = {
        "url": "https://api.stlouisfed.org/fred/series/observations",
        "params": {"series_id": "DGS10"},
        "native_id": "DGS10",
        "series_units": "Percent",
        "frequency": "D",
    }
    _store, payload = stored("FRED", content, request, FETCHED)
    row = parse_observations(payload, content)[0]
    assert row.series_id == "FRED:DGS10"
    assert row.unit == "pct"
    assert row.value == pytest.approx(3.95)
    assert row.ref_period_start == row.ref_period_end == date(2024, 1, 2)


def test_vintage_dates_and_release_calendar(fixtures_dir: Path):
    vintages = (fixtures_dir / "sources" / "fred" / "gdpc1_vintagedates.json").read_bytes()
    assert parse_vintage_dates(vintages) == [date(2024, 1, 25), date(2024, 2, 28)]
    releases = (fixtures_dir / "sources" / "fred" / "releases_dates.json").read_bytes()
    _store, payload = stored("FRED", releases, {"url": "https://api.stlouisfed.org/fred/releases/dates"}, FETCHED)
    events = parse_release_dates(payload, releases)
    assert events[0].release_id == "53"
    assert events[0].scheduled_at == datetime(2024, 1, 25, tzinfo=UTC)
    assert events[0].source_id == "FRED"
    series = parse_series((fixtures_dir / "sources" / "fred" / "gdpc1_series.json").read_bytes())
    assert series[0].series_id == "FRED:GDPC1"
    assert series[0].unit == "USD_bn_real2017"
    assert series[0].frequency == "Q"
    assert series[0].seasonal_adj == "SAAR"


def test_vintage_windows_cover_every_date_without_overlap():
    dates = [date(2020, 1, day) for day in range(1, 8)]
    windows = _vintage_windows(dates, width=3)
    assert windows == [
        (date(2020, 1, 1), date(2020, 1, 3)),
        (date(2020, 1, 4), date(2020, 1, 6)),
        (date(2020, 1, 7), date(2020, 1, 7)),
    ]


def test_mutated_observations_raise(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "fred" / "gdpc1_observations.json").read_bytes()
    mutated = json.loads(content)
    del mutated["observations"]
    _store, payload = stored("FRED", content, _obs_request(), FETCHED)
    with pytest.raises(ParserChangedError, match="observations"):
        parse_observations(payload, json.dumps(mutated).encode())
