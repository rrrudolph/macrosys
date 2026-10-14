"""MetaTrader 5 cross-check prices (spec §9.6).

`MetaTrader5` is imported inside the functions that talk to the terminal. The package is Windows-only
and may be absent; importing this module does not import it. Login is env `MT5_LOGIN`, `MT5_PASSWORD`,
and `MT5_SERVER` unless the constructor is given those values. The password is never logged.

`rates` calls `copy_rates_range` and `symbol_info`. Digits, contract size, profit currency, and any
session table the terminal object exposes are stored on a `SymbolSpec`. The Python package's documented
`symbol_info` tuple does not include the weekly session calendar (Appendix B); when `sessions` is
absent the bar close is the open plus the timeframe, and `session_note` says so. Rows use
`source_id='MT5'` and the registry tier (`cross_check`). They are not record prices.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

from macrosys.core.contracts import (
    Observation,
    ParserChangedError,
    RawPayload,
    RawStore,
    ReleaseEvent,
    SeriesMeta,
    SourceTier,
)
from macrosys.sources.records import Mt5PriceBar, SymbolSpec
from macrosys.sources.registry import require_source

PARSER_VERSION = "1"

# MQL5 timeframe constants (seconds). Values from the MetaTrader 5 Python docs (Appendix B).
_TIMEFRAME_SECONDS = {
    1: 60,
    5: 300,
    15: 900,
    30: 1800,
    16385: 3600,
    16388: 14_400,
    16408: 86_400,
    32769: 604_800,
}

_DAY_NAMES = {
    0: "monday",
    1: "tuesday",
    2: "wednesday",
    3: "thursday",
    4: "friday",
    5: "saturday",
    6: "sunday",
}


def _import_mt5() -> Any:
    """Import MetaTrader5 lazily so machines without the terminal can still import this module."""
    import MetaTrader5

    return MetaTrader5


def _credentials(login: Any, password: Any, server: Any) -> tuple[int, str, str]:
    import os

    resolved_login = login if login is not None else os.environ["MT5_LOGIN"]
    resolved_password = password if password is not None else os.environ["MT5_PASSWORD"]
    resolved_server = server if server is not None else os.environ["MT5_SERVER"]
    return int(resolved_login), str(resolved_password), str(resolved_server)


def _sessions(info: Any) -> list[dict[str, str]]:
    raw = getattr(info, "sessions", None)
    if not raw:
        return []
    rows: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            rows.append({str(key): str(value) for key, value in item.items()})
            continue
        rows.append(
            {
                "day": str(getattr(item, "day", "")),
                "open": str(getattr(item, "open", getattr(item, "from_time", ""))),
                "close": str(getattr(item, "close", getattr(item, "to_time", ""))),
            }
        )
    return rows


def _symbol_spec(info: Any, symbol: str) -> SymbolSpec:
    missing = [name for name in ("digits", "trade_contract_size", "currency_profit") if not hasattr(info, name)]
    if info is None or missing:
        raise ParserChangedError(f"MT5 symbol_info for {symbol} is missing {missing or 'the object'}")
    return SymbolSpec(
        symbol=symbol,
        digits=int(info.digits),
        trade_sessions=_sessions(info),
        contract_size=float(info.trade_contract_size),
        currency=str(info.currency_profit),
        source_id="MT5",
    )


def _iter_rates(rates: Any) -> list[dict[str, Any]]:
    if rates is None:
        return []
    names = getattr(getattr(rates, "dtype", None), "names", None)
    rows: list[dict[str, Any]] = []
    if names:
        for record in rates:
            rows.append({name: record[name] for name in names})
        return rows
    for record in rates:
        rows.append(dict(record))
    return rows


def _as_float(value: Any) -> float:
    item = value.item() if hasattr(value, "item") else value
    return float(item)


def _as_int(value: Any) -> int:
    item = value.item() if hasattr(value, "item") else value
    return int(item)


def _close_time(open_at: datetime, timeframe: int, sessions: list[dict[str, str]]) -> tuple[datetime, str]:
    day_name = _DAY_NAMES[open_at.weekday()]
    closes = []
    for session in sessions:
        label = str(session.get("day", "")).strip().lower()
        if label not in {day_name, day_name[:3], str(open_at.weekday())}:
            continue
        clock = str(session.get("close", "")).strip()
        if not clock:
            continue
        hour, minute = (int(part) for part in clock.split(":", 1))
        closes.append(open_at.replace(hour=hour, minute=minute, second=0, microsecond=0))
    if closes:
        return max(closes), "trade_session"
    seconds = _TIMEFRAME_SECONDS.get(timeframe)
    if seconds is None:
        return open_at, "unmapped_timeframe"
    return open_at + timedelta(seconds=seconds), "nominal_timeframe"


def _volume(row: dict[str, Any]) -> float | None:
    real = row.get("real_volume")
    tick = row.get("tick_volume")
    # [J] Broker FX bars often report real_volume 0 and a tick count instead.
    if real is not None and _as_float(real) > 0:
        return _as_float(real)
    if tick is None:
        return None
    return _as_float(tick)


class MT5Source:
    """MetaTrader 5 cross-check `DataSource` (spec §9.6). Env: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`."""

    source_id = "MT5"
    PARSER_VERSION = PARSER_VERSION

    def __init__(
        self,
        raw: RawStore | None = None,
        *,
        login: int | str | None = None,
        password: str | None = None,
        server: str | None = None,
        now: Any = None,
    ) -> None:
        self.cfg = require_source(self.source_id)
        self.tier: SourceTier = self.cfg.tier
        self.raw = raw
        self._login = login
        self._password = password
        self._server = server
        self._now = now or (lambda: datetime.now(UTC))

    def discover(self) -> list[SeriesMeta]:
        """Symbols come from the running terminal, not a static catalogue (spec §9.6). Returns []."""
        return []

    def fetch(self, series_id: str, since: date | None, raw: RawStore) -> RawPayload:
        """MT5 is local IPC. Use `rates`; this method does not speak HTTP (spec §9.6)."""
        raise RuntimeError("MT5 has no HTTP fetch; call rates(symbol, timeframe, start, end)")

    def parse(self, payload: RawPayload, content: bytes) -> list[Observation]:
        """MT5 bars are not observations (spec §9.1, §9.6)."""
        raise ParserChangedError("MT5 payloads are price bars; call rates()")

    def schedule(self) -> list[ReleaseEvent]:
        """Broker bars have no release calendar (spec §9.1). Returns []."""
        return []

    def rates(self, symbol: str, timeframe: int, start: datetime, end: datetime) -> tuple[SymbolSpec, list[Mt5PriceBar]]:
        """Copy bars for `symbol` and record the symbol spec (spec §9.6).

        `session_note` on each bar records `close_time` and whether it came from a trade session or
        from the timeframe length. Tests inject a fake module at `sys.modules['MetaTrader5']`.
        """
        mt5 = _import_mt5()
        login, password, server = _credentials(self._login, self._password, self._server)
        if not mt5.initialize(login=login, password=password, server=server):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        select = getattr(mt5, "symbol_select", None)
        if select is not None:
            select(symbol, True)
        info = mt5.symbol_info(symbol)
        spec = _symbol_spec(info, symbol)
        copied = mt5.copy_rates_range(symbol, timeframe, start, end)
        if copied is None:
            raise RuntimeError(f"MT5 copy_rates_range failed: {mt5.last_error()}")
        ingested = self._now()
        bars: list[Mt5PriceBar] = []
        for row in _iter_rates(copied):
            if "time" not in row or "close" not in row:
                raise ParserChangedError("MT5 rate row is missing time or close")
            open_at = datetime.fromtimestamp(_as_int(row["time"]), UTC)
            close_at, basis = _close_time(open_at, timeframe, spec.trade_sessions)
            note = f"bar_open={open_at.isoformat()}; close_time={close_at.isoformat()}; close_basis={basis}"
            bars.append(
                Mt5PriceBar(
                    instrument_id=symbol,
                    trade_date=open_at.date(),
                    open=_as_float(row["open"]) if row.get("open") is not None else None,
                    high=_as_float(row["high"]) if row.get("high") is not None else None,
                    low=_as_float(row["low"]) if row.get("low") is not None else None,
                    close=_as_float(row["close"]),
                    volume=_volume(row),
                    ccy=spec.currency,
                    source_id=self.source_id,
                    tier=self.tier,
                    ingested_at=ingested,
                    quality_flag="ok",
                    session_note=note,
                )
            )
        return spec, bars


def mt5_module_loaded() -> bool:
    """True when MetaTrader5 is already imported. Used to show this module does not import it."""
    return "MetaTrader5" in sys.modules
