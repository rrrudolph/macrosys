"""Small builders for store tests. Not collected by pytest."""

from __future__ import annotations

from datetime import date, datetime

from macrosys.core.contracts import Observation, SeriesMeta


def series(
    series_id: str,
    *,
    typical_lag_days: int | None = 30,
    unit: str = "index",
    frequency: str = "Q",
) -> SeriesMeta:
    source_id, native_id = series_id.split(":", 1)
    return SeriesMeta(
        series_id=series_id,
        source_id=source_id,
        native_id=native_id,
        title=series_id,
        unit=unit,
        frequency=frequency,  # type: ignore[arg-type]
        typical_lag_days=typical_lag_days,
        tier="record",
    )


def obs(
    series_id: str,
    start: date,
    end: date,
    value: float | None,
    published: datetime,
    vintage: str,
    *,
    unit: str = "index",
    quality_flag: str = "ok",
    ingested: datetime | None = None,
) -> Observation:
    return Observation(
        series_id=series_id,
        ref_period_start=start,
        ref_period_end=end,
        value=value,
        unit=unit,
        published_at=published,
        ingested_at=ingested or published,
        vintage_id=vintage,
        source_url=f"https://example.test/{series_id}",
        source_hash="a" * 64,
        quality_flag=quality_flag,  # type: ignore[arg-type]
    )
