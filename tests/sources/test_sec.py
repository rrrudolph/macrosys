"""SEC companyfacts (captured, trimmed) and an N-PORT ZIP built from the readme's TSV columns."""

import io
import json
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.iso3166 import ISO2_TO_ISO3, to_iso3
from macrosys.sources.sec import SecSource, parse_companyfacts, parse_nport_zip
from tests.sources.fakes import MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 21, 0, tzinfo=UTC)


def _zip_nport(fixtures_dir: Path, *, skip: str | None = None) -> bytes:
    folder = fixtures_dir / "sources" / "sec" / "nport"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(folder.glob("*.tsv")):
            if path.name == skip:
                continue
            archive.writestr(path.name, path.read_bytes())
    return buffer.getvalue()


def test_companyfacts_filed_end_of_day_and_accession(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "sec" / "companyfacts_aapl.json").read_bytes()
    request = {
        "url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        "params": {},
    }
    _store, payload = stored("SEC", content, request, FETCHED)
    source = SecSource(MemoryRawStore())
    assert source.tier == "record"
    rows = source.parse(payload, content)
    assert len(rows) == 3
    annual = next(row for row in rows if row.ref_period_end == date(2016, 9, 24))
    assert annual.series_id == "SEC:0000320193:us-gaap:Revenues:USD"
    assert annual.unit == "USD"
    assert annual.value == pytest.approx(215639000000)
    assert annual.ref_period_start == date(2015, 9, 27)
    assert annual.vintage_id == "0000320193-18-000145"
    assert annual.published_at == datetime(2018, 11, 6, 4, 59, 59, tzinfo=UTC)
    assert annual.quality_flag == "ok"
    assert annual.ingested_at == FETCHED


def test_mutated_companyfacts_raise(fixtures_dir: Path):
    doc = json.loads((fixtures_dir / "sources" / "sec" / "companyfacts_aapl.json").read_text(encoding="utf-8"))
    del doc["facts"]
    content = json.dumps(doc).encode()
    _store, payload = stored("SEC", content, {"url": "https://data.sec.gov/"}, FETCHED)
    with pytest.raises(ParserChangedError, match="facts"):
        parse_companyfacts(payload, content)


def test_nport_holdings_from_readme_columns(fixtures_dir: Path):
    assert to_iso3("US") == "USA" and to_iso3("DE") == "DEU"
    assert all(len(key) == 2 and len(value) == 3 for key, value in ISO2_TO_ISO3.items())
    content = _zip_nport(fixtures_dir)
    rows = {row.holding_key: row for row in parse_nport_zip(content)}
    apple = rows["US0378331005"]
    assert apple.instrument_id == "S000001234"
    assert apple.as_of == date(2024, 3, 31)
    assert apple.published_at == datetime(2024, 5, 31, 3, 59, 59, tzinfo=UTC)
    assert apple.source == "SEC"
    assert apple.name == "Apple Inc."
    assert apple.weight == pytest.approx(0.025)
    assert apple.country == "USA"
    assert apple.sector is None
    assert apple.ccy == "USD"
    assert apple.asset_class == "equity"
    assert apple.market_value == pytest.approx(25_000_000)
    assert apple.ccy_mv == "USD"
    bund = rows["DE0001102481"]
    assert bund.asset_class == "fixed_income"
    assert bund.country == "DEU"
    assert bund.weight == pytest.approx(0.01)
    assert bund.ccy == "EUR"


def test_nport_missing_table_raises(fixtures_dir: Path):
    content = _zip_nport(fixtures_dir, skip="FUND_REPORTED_HOLDING.tsv")
    with pytest.raises(ParserChangedError, match="FUND_REPORTED_HOLDING"):
        parse_nport_zip(content)
