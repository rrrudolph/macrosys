"""Holdings snapshots with publication-time filtering (spec §9.1, §9.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pyarrow as pa

from macrosys.core.contracts import AssetClass, HoldingRow
from macrosys.store.schema import ensure_utc

_INSERT = """
INSERT INTO holding_snapshot (
    instrument_id, as_of, published_at, source, holding_key, weight, country, sector,
    ccy, asset_class, name, market_value, ccy_mv
)
SELECT
    instrument_id, as_of, published_at, source, holding_key, weight, country, sector,
    ccy, asset_class, name, market_value, ccy_mv
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY instrument_id, as_of, holding_key, source
               ORDER BY ord
           ) AS rn
    FROM holding_incoming
) ranked
WHERE rn = 1
ON CONFLICT (instrument_id, as_of, holding_key, source) DO NOTHING
"""


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


class HoldingStore:
    """Append holding lines and read the latest snapshot published by ``as_of`` (spec §9.1, §9.2)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def append(self, rows: list[HoldingRow]) -> int:
        """Insert holding lines. Duplicates of ``(instrument_id, as_of, holding_key, source)`` are ignored.

        The first row in a batch wins. ``published_at`` must be timezone-aware. Returns rows inserted.
        """
        if not rows:
            return 0
        for row in rows:
            _require_aware(row.published_at, "published_at")
        table = pa.table(
            {
                "instrument_id": _strings([row.instrument_id for row in rows]),
                "as_of": pa.array([row.as_of for row in rows], type=pa.date32()),
                "published_at": pa.array([row.published_at for row in rows], type=pa.timestamp("us", tz="UTC")),
                "source": _strings([row.source for row in rows]),
                "holding_key": _strings([row.holding_key for row in rows]),
                "weight": pa.array([row.weight for row in rows], type=pa.float64()),
                "country": _strings([row.country for row in rows]),
                "sector": _strings([row.sector for row in rows]),
                "ccy": _strings([row.ccy for row in rows]),
                "asset_class": _strings([row.asset_class for row in rows]),
                "name": _strings([row.name for row in rows]),
                "market_value": pa.array([row.market_value for row in rows], type=pa.float64()),
                "ccy_mv": _strings([row.ccy_mv for row in rows]),
                "ord": pa.array(list(range(len(rows))), type=pa.int64()),
            }
        )
        ensure_utc(self._conn)
        self._conn.register("holding_incoming", table)
        try:
            inserted = self._conn.execute(_INSERT).fetchone()
        finally:
            self._conn.unregister("holding_incoming")
        if inserted is None:
            raise RuntimeError("holding insert did not report a row count")
        return int(inserted[0])

    def latest_snapshot(self, instrument_id: str, as_of: datetime) -> list[HoldingRow]:
        """Lines of the newest holdings date whose ``published_at`` is at or before ``as_of`` (spec §9.2).

        A filing dated later is invisible even when its holdings ``as_of`` is earlier. Among visible
        filings, the greatest holdings ``as_of`` wins; a tie takes the later ``published_at`` and
        that source. Returns [] when nothing was published in time.
        """
        as_of = _require_aware(as_of, "as_of")
        ensure_utc(self._conn)
        rows = self._conn.execute(
            """
            WITH visible AS (
                SELECT *
                FROM holding_snapshot
                WHERE instrument_id = ? AND published_at <= ?
            ),
            chosen AS (
                SELECT as_of, source, published_at
                FROM visible
                ORDER BY as_of DESC, published_at DESC, source
                LIMIT 1
            )
            SELECT
                v.instrument_id,
                v.as_of,
                strftime(timezone('UTC', v.published_at), '%Y-%m-%dT%H:%M:%S.%fZ'),
                v.source,
                v.holding_key,
                v.name,
                v.weight,
                v.country,
                v.sector,
                v.ccy,
                v.asset_class,
                v.market_value,
                v.ccy_mv
            FROM visible v
            JOIN chosen c
              ON v.as_of = c.as_of AND v.source = c.source AND v.published_at = c.published_at
            ORDER BY v.holding_key
            """,
            [instrument_id, as_of],
        ).fetchall()
        return [_row(item) for item in rows]


def _parse_utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _row(item: tuple) -> HoldingRow:
    (
        instrument_id,
        as_of,
        published_at,
        source,
        holding_key,
        name,
        weight,
        country,
        sector,
        ccy,
        asset_class,
        market_value,
        ccy_mv,
    ) = item
    return HoldingRow(
        instrument_id=instrument_id,
        as_of=as_of,
        published_at=_parse_utc(published_at),
        source=source,
        holding_key=holding_key,
        name=name or "",
        weight=float(weight),
        country=country,
        sector=sector,
        ccy=ccy,
        asset_class=_asset_class(asset_class),
        market_value=None if market_value is None else float(market_value),
        ccy_mv=ccy_mv,
    )


def _asset_class(value: str) -> AssetClass:
    return value  # type: ignore[return-value]
