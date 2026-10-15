"""Surprise events from ALFRED first prints (spec §9.2 point-in-time, §10.1 item 11).

For each reference period of a release series the event is the earliest stored vintage. The
headline change is that print minus the latest vintage of the prior period visible at the same
instant, so a revision that shipped inside the release is part of the headline as readers saw
it. The forecast is formed from earlier events' headline changes only, and the standardisation
uses earlier surprises only. Appending a later vintage to the store cannot change an earlier
event; the tests hold that property.

A consensus surprise would replace the statistical forecast here when a terms-clean consensus
source exists (spec §9.8, P2.4). Until then every event names its ``forecaster``.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Literal

import duckdb
import numpy as np
import pandas as pd

from macrosys.core.quantity import Quantity
from macrosys.events.config import EventsConfig, ForecasterSpec, RegimeSpec, ReleaseSpec
from macrosys.events.schema import SurpriseEvent
from macrosys.store.schema import ensure_utc

_VINTAGES_SQL = """
SELECT ref_period_start, ref_period_end, value, unit, published_at, vintage_id, quality_flag
FROM observation
WHERE series_id = ?
  AND value IS NOT NULL
  AND (? IS NULL OR published_at <= ?)
ORDER BY ref_period_start, published_at, ingested_at, vintage_id
"""


def load_vintages(conn: duckdb.DuckDBPyConnection, series_id: str, as_of: datetime | None = None) -> pd.DataFrame:
    """Every non-null vintage row of ``series_id`` published at or before ``as_of`` (all rows when ``None``)."""
    ensure_utc(conn)
    frame = conn.execute(_VINTAGES_SQL, [series_id, as_of, as_of]).df()
    if frame.empty:
        return frame
    frame["ref_period_start"] = pd.to_datetime(frame["ref_period_start"]).dt.date
    frame["ref_period_end"] = pd.to_datetime(frame["ref_period_end"]).dt.date
    frame["published_at"] = pd.to_datetime(frame["published_at"], utc=True)
    return frame.reset_index(drop=True)


def event_date_for(published_at: datetime, cutoff_utc_hour: int) -> date:
    """Trading date whose close first reflects a print released at ``published_at`` (spec §10.1 item 11).

    A clock at or after ``cutoff_utc_hour`` UTC is after the US close and counts for the next
    calendar day. A date-only publication time is midnight UTC and stays on its own date. Weekends
    are resolved later by the outcome window, which takes the first market date on or after this.
    """
    instant = published_at.astimezone(UTC)
    day = instant.date()
    if instant.hour >= cutoff_utc_hour:
        day = day + timedelta(days=1)
    return day


def headline_change(actual: Quantity, previous: Quantity, transform: Literal["pct_change", "diff"]) -> Quantity:
    """As-reported change between consecutive prints, with the unit the transform implies."""
    assert actual.value is not None and previous.value is not None
    if actual.unit != previous.unit:
        raise ValueError(f"unit changed between prints: {previous.unit} -> {actual.unit}")
    if transform == "pct_change":
        if previous.value == 0.0:
            raise ZeroDivisionError("percent change from a zero level")
        return Quantity(value=(actual.value / previous.value - 1.0) * 100.0, unit="pct", provenance=actual.provenance)
    unit = "pp" if actual.unit == "pct" else actual.unit
    return Quantity(value=actual.value - previous.value, unit=unit, provenance=actual.provenance)


def forecast_change(history: list[float], spec: ForecasterSpec) -> float | None:
    """Forecast of the next headline change from earlier headline changes only."""
    if not history:
        return None
    window = history[-spec.window :]
    if spec.method == "trailing_mean":
        return float(np.mean(window))
    if spec.method == "ar1":
        if len(window) < spec.min_fit:
            return float(np.mean(window))
        series = np.asarray(window, dtype=float)
        lagged, current = series[:-1], series[1:]
        if np.ptp(lagged) == 0.0:
            return float(np.mean(window))
        slope, intercept = np.polyfit(lagged, current, 1)
        # Keep the one-step forecast inside the stationary band so a near-unit-root fit cannot explode.
        slope = float(np.clip(slope, -0.99, 0.99))
        return float(intercept + slope * series[-1])
    raise ValueError(f"unknown forecaster {spec.method!r}")


def surprise_scale(history: list[float], method: Literal["mad", "std"]) -> float | None:
    """Scale of earlier surprises: ``1.4826 * MAD`` (falls back to the sample sd when the MAD is zero) or the sample sd.

    ``None`` when the scale is not positive and finite, in which case no standardized surprise exists.
    """
    values = np.asarray(history, dtype=float)
    if len(values) < 2:
        return None
    spread = float("nan")
    if method == "mad":
        spread = 1.4826 * float(np.median(np.abs(values - np.median(values))))
    if method == "std" or not (spread > 0.0):
        spread = float(np.std(values, ddof=1))
    if not (spread > 0.0 and math.isfinite(spread)):
        return None
    return spread


def build_surprises(
    conn: duckdb.DuckDBPyConnection,
    release: ReleaseSpec,
    config: EventsConfig,
    as_of: datetime | None = None,
) -> list[SurpriseEvent]:
    """Surprise events for one release series, in release order (spec §9.2, §10.1 item 11).

    A period is skipped when its earliest vintage is later than ``max_release_lag_days`` after the
    period end (the store's history starts after the real first print), when no prior-period value
    was visible at the release instant, or when no earlier event exists to forecast from.
    ``surprise_sigma`` is ``None`` until ``min_history_events`` earlier surprises exist; its scale
    comes from the last ``sigma_window`` surprises under ``sigma_method``.
    """
    frame = load_vintages(conn, release.series_id, as_of)
    if frame.empty:
        return []
    periods = sorted(frame["ref_period_start"].unique())
    by_period = {period: group for period, group in frame.groupby("ref_period_start", sort=True)}
    candidates: list[tuple[datetime, date, Quantity, Quantity, str]] = []
    for index in range(1, len(periods)):
        period = periods[index]
        group = by_period[period].sort_values(["published_at"], kind="mergesort")
        first = group.iloc[0]
        released: datetime = first["published_at"].to_pydatetime()
        period_end: date = first["ref_period_end"]
        if (released.date() - period_end).days > config.max_release_lag_days:
            continue
        prior = by_period[periods[index - 1]]
        visible = prior[prior["published_at"] <= first["published_at"]]
        if visible.empty:
            continue
        prev = visible.sort_values(["published_at"], kind="mergesort").iloc[-1]
        actual = Quantity(value=float(first["value"]), unit=str(first["unit"]), provenance=release.series_id)
        previous = Quantity(value=float(prev["value"]), unit=str(prev["unit"]), provenance=release.series_id)
        candidates.append((released, period, actual, previous, str(first["quality_flag"])))
    candidates.sort(key=lambda item: (item[0], item[1]))

    events: list[SurpriseEvent] = []
    changes: list[float] = []
    surprises: list[float] = []
    for released, period, actual, previous, quality in candidates:
        change = headline_change(actual, previous, release.transform)
        assert change.value is not None
        forecast = forecast_change(changes, config.forecaster)
        if forecast is None:
            changes.append(change.value)
            continue
        surprise_value = change.value - forecast
        sigma: float | None = None
        if len(surprises) >= config.min_history_events:
            spread = surprise_scale(surprises[-config.sigma_window :], config.sigma_method)
            if spread is not None:
                sigma = surprise_value / spread
        events.append(
            SurpriseEvent(
                series_id=release.series_id,
                ref_period_start=period,
                published_at=released,
                event_date=event_date_for(released, config.close_cutoff_utc_hour),
                publication_quality=quality,
                actual=actual,
                previous=previous,
                actual_change=change,
                forecast_change=Quantity(value=forecast, unit=change.unit, provenance=config.forecaster.name),
                surprise=Quantity(value=surprise_value, unit=change.unit, provenance=f"surprise:{config.forecaster.name}"),
                surprise_sigma=sigma,
                forecaster=config.forecaster.name,
                n_history=len(surprises),
            )
        )
        changes.append(change.value)
        surprises.append(surprise_value)
    return events


def regime_state(
    conn: duckdb.DuckDBPyConnection,
    regime: RegimeSpec,
    at: datetime,
) -> bool | None:
    """State of ``regime`` using prints visible strictly before ``at`` (spec §3.4 rule 5, §9.2).

    ``sahm``: with the latest visible vintage of each of the last twelve periods, the three-month
    mean minus the twelve-month minimum. Returns ``True`` for expansion (gap below the threshold),
    ``False`` for contraction, ``None`` with fewer than twelve visible periods.
    """
    before = at - timedelta(microseconds=1)
    frame = load_vintages(conn, regime.series_id, before)
    if frame.empty:
        return None
    latest = frame.sort_values(["ref_period_start", "published_at"], kind="mergesort").groupby("ref_period_start").tail(1)
    values = latest.sort_values("ref_period_start")["value"].to_numpy(dtype=float)
    if regime.rule == "sahm":
        if len(values) < 12:
            return None
        window = values[-12:]
        gap = float(np.mean(window[-3:]) - np.min(window))
        return gap < regime.threshold_pp
    raise ValueError(f"unknown regime rule {regime.rule!r}")
