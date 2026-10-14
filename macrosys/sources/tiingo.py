"""Tiingo end-of-day prices (spec §9.1, §9.6).

The Starter caps (50 requests/hour, 1_000/day, 500 unique symbols/month) are read from the registry.
The hourly and daily caps are the HTTP client's token bucket. Unique symbols are tracked in a JSON
file whose path the caller passes in; the 501st distinct symbol in a calendar month is refused.
Auth is env `TIINGO_API_KEY`, sent as the `Authorization` header value (Tiingo expects `Token <key>`).

Bars use the unadjusted OHLC. `divCash > 0` and `splitFactor != 1` become corporate-action rows so
total return can be rebuilt (spec §9.1). The EOD document has no currency; `ccy` defaults to `USD`
for US listings on this feed and can be overridden on the stored request ([J]). Env: `TIINGO_API_KEY`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
from macrosys.sources.common import fetched_at, load_json
from macrosys.sources.records import CorporateActionRow
from macrosys.sources.registry import HttpClient, require_source

PARSER_VERSION = "1"

_BAR_KEYS = ("date", "open", "high", "low", "close", "volume", "divCash", "splitFactor")


class UniqueSymbolTracker:
    """Monthly unique-symbol budget for Tiingo Starter (spec §9.6).

    The JSON state is `{"month": "YYYY-MM", "symbols": [...]}`. A symbol already seen this month
    does not consume another slot. `today` is injectable so tests do not depend on the wall clock.
    """

    def __init__(self, path: Path, *, limit: int, today: date | None = None) -> None:
        self.path = path
        self.limit = limit
        self.today = today

    def _current_month(self) -> str:
        day = self.today or date.today()
        return f"{day.year:04d}-{day.month:02d}"

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"month": self._current_month(), "symbols": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise ValueError(f"{self.path} is not a Tiingo symbol-budget file")
        return data

    def allow(self, symbol: str) -> None:
        """Record `symbol` or raise when this month's unique-symbol cap is already full."""
        key = symbol.strip().upper()
        if not key:
            raise ValueError("Tiingo symbol is empty")
        state = self._read()
        month = self._current_month()
        if state.get("month") != month:
            state = {"month": month, "symbols": []}
        symbols: list[str] = [str(item) for item in state["symbols"]]
        if key in symbols:
            return
        if len(symbols) >= self.limit:
            raise RuntimeError(f"TIINGO unique-symbol cap is {self.limit} for {month}; refusing {key}")
        symbols.append(key)
        state["symbols"] = symbols
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_eod(payload: RawPayload, content: bytes) -> tuple[list[PriceBar], list[CorporateActionRow]]:
    """Parse a Tiingo EOD JSON array into price bars and corporate actions (spec §9.1, §9.6)."""
    doc = load_json(content, what="Tiingo EOD")
    if not isinstance(doc, list):
        raise ParserChangedError("Tiingo EOD payload is not a JSON array")
    cfg = require_source("TIINGO")
    instrument = payload.request.get("instrument_id")
    if not isinstance(instrument, str) or not instrument:
        raise ParserChangedError("Tiingo payload request has no instrument_id")
    ccy = str(payload.request.get("ccy") or "USD")
    assert_unit(ccy)
    ingested = fetched_at(payload)
    bars: list[PriceBar] = []
    actions: list[CorporateActionRow] = []
    for item in doc:
        if not isinstance(item, dict):
            raise ParserChangedError("Tiingo EOD row is not an object")
        missing = [key for key in _BAR_KEYS if key not in item]
        if missing:
            raise ParserChangedError(f"Tiingo EOD row is missing {missing}")
        trade_date = date.fromisoformat(str(item["date"])[:10])
        close = item["close"]
        if close is None:
            raise ParserChangedError("Tiingo EOD close is null")
        bars.append(
            PriceBar(
                instrument_id=instrument,
                trade_date=trade_date,
                open=_optional_float(item["open"]),
                high=_optional_float(item["high"]),
                low=_optional_float(item["low"]),
                close=float(close),
                volume=_optional_float(item["volume"]),
                ccy=ccy,
                source_id=cfg.source_id,
                tier=cfg.tier,
                ingested_at=ingested,
                quality_flag="ok",
            )
        )
        dividend = float(item["divCash"] or 0)
        if dividend > 0:
            actions.append(
                CorporateActionRow(
                    instrument_id=instrument,
                    ex_date=trade_date,
                    type="cash_div",
                    amount=dividend,
                    ccy=ccy,
                )
            )
        split = float(item["splitFactor"])
        if abs(split - 1.0) > 1e-12:
            actions.append(
                CorporateActionRow(
                    instrument_id=instrument,
                    ex_date=trade_date,
                    type="split",
                    ratio=split,
                    ccy=ccy,
                )
            )
    return bars, actions


class TiingoSource:
    """Tiingo EOD `DataSource` (spec §9.6). Env: `TIINGO_API_KEY`."""

    source_id = "TIINGO"
    PARSER_VERSION = PARSER_VERSION

    def __init__(
        self,
        raw: RawStore,
        symbol_state_path: Path,
        *,
        transport: Any = None,
        sleeper: Any = None,
        today: date | None = None,
    ) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self.client = HttpClient(self.cfg, raw, transport=transport, sleeper=sleeper)
        limit = self.cfg.rate_limit.unique_symbols_per_month if self.cfg.rate_limit else None
        if not limit:
            raise RuntimeError("TIINGO registry entry has no unique_symbols_per_month")
        self.symbols = UniqueSymbolTracker(symbol_state_path, limit=limit, today=today)

    def discover(self) -> list[SeriesMeta]:
        """Tiingo has no series catalogue in this connector (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """Download EOD prices after reserving the symbol against the monthly unique cap (spec §9.6)."""
        ticker = series_id.removeprefix("TIINGO:").upper()
        self.symbols.allow(ticker)
        client = self.client if raw is self.raw else HttpClient(self.cfg, raw, transport=self.client.transport)
        base = self.cfg.access.base_url or ""
        params: dict[str, Any] = {}
        if since is not None:
            params["startDate"] = since.isoformat()
        return client.get(
            urljoin(base, f"daily/{ticker}/prices"),
            params=params,
            extra_request={"instrument_id": f"TIINGO:{ticker}", "ccy": "USD"},
        )

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """Validate the EOD document. Price rows are returned by `parse_eod`, not as observations.

        A schema change still raises ParserChangedError (spec §9.4). Spec §9.1 keeps price bars off
        the observation table.
        """
        parse_eod(payload, content)
        return []

    def schedule(self) -> list[ReleaseEvent]:
        """EOD files have no macro release calendar (spec §9.1). Returns []."""
        return []

