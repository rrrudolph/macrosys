"""Append-only observations and point-in-time snapshots (spec §9.1, §9.2).

A revision is a new row, never an update. ``snapshot(as_of)`` returns only rows with
``published_at <= as_of``. ``'latest'`` keeps, per reference period, the vintage with the
greatest ``published_at``; ``'first'`` keeps the earliest. Ingestion time does not gate
visibility.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Literal

import duckdb
import pandas as pd
import pyarrow as pa

from macrosys.core.contracts import Observation, SeriesMeta
from macrosys.core.quantity import Quantity
from macrosys.core.units import assert_unit
from macrosys.store.schema import ensure_utc

_COLUMNS = [
    "ref_period_start",
    "ref_period_end",
    "value",
    "unit",
    "published_at",
    "vintage_id",
    "quality_flag",
]

_OBS_INSERT = """
INSERT INTO observation (
    series_id, ref_period_start, ref_period_end, value, unit,
    published_at, ingested_at, vintage_id, source_url, source_hash, quality_flag
)
SELECT
    series_id, ref_period_start, ref_period_end, value, unit,
    published_at, ingested_at, vintage_id, source_url, source_hash, quality_flag
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY series_id, ref_period_start, vintage_id
               ORDER BY ord
           ) AS rn
    FROM obs_incoming
) ranked
WHERE rn = 1
ON CONFLICT (series_id, ref_period_start, vintage_id) DO NOTHING
"""

_SERIES_UPSERT = """
INSERT INTO series (
    series_id, source, native_id, title, unit, frequency, seasonal_adj, geography,
    concept, release_id, typical_lag_days, revision_policy, license, tier
)
SELECT
    series_id, source, native_id, title, unit, frequency, seasonal_adj, geography,
    concept, release_id, typical_lag_days, revision_policy, license, tier
FROM (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY ord DESC) AS rn
    FROM series_incoming
) ranked
WHERE rn = 1
ON CONFLICT (series_id) DO UPDATE SET
    source = EXCLUDED.source,
    native_id = EXCLUDED.native_id,
    title = EXCLUDED.title,
    unit = EXCLUDED.unit,
    frequency = EXCLUDED.frequency,
    seasonal_adj = EXCLUDED.seasonal_adj,
    geography = EXCLUDED.geography,
    concept = EXCLUDED.concept,
    release_id = EXCLUDED.release_id,
    typical_lag_days = EXCLUDED.typical_lag_days,
    revision_policy = EXCLUDED.revision_policy,
    license = EXCLUDED.license,
    tier = EXCLUDED.tier
