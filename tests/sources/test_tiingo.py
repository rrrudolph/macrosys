"""Tiingo EOD parser (hand-written from the documented JSON) and the unique-symbol cap."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.tiingo import TiingoSource, parse_eod
from tests.sources.fakes import MemoryRawStore, stored

FETCHED = datetime(2026, 10, 14, 22, 0, tzinfo=UTC)


def test_eod_bars_and_corporate_actions(fixtures_dir: Path):
    content = (fixtures_dir / "sources" / "tiingo" / "aapl_eod.json").read_bytes()
    request = {
        "url": "https://api.tiingo.com/tiingo/daily/AAPL/prices",
        "params": {},
        "instrument_id": "TIINGO:AAPL",
        "ccy": "USD",
    }
    _store, payload = stored("TIINGO", content, request, FETCHED)
    source = TiingoSource(MemoryRawStore(), Path("unused.json"))
    assert source.tier == "record"
    bars, actions = parse_eod(payload, content)
    assert source.parse(payload, content) == []
    assert [bar.trade_date for bar in bars] == [date(2024, 1, 2), date(2024, 2, 9), date(2024, 6, 10)]
    assert bars[0].close == pytest.approx(185.5)
    assert bars[0].ccy == "USD"
    assert bars[0].source_id == "TIINGO"
    assert bars[0].tier == "record"
    assert bars[0].quality_flag == "ok"
    assert bars[2].close == pytest.approx(205.0)
    kinds = {(row.type, row.ex_date): row for row in actions}
    dividend = kinds[("cash_div", date(2024, 2, 9))]
    assert dividend.amount == pytest.approx(0.24)
    assert dividend.ccy == "USD"
    split = kinds[("split", date(2024, 6, 10))]
    assert split.ratio == pytest.approx(2.0)


def test_mutated_eod_raises(fixtures_dir: Path):
    rows = json.loads((fixtures_dir / "sources" / "tiingo" / "aapl_eod.json").read_text(encoding="utf-8"))
    del rows[0]["close"]
    content = json.dumps(rows).encode()
    _store, payload = stored("TIINGO", content, {"instrument_id": "TIINGO:AAPL", "ccy": "USD"}, FETCHED)
    with pytest.raises(ParserChangedError, match="close"):
        parse_eod(payload, content)


def test_unique_symbol_tracker_refuses_the_501st(tmp_path: Path):
    source = TiingoSource(MemoryRawStore(), tmp_path / "symbols.json", today=date(2026, 10, 14))
    assert source.symbols.limit == 500
    for index in range(500):
        source.symbols.allow(f"S{index}")
    source.symbols.allow("s0")
    with pytest.raises(RuntimeError, match="501|unique-symbol"):
        source.symbols.allow("S500")
    source.symbols.today = date(2026, 11, 1)
    source.symbols.allow("FRESH")
