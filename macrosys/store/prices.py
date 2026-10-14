"""Price bars, tier rules, and record-vs-cross-check flags (spec §9.1, §9.6).

A research-tier bar is stored only with ``quality_flag='unofficial'``. ``read_closes`` returns a
wide close panel (one column per instrument, indexed by trade date) for the requested tier.
``aligned_returns`` takes that panel split into per-instrument frames whose column is ``close``.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import duckdb
import pandas as pd
import pyarrow as pa

from macrosys.core.contracts import PriceBar
from macrosys.instruments.actions import PRICE_DISCREPANCY_FLAG
from macrosys.store.schema import ensure_utc

_INSERT = """
INSERT INTO price_bar (
    instrument_id, trade_date, open, high, low, close, volume, nav, ccy,
    source, ingested_at, tier, quality_flag, session_note
)
SELECT
    instrument_id, trade_date, open, high, low, close, volume, nav, ccy,
    source, ingested_at, tier, quality_flag, session_note
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY instrument_id, trade_date, source
               ORDER BY ord
           ) AS rn
    FROM price_incoming
) ranked
WHERE rn = 1
ON CONFLICT (instrument_id, trade_date, source) DO NOTHING
"""


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


class PriceStore:
    """Append price bars and read a point-in-time close panel (spec §9.1, §9.6)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """DuckDB connection the price tables live on."""
        return self._conn

    def append_bars(self, rows: list[PriceBar]) -> int:
        """Insert bars, skipping duplicates of ``(instrument_id, trade_date, source)`` (spec §9.6).

        ``source`` is ``PriceBar.source_id``. The first row in a batch wins for a duplicate key.
        A ``tier='research'`` bar is stored only when ``quality_flag='unofficial'``; any other
        flag raises ``ValueError`` and nothing in the batch is written. Returns rows inserted.
        """
        if not rows:
            return 0
        for row in rows:
            _require_aware(row.ingested_at, "ingested_at")
            if row.tier == "research" and row.quality_flag != "unofficial":
                raise ValueError(
                    f"{row.instrument_id} {row.trade_date}: research-tier bars require "
                    f"quality_flag='unofficial', got {row.quality_flag!r}"
                )
        table = pa.table(
            {
                "instrument_id": _strings([row.instrument_id for row in rows]),
                "trade_date": pa.array([row.trade_date for row in rows], type=pa.date32()),
                "open": pa.array([row.open for row in rows], type=pa.float64()),
                "high": pa.array([row.high for row in rows], type=pa.float64()),
                "low": pa.array([row.low for row in rows], type=pa.float64()),
                "close": pa.array([row.close for row in rows], type=pa.float64()),
                "volume": pa.array([row.volume for row in rows], type=pa.float64()),
                "nav": pa.array([row.nav for row in rows], type=pa.float64()),
                "ccy": _strings([row.ccy for row in rows]),
                "source": _strings([row.source_id for row in rows]),
                "ingested_at": pa.array([row.ingested_at for row in rows], type=pa.timestamp("us", tz="UTC")),
                "tier": _strings([row.tier for row in rows]),
                "quality_flag": _strings([row.quality_flag for row in rows]),
                "session_note": _strings([row.session_note for row in rows]),
                "ord": pa.array(list(range(len(rows))), type=pa.int64()),
            }
        )
        ensure_utc(self._conn)
        self._conn.register("price_incoming", table)
        try:
            inserted = self._conn.execute(_INSERT).fetchone()
        finally:
            self._conn.unregister("price_incoming")
        if inserted is None:
            raise RuntimeError("price insert did not report a row count")
        return int(inserted[0])

    def read_closes(
        self,
        instrument_ids: list[str],
        start: date,
        end: date,
        tier: str = "record",
    ) -> pd.DataFrame:
        """Wide closes for ``tier`` between ``start`` and ``end`` inclusive (spec §6.1, §9.6).

        The index is ``trade_date`` and each column is an instrument id. A missing print is NaN,
        never zero. When several sources share the tier, the latest ``ingested_at`` wins. To feed
        ``macrosys.instruments.align.aligned_returns``, split the panel into
        ``{instrument_id: frame.rename(columns={instrument_id: "close"}).dropna()}``.
        """
        if tier not in ("record", "cross_check", "research"):
            raise ValueError(f"unknown tier {tier!r}")
        if not instrument_ids:
            return pd.DataFrame(index=pd.DatetimeIndex([], name="trade_date"))
        ensure_utc(self._conn)
        ids = pa.table({"instrument_id": pa.array(instrument_ids, type=pa.string())})
        self._conn.register("price_ids", ids)
        try:
            frame = self._conn.execute(
                """
                SELECT trade_date, instrument_id, close
                FROM (
                    SELECT
                        trade_date, instrument_id, close,
                        ROW_NUMBER() OVER (
                            PARTITION BY instrument_id, trade_date
                            ORDER BY ingested_at DESC, source
                        ) AS rn
                    FROM price_bar
                    WHERE tier = ?
                      AND trade_date >= ?
                      AND trade_date <= ?
                      AND instrument_id IN (SELECT instrument_id FROM price_ids)
                ) ranked
                WHERE rn = 1
                """,
                [tier, start, end],
            ).df()
        finally:
            self._conn.unregister("price_ids")
        if frame.empty:
            wide = pd.DataFrame(columns=list(instrument_ids), index=pd.DatetimeIndex([], name="trade_date"))
            wide.index.name = "trade_date"
            return wide
        wide = frame.pivot(index="trade_date", columns="instrument_id", values="close")
        wide = wide.reindex(columns=list(instrument_ids))
        wide.index = pd.to_datetime(wide.index)
        wide.index.name = "trade_date"
        wide.columns.name = None
        return wide.sort_index()

    def cross_check(self, instrument_id: str, start: date, end: date, tol: float) -> list[date]:
        """Flag trade dates whose record and cross-check closes differ by more than ``tol`` (spec §9.6).

        ``tol`` is a relative tolerance: a date is flagged when
        ``abs(record - cross_check) / abs(record) > tol`` (absolute difference when the record
        close is 0). Each flagged date is written to ``data_flag`` with kind ``price_discrepancy``.
        Dates missing either tier are not flags. Returns the flagged dates, ascending.
        """
        if tol < 0:
            raise ValueError(f"tol must be >= 0, got {tol}")
        ensure_utc(self._conn)
        frame = self._conn.execute(
            """
            SELECT trade_date, tier, close
            FROM (
                SELECT
                    trade_date, tier, close,
                    ROW_NUMBER() OVER (
                        PARTITION BY trade_date, tier
                        ORDER BY ingested_at DESC, source
                    ) AS rn
                FROM price_bar
                WHERE instrument_id = ?
                  AND trade_date >= ?
                  AND trade_date <= ?
                  AND tier IN ('record', 'cross_check')
            ) ranked
            WHERE rn = 1
            """,
            [instrument_id, start, end],
        ).df()
        if frame.empty:
            return []
        flagged: list[date] = []
        by_date: dict[date, dict[str, float]] = {}
        for record in frame.to_dict(orient="records"):
            trade_date = _as_date(record["trade_date"])
            by_date.setdefault(trade_date, {})[str(record["tier"])] = float(record["close"])
        for trade_date in sorted(by_date):
            closes = by_date[trade_date]
            if "record" not in closes or "cross_check" not in closes:
                continue
            record_close = closes["record"]
            cross_close = closes["cross_check"]
            gap = abs(record_close - cross_close)
            scale = abs(record_close) if record_close != 0 and not math.isnan(record_close) else 1.0
            if gap / scale <= tol:
                continue
            detail = f"record={record_close};cross_check={cross_close};tol={tol}"
            self._conn.execute(
                """
                INSERT INTO data_flag (as_of, kind, instrument_id, detail)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (as_of, kind, instrument_id) DO UPDATE SET detail = EXCLUDED.detail
                """,
                [trade_date, PRICE_DISCREPANCY_FLAG, instrument_id, detail],
            )
            flagged.append(trade_date)
        return flagged


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()  # type: ignore[arg-type]