"""


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ref_period_start": pd.Series(dtype="object"),
            "ref_period_end": pd.Series(dtype="object"),
            "value": pd.Series(dtype="float64"),
            "unit": pd.Series(dtype="object"),
            "published_at": pd.Series(dtype="datetime64[us, UTC]"),
            "vintage_id": pd.Series(dtype="object"),
            "quality_flag": pd.Series(dtype="object"),
        }
    )


class PointInTime:
    """Observations that had been published by ``as_of`` (spec §9.2).

    The view reads the live table and applies ``published_at <= as_of`` on every call.
    A row ingested later is visible when its publication time is already at or before ``as_of``.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, as_of: datetime) -> None:
        self._conn = conn
        self.as_of = _require_aware(as_of, "as_of")

    def get(
        self,
        series_id: str,
        start: date | None = None,
        end: date | None = None,
        vintage: Literal["latest", "first"] = "latest",
    ) -> pd.DataFrame:
        """Return one row per reference period visible at ``as_of`` (spec §9.2).

        ``start`` and ``end`` bound ``ref_period_start`` inclusive. ``vintage='latest'`` is the
        most recently published vintage of that period with ``published_at <= as_of``.
        ``vintage='first'`` is the earliest vintage of that period that had been published by
        ``as_of``. Columns are ``ref_period_start``, ``ref_period_end``, ``value``, ``unit``,
        ``published_at``, ``vintage_id``, ``quality_flag``, ordered by reference period.
        """
        if vintage not in ("latest", "first"):
            raise ValueError(f"vintage must be 'latest' or 'first', got {vintage!r}")
        # Only the two literals above reach this f-string.
        direction = "DESC" if vintage == "latest" else "ASC"
        ensure_utc(self._conn)
        frame = self._conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    ref_period_start, ref_period_end, value, unit,
                    published_at, vintage_id, quality_flag,
                    ROW_NUMBER() OVER (
                        PARTITION BY ref_period_start
                        ORDER BY published_at {direction}, ingested_at {direction}, vintage_id {direction}
                    ) AS rn
                FROM observation
                WHERE series_id = ?
                  AND published_at <= ?
                  AND (? IS NULL OR ref_period_start >= ?)
                  AND (? IS NULL OR ref_period_start <= ?)
            )
            SELECT ref_period_start, ref_period_end, value, unit, published_at, vintage_id, quality_flag
            FROM ranked
            WHERE rn = 1
            ORDER BY ref_period_start
            """,
            [series_id, self.as_of, start, start, end, end],
        ).df()
        return _normalize(frame)

    def latest_value(self, series_id: str) -> Quantity | None:
        """Value of the newest visible reference period, as a ``Quantity`` (spec §9.2, §4.7).

        Provenance is ``series_id``. Returns ``None`` when the series has no visible row or the
        newest period's value is null (missing is stored as absent, spec §9.4). Uses the
        ``'latest'`` vintage of that period.
        """
        frame = self.get(series_id, vintage="latest")
        if frame.empty:
            return None
        ordered = frame.sort_values(["ref_period_end", "ref_period_start"], kind="mergesort")
        row = ordered.iloc[-1]
        value = row["value"]
        if value is None or (isinstance(value, float) and math.isnan(value)) or pd.isna(value):
            return None
        return Quantity(value=float(value), unit=str(row["unit"]), provenance=series_id)


class ObservationStore:
    """Append observations and read them through ``snapshot`` (spec §9.1, §9.2)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        """DuckDB connection the observation tables live on."""
        return self._conn

    def append(self, rows: list[Observation]) -> int:
        """Insert observations, skipping natural-key duplicates (spec §9.1, §9.4).

        The natural key is ``(series_id, ref_period_start, vintage_id)``. A replay inserts
        nothing for keys already stored. Within one batch the first row for a key wins.
        Returns the number of rows actually inserted. Units must be in the controlled vocabulary.
        """
        if not rows:
            return 0
        for row in rows:
            assert_unit(row.unit)
            _require_aware(row.published_at, "published_at")
            _require_aware(row.ingested_at, "ingested_at")
            if row.ref_period_end < row.ref_period_start:
                raise ValueError(
                    f"{row.series_id} {row.vintage_id}: ref_period_end before ref_period_start"
                )
        table = pa.table(
            {
                "series_id": _strings([row.series_id for row in rows]),
                "ref_period_start": pa.array([row.ref_period_start for row in rows], type=pa.date32()),
                "ref_period_end": pa.array([row.ref_period_end for row in rows], type=pa.date32()),
                "value": pa.array([row.value for row in rows], type=pa.float64()),
                "unit": _strings([row.unit for row in rows]),
                "published_at": pa.array(
                    [row.published_at for row in rows], type=pa.timestamp("us", tz="UTC")
                ),
                "ingested_at": pa.array(
                    [row.ingested_at for row in rows], type=pa.timestamp("us", tz="UTC")
                ),
                "vintage_id": _strings([row.vintage_id for row in rows]),
                "source_url": _strings([row.source_url for row in rows]),
                "source_hash": _strings([row.source_hash for row in rows]),
                "quality_flag": _strings([row.quality_flag for row in rows]),
                "ord": pa.array(list(range(len(rows))), type=pa.int64()),
            }
        )
        ensure_utc(self._conn)
        self._conn.register("obs_incoming", table)
        try:
            inserted = self._conn.execute(_OBS_INSERT).fetchone()
        finally:
            self._conn.unregister("obs_incoming")
        if inserted is None:
            raise RuntimeError("observation insert did not report a row count")
        return int(inserted[0])

    def upsert_series(self, meta: list[SeriesMeta]) -> None:
        """Insert or replace series metadata (spec §4.7 ``series``, §9.6 ``tier``).

        ``SeriesMeta.source_id`` is stored in the spec's ``source`` column. Within one batch
        the last row for a ``series_id`` wins. Units must be in the controlled vocabulary.
        """
        if not meta:
            return
        for row in meta:
            assert_unit(row.unit)
        table = pa.table(
            {
                "series_id": _strings([row.series_id for row in meta]),
                "source": _strings([row.source_id for row in meta]),
                "native_id": _strings([row.native_id for row in meta]),
                "title": _strings([row.title for row in meta]),
                "unit": _strings([row.unit for row in meta]),
                "frequency": _strings([row.frequency for row in meta]),
                "seasonal_adj": _strings([row.seasonal_adj for row in meta]),
                "geography": _strings([row.geography for row in meta]),
                "concept": _strings([row.concept for row in meta]),
                "release_id": _strings([row.release_id for row in meta]),
                "typical_lag_days": pa.array([row.typical_lag_days for row in meta], type=pa.int32()),
                "revision_policy": _strings([row.revision_policy for row in meta]),
                "license": _strings([row.license for row in meta]),
                "tier": _strings([row.tier for row in meta]),
                "ord": pa.array(list(range(len(meta))), type=pa.int64()),
            }
        )
        ensure_utc(self._conn)
        self._conn.register("series_incoming", table)
        try:
            self._conn.execute(_SERIES_UPSERT)
        finally:
            self._conn.unregister("series_incoming")

    def snapshot(self, as_of: datetime) -> PointInTime:
        """Open a point-in-time view of observations published at or before ``as_of`` (spec §9.2)."""
        return PointInTime(self._conn, as_of)


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return _empty_frame()
    out = frame.loc[:, _COLUMNS].copy()
    for column in ("ref_period_start", "ref_period_end"):
        out[column] = pd.to_datetime(out[column]).dt.date
    out["published_at"] = pd.to_datetime(out["published_at"], utc=True)
    return out.reset_index(drop=True)
