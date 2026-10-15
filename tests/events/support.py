"""Synthetic vintage and market fixtures for the event study. No network, no real data."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import duckdb

from macrosys.core.contracts import Observation
from macrosys.events.config import (
    EventsConfig,
    ForecasterSpec,
    MarketSpec,
    RegimeSpec,
    ReleaseSpec,
    ResponseSpec,
)
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema

RELEASE = "FRED:TESTCPI"
RATE = "FRED:TESTUR"
YIELD = "FRED:TESTY"
INDEX = "FRED:TESTPX"
INGESTED = datetime(2030, 1, 1, tzinfo=UTC)


def month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def next_month(day: date) -> date:
    return date(day.year + (day.month // 12), day.month % 12 + 1, 1)


def month_end(day: date) -> date:
    return next_month(day) - timedelta(days=1)


def release_instant(period: date, hour: int = 13, minute: int = 30) -> datetime:
    """Release on the 12th of the month after ``period`` at the given UTC clock."""
    following = next_month(period)
    return datetime.combine(date(following.year, following.month, 12), time(hour, minute), tzinfo=UTC)


def obs(series_id: str, period: date, value: float, published: datetime, unit: str = "index", end: date | None = None) -> Observation:
    return Observation(
        series_id=series_id,
        ref_period_start=period,
        ref_period_end=end if end is not None else month_end(period),
        value=value,
        unit=unit,
        published_at=published,
        ingested_at=INGESTED,
        vintage_id=published.date().isoformat(),
        source_url="https://example.invalid/test",
        source_hash="0" * 64,
        quality_flag="ok",
    )


def monthly_release_rows(levels: list[float], first: date, unit: str = "index", revise_by: float = 0.0) -> list[Observation]:
    """First prints for each month; when ``revise_by`` is set, the prior month is revised with each release."""
    rows: list[Observation] = []
    period = first
    periods: list[date] = []
    for _ in levels:
        periods.append(period)
        period = next_month(period)
    for index, (period, level) in enumerate(zip(periods, levels, strict=True)):
        released = release_instant(period)
        rows.append(obs(RELEASE if unit == "index" else RATE, period, level, released, unit))
        if revise_by and index > 0:
            rows.append(obs(RELEASE if unit == "index" else RATE, periods[index - 1], levels[index - 1] + revise_by, released, unit))
    return rows


def business_days(start: date, end: date) -> list[date]:
    days: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def daily_rows(series_id: str, values: dict[date, float], unit: str) -> list[Observation]:
    return [
        obs(series_id, day, value, datetime.combine(day, time(22, 0), tzinfo=UTC), unit, end=day)
        for day, value in sorted(values.items())
    ]


def new_store() -> tuple[duckdb.DuckDBPyConnection, ObservationStore]:
    conn = duckdb.connect(database=":memory:")
    create_schema(conn)
    return conn, ObservationStore(conn)


def make_config(*, responses: list[ResponseSpec], regimes: list[RegimeSpec] | None = None, horizons: list[int] | None = None) -> EventsConfig:
    return EventsConfig(
        close_cutoff_utc_hour=20,
        max_release_lag_days=120,
        horizons_days=horizons or [1, 5],
        min_history_events=6,
        sigma_window=60,
        sigma_method="std",
        sigma_cap=5.0,
        sign_threshold_sigma=0.5,
        min_events_for_regression=24,
        walk_forward_splits=3,
        forecaster=ForecasterSpec(method="trailing_mean", window=6),
        releases=[
            ReleaseSpec(series_id=RELEASE, label="test_cpi", transform="pct_change"),
            ReleaseSpec(series_id=RATE, label="test_unemployment", transform="diff"),
        ],
        markets=[
            MarketSpec(series_id=YIELD, label="test_yield", response="diff"),
            MarketSpec(series_id=INDEX, label="test_index", response="pct_change"),
        ],
        regimes=regimes or [],
        responses=responses,
    )
