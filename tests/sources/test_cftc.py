"""CFTC TFF parser against a captured Socrata page (dataset gpe5-46if)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.cftc import CftcSource, parse_socrata
from macrosys.sources.registry import require_source
from tests.sources.fakes import MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 20, 0, tzinfo=UTC)


def test_tff_positions_and_reconstructed_friday_release(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "cftc" / "tff_futures.json").read_bytes()
    request = {
        "url": "https://publicreporting.cftc.gov/resource/gpe5-46if.json",
        "params": {},
        "dataset_key": "tff_futures",
    }
    _store, payload = stored("CFTC", content, request, FETCHED)
    source = CftcSource(MemoryRawStore())
    assert source.tier == "record"
    rows = source.parse(payload, content)
    interest = next(row for row in rows if row.series_id == "CFTC:TFF:344607:open_interest_all")
    assert interest.value == pytest.approx(31328)
    assert interest.unit == "count"
    assert interest.ref_period_start == date(2026, 10, 6)
    assert interest.published_at == datetime(2026, 10, 9, 19, 30, tzinfo=UTC)
    assert interest.quality_flag == "reconstructed_pubtime"
    share = next(row for row in rows if row.series_id == "CFTC:TFF:344607:pct_of_oi_dealer_long_all")
    assert share.unit == "pct"
    assert share.value == pytest.approx(10.4)
    assert any(row.series_id.startswith("CFTC:TFF:344606:") for row in rows)


def test_schedule_uses_registry_friday_slot():
    source = CftcSource(MemoryRawStore())
    event = source.schedule(today=date(2026, 10, 14))[0]
    assert event.release_id == "CFTC:COT"
    assert event.scheduled_at == datetime(2026, 10, 16, 19, 30, tzinfo=UTC)
    assert source.cfg.schedule.weekday == "Fri"
    assert source.cfg.schedule.time_local == "15:30"


def test_fetch_pages_with_where_offset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SOCRATA_APP_TOKEN", raising=False)
    dataset = require_source("CFTC").datasets["tff_futures"]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        offset = int(request.url.params["$offset"])
        if offset == 0:
            return httpx.Response(200, json=[{"report_date_as_yyyy_mm_dd": "2026-10-06T00:00:00.000", "cftc_contract_market_code": "1", "open_interest_all": "1"}])
        return httpx.Response(200, json=[])

    store = MemoryRawStore()
    source = CftcSource(store, transport=httpx.MockTransport(handler))
    pages = source.fetch_dataset("tff_futures", date(2026, 10, 1), store, page_size=1)
    assert len(pages) == 2
    assert dataset in str(calls[0].url)
    assert "report_date_as_yyyy_mm_dd > '2026-10-01'" in calls[0].url.params["$where"]
    assert calls[1].url.params["$offset"] == "1"


def test_mutated_row_raises(fixtures_dir: Path):
    rows = json.loads((fixtures_dir / "sources" / "cftc" / "tff_futures.json").read_text(encoding="utf-8"))
    del rows[0]["cftc_contract_market_code"]
    content = json.dumps(rows).encode()
    _store, payload = stored("CFTC", content, {"dataset_key": "tff_futures", "url": "https://example.test"}, FETCHED)
    with pytest.raises(ParserChangedError, match="cftc_contract_market_code"):
        parse_socrata(payload, content)
