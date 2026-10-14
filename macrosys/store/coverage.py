"""Decision-blocking coverage report (spec §9.4, ticket D0.4).

A required series is missing when no observation of it was published by ``as_of``. It is stale
when the latest visible ``ref_period_end`` is strictly older than
``as_of - max_lag_days - typical_lag_days``. ``typical_lag_days`` comes from the ``series``
table; a null lag is treated as zero grace days, which blocks sooner rather than later.
``blocking`` is true when any required series is missing or stale.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Literal

import duckdb
import pandas as pd
import pyarrow as pa
from pydantic import BaseModel, ConfigDict

from macrosys.store.schema import ensure_utc


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


class SeriesCoverage(BaseModel):
    """One required series in a coverage report (spec §9.4)."""

    model_config = ConfigDict(frozen=True)

    series_id: str
    max_lag_days: int
    typical_lag_days: int | None
    latest_ref_period_end: date | None
    latest_published_at: datetime | None
    stale_before: date
    status: Literal["ok", "stale", "missing"]


class CoverageReport(BaseModel):
    """Per-series freshness at ``as_of``, plus the decision-blocking flag (spec §9.4)."""

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    rows: list[SeriesCoverage]

    @property
    def blocking(self) -> bool:
        """True when any required series is missing or stale (spec §9.4)."""
        return any(row.status != "ok" for row in self.rows)


def coverage_report(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    required: list[tuple[str, int]],
) -> CoverageReport:
    """Report whether each ``(series_id, max_lag_days)`` is fresh enough to decide (spec §9.4).

    Visibility uses ``published_at <= as_of``, the same rule as ``snapshot``. A series is stale
    when its latest visible ``ref_period_end`` is older than ``as_of`` minus ``max_lag_days``
    minus the series' ``typical_lag_days`` (null lag counts as 0). Missing series block.
    ``as_of`` must be timezone-aware. The cutoff date is the calendar date of that subtraction
    in ``as_of``'s own zone; a period ending on the cutoff date is still fresh.
    """
    as_of = _require_aware(as_of, "as_of")
    for series_id, max_lag_days in required:
        if max_lag_days < 0:
            raise ValueError(f"max_lag_days for {series_id} must be >= 0, got {max_lag_days}")
    if not required:
        return CoverageReport(as_of=as_of, rows=[])

    ensure_utc(conn)
    incoming = pa.table({"series_id": pa.array([series_id for series_id, _ in required], type=pa.string())})
    conn.register("coverage_required", incoming)
    try:
        frame = conn.execute(
            """
            WITH visible AS (
                SELECT
                    series_id,
                    ref_period_end,
                    published_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY series_id
                        ORDER BY ref_period_end DESC, published_at DESC, vintage_id DESC
                    ) AS rn
                FROM observation
                WHERE published_at <= ?
                  AND series_id IN (SELECT series_id FROM coverage_required)
            )
            SELECT
                r.series_id,
                s.typical_lag_days,
                v.ref_period_end AS latest_ref_period_end,
                v.published_at AS latest_published_at
            FROM (SELECT DISTINCT series_id FROM coverage_required) r
            LEFT JOIN series s ON s.series_id = r.series_id
            LEFT JOIN visible v ON v.series_id = r.series_id AND v.rn = 1
            """,
            [as_of],
        ).df()
    finally:
        conn.unregister("coverage_required")

    facts = {rec["series_id"]: rec for rec in frame.to_dict(orient="records")}
    rows: list[SeriesCoverage] = []
    for series_id, max_lag_days in required:
        fact = facts.get(series_id, {})
        typical = _optional_int(fact.get("typical_lag_days"))
        if typical is not None and typical < 0:
            raise ValueError(f"typical_lag_days for {series_id} must be >= 0, got {typical}")
        grace = max_lag_days + (0 if typical is None else typical)
        stale_before = (as_of - timedelta(days=grace)).date()
        latest_end = _optional_date(fact.get("latest_ref_period_end"))
        latest_published = _optional_datetime(fact.get("latest_published_at"))
        if latest_end is None:
            status: Literal["ok", "stale", "missing"] = "missing"
        elif latest_end < stale_before:
            status = "stale"
        else:
            status = "ok"
        rows.append(
            SeriesCoverage(
                series_id=series_id,
                max_lag_days=max_lag_days,
                typical_lag_days=typical,
                latest_ref_period_end=latest_end,
                latest_published_at=latest_published,
                stale_before=stale_before,
                status=status,
            )
        )
    return CoverageReport(as_of=as_of, rows=rows)


def _optional_int(value: object) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)  # type: ignore[arg-type]


def _optional_date(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()  # type: ignore[arg-type]


def _optional_datetime(value: object) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    stamp = pd.Timestamp(value)  # type: ignore[arg-type]
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.to_pydatetime()
