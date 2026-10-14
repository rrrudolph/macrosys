"""Unofficial TradingView candles via the community library (spec §9.6).

This adapter shells out to Node and `@mathieuc/tradingview` (v4, pinned in `js/package.json`). It is an
unofficial community project, not endorsed by TradingView. TradingView's terms govern automated access
and redistribution. Rows are research-tier only: `source_id='TRADINGVIEW_UNOFFICIAL'`,
`quality_flag='unofficial'`. They cannot be record data and cannot by themselves satisfy a thesis gate
(spec §9.6).

`tv_candles.mjs` reads `{symbol, timeframe, count|from}` from stdin and writes one JSON line per candle
from `getCandles` (`@mathieuc/tradingview/data`). v4.0.0 takes account cookies as
`credentials: {session, signature}` on that call (the `sessionid` and `sessionid_sign` cookies), which
the script fills from env `TV_SESSION` and `TV_SIGNATURE`. Those variables are passed on the child
environment and are never logged. `searchMarkets` in v4.0.0 does not accept credentials; the env vars
are still present for the child. Tests must not run `npm install`; they mock `subprocess.run`.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from macrosys.core.contracts import (
    Observation,
    ParserChangedError,
    PriceBar,
    RawPayload,
    RawStore,
    ReleaseEvent,
    SeriesMeta,
    SourceTier,
)
from macrosys.core.units import assert_unit
from macrosys.sources.registry import require_source

PARSER_VERSION = "1"

_JS = Path(__file__).resolve().parent / "js"
_CANDLES = _JS / "tv_candles.mjs"
_SEARCH = _JS / "tv_search.mjs"


def _redact(text: str) -> str:
    cleaned = text
    for name in ("TV_SESSION", "TV_SIGNATURE"):
        secret = os.environ.get(name)
        if secret:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def _child_env() -> dict[str, str]:
    """Copy the process environment, including optional TV_SESSION and TV_SIGNATURE."""
    return dict(os.environ)


def _run(script: Path, payload: dict[str, Any], *, timeout: float) -> str:
    completed = subprocess.run(
        ["node", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=timeout,
        cwd=str(_JS),
        env=_child_env(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"TradingView node runner failed: {_redact(completed.stderr).strip()}")
    return completed.stdout


def _trade_date(timestamp: int | float) -> date:
    seconds = float(timestamp)
    if seconds > 10**11:
        seconds /= 1000.0
    return datetime.fromtimestamp(seconds, UTC).date()


def parse_candle_lines(text: str, *, symbol: str, ccy: str, ingested_at: datetime) -> list[PriceBar]:
    """Parse newline-delimited candle JSON into research-tier bars (spec §9.6)."""
    cfg = require_source("TRADINGVIEW_UNOFFICIAL")
    assert_unit(ccy)
    bars: list[PriceBar] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParserChangedError("TradingView candle line is not JSON") from exc
        if not isinstance(item, dict):
            raise ParserChangedError("TradingView candle line is not an object")
        missing = [key for key in ("time", "open", "high", "low", "close") if key not in item]
        if missing:
            raise ParserChangedError(f"TradingView candle is missing {missing}")
        volume = item.get("volume")
        bars.append(
            PriceBar(
                instrument_id=f"TV:{symbol}",
                trade_date=_trade_date(item["time"]),
                open=float(item["open"]) if item["open"] is not None else None,
                high=float(item["high"]) if item["high"] is not None else None,
                low=float(item["low"]) if item["low"] is not None else None,
                close=float(item["close"]),
                volume=float(volume) if volume is not None else None,
                ccy=ccy,
                source_id=cfg.source_id,
                tier=cfg.tier,
                ingested_at=ingested_at,
                quality_flag="unofficial",
            )
        )
    return bars


class TradingViewSource:
    """Research-tier TradingView candles (spec §9.6). Env: `TV_SESSION`, `TV_SIGNATURE` (both optional)."""

    source_id = "TRADINGVIEW_UNOFFICIAL"
    PARSER_VERSION = PARSER_VERSION

    def __init__(self, raw: RawStore | None = None, *, now: Any = None, timeout: float = 60.0) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.timeout = timeout
        self._now = now or (lambda: datetime.now(UTC))

    def discover(self) -> list[SeriesMeta]:
        """Symbol discovery is `search` (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """TradingView is a Node subprocess. Use `candles` (spec §9.6)."""
        raise RuntimeError("TradingView has no HTTP fetch; call candles(symbol, timeframe, count)")

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Candle lines are price bars, not observations (spec §9.1, §9.6)."""
        symbol = str(payload.request.get("symbol") or "")
        ccy = str(payload.request.get("ccy") or "")
        if not symbol or not ccy:
            raise ParserChangedError("TradingView payload request needs symbol and ccy")
        parse_candle_lines(content.decode("utf-8"), symbol=symbol, ccy=ccy, ingested_at=fetched(payload))
        return []

    def schedule(self) -> list[ReleaseEvent]:
        """Unofficial candles have no release calendar (spec §9.1). Returns []."""
        return []

    def candles(self, symbol: str, timeframe: str, count: int, *, ccy: str, timeout: float | None = None) -> list[PriceBar]:
        """Run `tv_candles.mjs` and parse its candle lines (spec §9.6).

        `ccy` is required because the candle object has no currency. Credentials, when set, travel only
        in the child environment.
        """
        stdout = _run(
            _CANDLES,
            {"symbol": symbol, "timeframe": timeframe, "count": count},
            timeout=self.timeout if timeout is None else timeout,
        )
        return parse_candle_lines(stdout, symbol=symbol, ccy=ccy, ingested_at=self._now())

    def search(self, query: str, *, type: str | None = None, timeout: float | None = None) -> list[dict[str, Any]]:
        """Run `tv_search.mjs` (`searchMarkets`) and return the JSON array (spec §9.6)."""
        body: dict[str, Any] = {"query": query}
        if type is not None:
            body["type"] = type
        stdout = _run(_SEARCH, body, timeout=self.timeout if timeout is None else timeout)
        try:
            found = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ParserChangedError("TradingView search output is not JSON") from exc
        if not isinstance(found, list):
            raise ParserChangedError("TradingView search output is not a list")
        return found


def fetched(payload: RawPayload) -> datetime:
    """Ingestion time for a stored candle payload (spec §9.2)."""
    if payload.fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    return payload.fetched_at
