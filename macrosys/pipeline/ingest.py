"""Fetch, parse, and append, or replay bytes already in the raw store (spec §9.1, §9.4).

``ingest_series`` is fetch → raw → parse → append. ``ParserChangedError`` writes a quarantine row
and the run continues with the next series. ``replay`` re-parses stored payloads for one source
after a parser fix; it does not download. Price producers are duck-typed: ``bars(instrument_id,
start, end)`` is preferred, otherwise ``rates`` (MetaTrader 5) or ``fetch`` plus ``parse_eod``
(Tiingo) or ``candles`` (TradingView).
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from datetime import time as clock_time
from typing import Any

from pydantic import BaseModel, ConfigDict

from macrosys.core.contracts import CorporateAction, DataSource, ParserChangedError, PriceBar, RawStore
from macrosys.store.actions import CorporateActionStore
from macrosys.store.observations import ObservationStore
from macrosys.store.prices import PriceStore
from macrosys.store.quarantine import clear_quarantine, quarantine_payload

Now = datetime | Callable[[], datetime]


class ItemResult(BaseModel):
    """One series or instrument inside an ingest run (spec §9.4)."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    inserted: int = 0
    error: str | None = None
    quarantined: bool = False
    elapsed_seconds: float


class IngestReport(BaseModel):
    """Per-item counts, errors, and elapsed time for one source (spec §9.1, §9.4)."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    results: list[ItemResult]
    started_at: datetime
    ended_at: datetime

    @property
    def failed(self) -> bool:
        """True when any item recorded an error, including a quarantine."""
        return any(item.error for item in self.results)


class ReplayReport(BaseModel):
    """How many stored payloads were re-parsed and how many rows that inserted (spec §9.1)."""

    model_config = ConfigDict(frozen=True)

    source_id: str
    payloads: int
    inserted: int
    quarantined: int
    errors: list[str]


def ingest_series(
    source: DataSource,
    series_ids: list[str],
    raw_store: RawStore,
    obs_store: ObservationStore,
    *,
    now: Now,
) -> IngestReport:
    """Fetch each series into the raw store, parse it, and append observations (spec §9.1, §9.4).

    A ``ParserChangedError`` quarantines that payload (sha256 and reason) and does not append
    its rows. The rest of ``series_ids`` still run. Any other exception is recorded on that
    series and also does not abort the run. ``now`` is a timezone-aware timestamp or a clock.
    """
    started = _resolve(now, "now")
    results: list[ItemResult] = []
    for series_id in series_ids:
        t0 = time.perf_counter()
        try:
            payload = source.fetch(series_id, None, raw_store)
            content = raw_store.get(payload)
            try:
                rows = source.parse(payload, content)
            except ParserChangedError as exc:
                quarantine_payload(
                    obs_store.conn,
                    sha256=payload.sha256,
                    source_id=source.source_id,
                    reason=str(exc),
                    series_id=series_id,
                    at=started,
                )
                results.append(
                    ItemResult(
                        item_id=series_id,
                        error=str(exc),
                        quarantined=True,
                        elapsed_seconds=time.perf_counter() - t0,
                    )
                )
                continue
            inserted = obs_store.append(rows)
            results.append(
                ItemResult(item_id=series_id, inserted=inserted, elapsed_seconds=time.perf_counter() - t0)
            )
        except Exception as exc:
            results.append(
                ItemResult(
                    item_id=series_id,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=time.perf_counter() - t0,
                )
            )
    return IngestReport(
        source_id=source.source_id,
        results=results,
        started_at=started,
        ended_at=started,
    )


def ingest_prices(
    source: Any,
    instrument_ids: list[str],
    raw_store: RawStore,
    price_store: PriceStore,
    action_store: CorporateActionStore | None = None,
    *,
    start: date,
    end: date,
    now: Now,
) -> IngestReport:
    """Load price bars (and corporate actions, when the producer emits them) for each instrument.

    Implements spec §9.1 and §9.6. A producer with ``bars(instrument_id, start, end)`` is called
    directly. Otherwise MetaTrader 5 ``rates``, Tiingo ``fetch`` + ``parse_eod``, or TradingView
    ``candles`` is used. One instrument's failure is recorded and the loop continues.
    """
    started = _resolve(now, "now")
    source_id = str(getattr(source, "source_id", ""))
    results: list[ItemResult] = []
    for instrument_id in instrument_ids:
        t0 = time.perf_counter()
        try:
            bars, actions = _produce_prices(source, instrument_id, start, end, raw_store)
            inserted = price_store.append_bars(bars)
            if action_store is not None and actions:
                inserted += action_store.append([_as_action(row) for row in actions])
            results.append(
                ItemResult(
                    item_id=instrument_id,
                    inserted=inserted,
                    elapsed_seconds=time.perf_counter() - t0,
                )
            )
        except Exception as exc:
            results.append(
                ItemResult(
                    item_id=instrument_id,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=time.perf_counter() - t0,
                )
            )
    return IngestReport(source_id=source_id, results=results, started_at=started, ended_at=started)


def replay(raw_store: Any, source: DataSource, obs_store: ObservationStore) -> ReplayReport:
    """Re-parse stored raw payloads for ``source`` and append observations (spec §9.1).

    ``raw_store`` must implement ``list(source_id)`` (``FileRawStore`` does). A payload that still
    raises ``ParserChangedError`` is quarantined and skipped. A payload that parses is appended;
    a second replay of the same bytes inserts nothing because observation append is idempotent.
    A successful parse clears any quarantine row for that sha256.
    """
    if not hasattr(raw_store, "list"):
        raise TypeError("raw_store must implement list(source_id) so replay can find stored payloads")
    payloads = raw_store.list(source.source_id)
    inserted = 0
    quarantined = 0
    errors: list[str] = []
    for payload in payloads:
        content = raw_store.get(payload)
        try:
            rows = source.parse(payload, content)
        except ParserChangedError as exc:
            quarantine_payload(
                obs_store.conn,
                sha256=payload.sha256,
                source_id=source.source_id,
                reason=str(exc),
                series_id=None,
                at=payload.fetched_at,
            )
            quarantined += 1
            errors.append(f"{payload.sha256}: {exc}")
            continue
        except Exception as exc:
            errors.append(f"{payload.sha256}: {type(exc).__name__}: {exc}")
            continue
        inserted += obs_store.append(rows)
        clear_quarantine(obs_store.conn, payload.sha256)
    return ReplayReport(
        source_id=source.source_id,
        payloads=len(payloads),
        inserted=inserted,
        quarantined=quarantined,
        errors=errors,
    )


def _produce_prices(
    source: Any,
    instrument_id: str,
    start: date,
    end: date,
    raw_store: RawStore,
) -> tuple[list[PriceBar], list[Any]]:
    bars = getattr(source, "bars", None)
    if callable(bars):
        return _split(bars(instrument_id, start, end))
    rates = getattr(source, "rates", None)
    if callable(rates):
        timeframe = getattr(source, "daily_timeframe", 1440)
        start_at = datetime.combine(start, clock_time.min, tzinfo=UTC)
        end_at = datetime.combine(end, clock_time.max, tzinfo=UTC)
        return _split(rates(instrument_id, timeframe, start_at, end_at))
    parse_eod = getattr(importlib.import_module(type(source).__module__), "parse_eod", None)
    fetch = getattr(source, "fetch", None)
    if callable(fetch) and callable(parse_eod):
        payload = fetch(instrument_id, start, raw_store)
        return _split(parse_eod(payload, raw_store.get(payload)))
    candles = getattr(source, "candles", None)
    if callable(candles):
        count = max((end - start).days + 1, 1)
        return list(candles(instrument_id, "1D", count, ccy="USD")), []
    raise TypeError(f"{type(source).__name__} does not produce price bars")


def _split(produced: Any) -> tuple[list[Any], list[Any]]:
    if isinstance(produced, tuple) and len(produced) == 2 and isinstance(produced[0], list):
        return list(produced[0]), list(produced[1])
    if isinstance(produced, tuple) and len(produced) == 2 and isinstance(produced[1], list):
        # MetaTrader 5 rates() returns (SymbolSpec, bars).
        return list(produced[1]), []
    if isinstance(produced, list):
        return produced, []
    raise TypeError("price producer must return bars or (bars, actions)")


def _as_action(row: Any) -> CorporateAction:
    if isinstance(row, CorporateAction):
        return row
    return CorporateAction(
        instrument_id=row.instrument_id,
        ex_date=row.ex_date,
        type=row.type,
        ratio=getattr(row, "ratio", None),
        amount=getattr(row, "amount", None),
        ccy=getattr(row, "ccy", None),
    )


def _resolve(now: Now, name: str) -> datetime:
    stamp = now() if callable(now) else now
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.tzinfo.utcoffset(stamp) is None:
        raise ValueError(f"{name} must be a timezone-aware datetime or a clock that returns one")
    return stamp
