"""Market responses around release events (spec §10.1 item 11, §6.1 aligned returns).

The window runs from the last market close strictly before ``event_date`` to the ``h``-th market
close on or after it, so ``h = 1`` is the release-day close. A yield in percent is reported as a
change in basis points; a level series under ``pct_change`` is reported as a percent return.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
import numpy as np
import pandas as pd

from macrosys.core.quantity import Quantity
from macrosys.events.config import MarketSpec
from macrosys.events.schema import EventOutcome, SurpriseEvent
from macrosys.events.surprise import load_vintages


def market_series(conn: duckdb.DuckDBPyConnection, market: MarketSpec, as_of: datetime | None = None) -> pd.DataFrame:
    """Latest visible vintage per date for a daily market series: columns ``date``, ``value``, ``unit``."""
    frame = load_vintages(conn, market.series_id, as_of)
    if frame.empty:
        return pd.DataFrame({"date": pd.Series(dtype="object"), "value": pd.Series(dtype="float64"), "unit": pd.Series(dtype="object")})
    latest = frame.sort_values(["ref_period_start", "published_at"], kind="mergesort").groupby("ref_period_start").tail(1)
    out = latest.rename(columns={"ref_period_start": "date"})[["date", "value", "unit"]]
    return out.sort_values("date").reset_index(drop=True)


def response_change(pre: float, post: float, unit: str, response: str, provenance: str) -> Quantity:
    """Change from ``pre`` to ``post`` under the market's response rule, with the implied unit."""
    if response == "pct_change":
        if pre == 0.0:
            raise ZeroDivisionError("percent change from a zero level")
        return Quantity(value=(post / pre - 1.0) * 100.0, unit="pct", provenance=provenance)
    if unit == "pct":
        return Quantity(value=(post - pre) * 100.0, unit="bp", provenance=provenance)
    return Quantity(value=post - pre, unit=unit, provenance=provenance)


def market_outcomes(
    conn: duckdb.DuckDBPyConnection,
    events: list[SurpriseEvent],
    market: MarketSpec,
    horizons_days: list[int],
    as_of: datetime | None = None,
) -> list[EventOutcome]:
    """Responses of ``market`` to each event at each horizon; events without a full window are omitted."""
    series = market_series(conn, market, as_of)
    if series.empty or not events:
        return []
    dates = np.array(series["date"].tolist(), dtype=object)
    values = series["value"].to_numpy(dtype=float)
    units = series["unit"].tolist()
    out: list[EventOutcome] = []
    for event in events:
        first_on_or_after = int(np.searchsorted(dates, event.event_date, side="left"))
        pre_index = first_on_or_after - 1
        if pre_index < 0:
            continue
        for horizon in horizons_days:
            post_index = first_on_or_after + horizon - 1
            if post_index >= len(dates):
                continue
            if units[pre_index] != units[post_index]:
                raise ValueError(f"{market.series_id}: unit changed inside the outcome window")
            change = response_change(
                float(values[pre_index]),
                float(values[post_index]),
                str(units[post_index]),
                market.response,
                provenance=f"{market.series_id}@{horizon}d",
            )
            out.append(
                EventOutcome(
                    series_id=event.series_id,
                    ref_period_start=event.ref_period_start,
                    market_id=market.series_id,
                    horizon_days=horizon,
                    pre_date=dates[pre_index],
                    post_date=dates[post_index],
                    change=change,
                )
            )
    return out
