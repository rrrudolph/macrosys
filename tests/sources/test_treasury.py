"""Treasury par-yield XML (captured feed, two days) and a trimmed Fed GSW CSV."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.treasury import (
    FedGswSource,
    TreasurySource,
    parse_gsw,
    parse_par_yields,
    treasury_xml_url,
)
from tests.sources.fakes import MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


def test_treasury_url_matches_the_documented_feed():
    url = treasury_xml_url(2024)
    assert url == (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
        "?data=daily_treasury_yield_curve&field_tdr_date_value=2024"
    )


def test_par_yields_one_series_per_tenor(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "treasury" / "daily_par_yield.xml").read_bytes()
    _store, payload = stored(
        "TREASURY",
        content,
        {"url": treasury_xml_url(2024), "params": {}},
        FETCHED,
        content_type="text/xml",
    )
    source = TreasurySource(MemoryRawStore())
    assert source.tier == "record"
    rows = source.parse(payload, content)
    ten_year = next(row for row in rows if row.series_id == "TREASURY:PAR:10Y" and row.ref_period_start == date(2024, 1, 2))
    assert ten_year.value == pytest.approx(3.95)
    assert ten_year.unit == "pct"
    assert ten_year.published_at == datetime(2024, 1, 3, 4, 59, 59, tzinfo=UTC)
    assert ten_year.quality_flag == "reconstructed_pubtime"
    assert ten_year.ref_period_end == date(2024, 1, 2)
    series = {row.series_id for row in rows if row.ref_period_start == date(2024, 1, 2)}
    assert "TREASURY:PAR:30YDISPLAY" not in series
    assert series == {
        "TREASURY:PAR:1M",
        "TREASURY:PAR:2M",
        "TREASURY:PAR:3M",
        "TREASURY:PAR:4M",
        "TREASURY:PAR:6M",
        "TREASURY:PAR:1Y",
        "TREASURY:PAR:2Y",
        "TREASURY:PAR:3Y",
        "TREASURY:PAR:5Y",
        "TREASURY:PAR:7Y",
        "TREASURY:PAR:10Y",
        "TREASURY:PAR:20Y",
        "TREASURY:PAR:30Y",
    }


def test_mutated_treasury_feed_raises():
    _store, payload = stored("TREASURY", b"<feed></feed>", {"url": treasury_xml_url(2024)}, FETCHED)
    with pytest.raises(ParserChangedError, match="properties"):
        parse_par_yields(payload, b"<feed></feed>")


def test_gsw_sveny_beta_and_tau(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "fedgsw" / "feds200628_sample.csv").read_bytes()
    url = "https://www.federalreserve.gov/data/yield-curve-tables/feds200628.csv"
    _store, payload = stored("FEDGSW", content, {"url": url, "params": {}}, FETCHED, content_type="text/csv")
    source = FedGswSource(MemoryRawStore())
    assert source.tier == "record"
    rows = source.parse(payload, content)
    sveny = next(row for row in rows if row.series_id == "FEDGSW:SVENY01")
    assert sveny.value == pytest.approx(2.9825)
    assert sveny.unit == "pct"
    assert sveny.ref_period_start == date(1961, 6, 14)
    assert sveny.published_at == FETCHED
    assert sveny.quality_flag == "reconstructed_pubtime"
    beta = next(row for row in rows if row.series_id == "FEDGSW:BETA0")
    assert beta.unit == "pct"
    assert beta.value == pytest.approx(3.91760612208239)
    tau = next(row for row in rows if row.series_id == "FEDGSW:TAU1")
    assert tau.unit == "y"
    assert tau.value == pytest.approx(0.339217585301823)
    assert all(row.ref_period_start != date(1961, 7, 4) for row in rows)


def test_mutated_gsw_header_raises(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "fedgsw" / "feds200628_sample.csv").read_bytes().replace(b"Date,", b"Day,", 1)
    _store, payload = stored("FEDGSW", content, {"url": "https://example.test"}, FETCHED)
    with pytest.raises(ParserChangedError, match="Date"):
        parse_gsw(payload, content)
