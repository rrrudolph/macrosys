"""TradingView runner with subprocess.run mocked. Does not install the Node package."""

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.core.contracts import ParserChangedError
from macrosys.sources.tradingview import TradingViewSource, parse_candle_lines

WHEN = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def test_candles_are_unofficial_research(monkeypatch: pytest.MonkeyPatch, fixtures_dir: Path):
    monkeypatch.setenv("TV_SESSION", "sess-secret")
    monkeypatch.setenv("TV_SIGNATURE", "sig-secret")
    stdout = (fixtures_dir / "sources" / "tradingview" / "candles.ndjson").read_text(encoding="utf-8")
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("macrosys.sources.tradingview.subprocess.run", fake_run)
    source = TradingViewSource(now=lambda: WHEN)
    assert source.tier == "research"
    bars = source.candles("NASDAQ:AAPL", "D", 2, ccy="USD")
    assert captured["args"][0] == "node"
    assert captured["args"][1].endswith("tv_candles.mjs")
    assert json.loads(captured["kwargs"]["input"])["symbol"] == "NASDAQ:AAPL"
    assert captured["kwargs"]["env"]["TV_SESSION"] == "sess-secret"
    assert captured["kwargs"]["env"]["TV_SIGNATURE"] == "sig-secret"
    assert bars[0].source_id == "TRADINGVIEW_UNOFFICIAL"
    assert bars[0].tier == "research"
    assert bars[0].quality_flag == "unofficial"
    assert bars[0].instrument_id == "TV:NASDAQ:AAPL"
    assert bars[0].trade_date == date(2024, 1, 2)
    assert bars[0].close == 1.5
    assert bars[0].ccy == "USD"
    assert bars[0].ingested_at == WHEN
    assert "sess-secret" not in bars[0].model_dump_json()
    assert "sig-secret" not in bars[0].model_dump_json()


def test_failed_runner_redacts_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TV_SESSION", "sess-secret")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="denied sess-secret")

    monkeypatch.setattr("macrosys.sources.tradingview.subprocess.run", fake_run)
    source = TradingViewSource()
    with pytest.raises(RuntimeError, match="denied") as exc:
        source.candles("NASDAQ:AAPL", "D", 1, ccy="USD")
    assert "sess-secret" not in str(exc.value)
    assert "***" in str(exc.value)


def test_mutated_candle_line_raises():
    with pytest.raises(ParserChangedError, match="close"):
        parse_candle_lines(
            '{"time": 1704153600, "open": 1, "high": 2, "low": 0.5, "volume": 1}\n',
            symbol="NASDAQ:AAPL",
            ccy="USD",
            ingested_at=WHEN,
        )
