"""ECB SDMX csvdata parser. Daily and monthly rows are a captured EXR extract; the quarterly file is BSI."""

import csv
from datetime import UTC, date, datetime
from io import StringIO
from pathlib import Path

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.ecb import EcbSource, parse_csvdata
from tests.sources.fakes import MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


def _request(url: str) -> dict:
    return {"method": "GET", "url": url, "params": {"format": "csvdata"}}


def test_exr_daily_and_monthly_are_ratios_with_reconstructed_time(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "ecb" / "exr_usd.csv").read_bytes()
    _store, payload = stored(
        "ECB",
        content,
        _request("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"),
        FETCHED,
        content_type="text/csv",
    )
    source = EcbSource(MemoryRawStore())
    assert source.tier == "record"
    rows = { (row.series_id, row.ref_period_start): row for row in source.parse(payload, content) }
    daily = rows[("ECB:EXR.D.USD.EUR.SP00.A", date(2024, 1, 2))]
    assert daily.value == pytest.approx(1.0956)
    assert daily.unit == "ratio"
    assert daily.ref_period_end == date(2024, 1, 2)
    assert daily.published_at == FETCHED
    assert daily.quality_flag == "reconstructed_pubtime"
    assert daily.vintage_id == payload.sha256
    monthly = rows[("ECB:EXR.M.USD.EUR.SP00.A", date(2024, 1, 1))]
    assert monthly.ref_period_end == date(2024, 1, 31)
    assert monthly.value == pytest.approx(1.0905136363636)
    assert monthly.unit == "ratio"


def test_bsi_quarter_uses_unit_multiplier(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "ecb" / "bsi_loans_q.csv").read_bytes()
    _store, payload = stored(
        "ECB",
        content,
        _request("https://data-api.ecb.europa.eu/service/data/BSI/Q.U2.N.A.A20.A.1.U2.2240.Z01.E"),
        FETCHED,
        content_type="text/csv",
    )
    row = parse_csvdata(payload, content)[0]
    assert row.series_id == "ECB:BSI.Q.U2.N.A.A20.A.1.U2.2240.Z01.E"
    assert row.ref_period_start == date(2023, 10, 1)
    assert row.ref_period_end == date(2023, 12, 31)
    assert row.unit == "EUR_mn"
    assert row.value == pytest.approx(5113385.80920022)
    assert row.quality_flag == "reconstructed_pubtime"


def test_compilation_timestamp_is_the_publication_time(fixtures_dir: Path):
    text = (fixtures_dir / "sources" / "ecb" / "exr_usd.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(StringIO(text))
    fieldnames = list(reader.fieldnames or [])
    row = next(reader)
    row["COMPILATION"] = "2024-01-02T15:15:00+01:00"
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerow(row)
    content = buffer.getvalue().encode()
    _store, payload = stored("ECB", content, _request("https://data-api.ecb.europa.eu/service/data/EXR"), FETCHED)
    parsed = parse_csvdata(payload, content)[0]
    assert parsed.published_at == datetime(2024, 1, 2, 14, 15, tzinfo=UTC)
    assert parsed.quality_flag == "ok"


def test_missing_column_raises(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "ecb" / "exr_usd.csv").read_bytes()
    mutated = content.replace(b"OBS_VALUE", b"OBS_VALUEX")
    _store, payload = stored("ECB", mutated, _request("https://data-api.ecb.europa.eu/service/data/EXR"), FETCHED)
    with pytest.raises(ParserChangedError, match="OBS_VALUE"):
        parse_csvdata(payload, mutated)
