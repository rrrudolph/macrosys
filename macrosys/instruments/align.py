"""Aligned return panels and ETF premium/discount (spec §6.1, C10).

Statistics start from this panel. A day with no print is missing: it is never stored as zero and
never forward-filled. Instruments with a long hole inside the aligned window are marked stale and
stay out of estimation.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from macrosys.instruments._index import values_on_dates

Calendar = Literal["union", "intersection"]
ReturnMethod = Literal["log", "simple"]
PREMIUM_DISCOUNT_FLAG = "premium_discount"


class AlignedReturns(BaseModel):
    """Return panel on one calendar (spec §6.1).

    ``prices`` is the aligned close panel; missing prints are NaN. ``returns`` is the simple or
    log change versus the previous aligned date, and is NaN when either price is missing — a gap
    is not bridged by using the last print before it. ``stale`` lists instruments whose longest
    run of missing prices on this calendar exceeds ``max_gap_days``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    returns: pd.DataFrame
    prices: pd.DataFrame
    stale: tuple[str, ...]
    calendar: Calendar
    method: ReturnMethod
    max_gap_days: int


def _close_series(frame: pd.DataFrame) -> pd.Series:
    if "close" not in frame.columns:
        raise ValueError("bars need a close column")
    closes = frame["close"]
    if "trade_date" in frame.columns:
        series = pd.Series(closes.to_numpy(), index=pd.to_datetime(frame["trade_date"]))
    else:
        series = pd.Series(closes.to_numpy(), index=frame.index)
    _, ordered = values_on_dates(series)
    return ordered


def _longest_missing(mask: np.ndarray) -> int:
    """Longest run of True values. The run is counted in aligned rows, not raw calendar days."""
    if mask.size == 0 or not bool(mask.any()):
        return 0
    padded = np.concatenate(([False], mask.astype(bool), [False]))
    changes = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    return int((ends - starts).max())


def aligned_returns(
    bars: dict[str, pd.DataFrame],
    calendar: Calendar = "union",
    max_gap_days: int = 2,
    *,
    method: ReturnMethod = "simple",
) -> AlignedReturns:
    """Align ETF closes onto one calendar and compute returns (spec §6.1).

    Each frame has a ``close`` column and either a ``trade_date`` column or a date index.
    ``calendar='union'`` is the spec default: every trading date any instrument printed, so a
    holiday that only one listing observed is an explicit missing row for the others.
    ``intersection`` keeps only dates present for every instrument, which drops one-sided gaps
    from the stale test; a return on that panel then spans the dropped days. Missing closes stay
    NaN. A return is the simple difference
    ``p_t / p_{t-1} - 1`` or the log difference, and is NaN unless both prices exist; the first
    row is therefore NaN. An instrument is stale when more than ``max_gap_days`` consecutive
    aligned prices are missing, including a hole at either end of the window. Weekends that
    nobody traded are not in the union, so they are not gaps.
    """
    if not bars:
        raise ValueError("no bars")
    if calendar not in ("union", "intersection"):
        raise ValueError(f"calendar must be union or intersection, got {calendar!r}")
    if method not in ("log", "simple"):
        raise ValueError(f"method must be log or simple, got {method!r}")
    if max_gap_days < 0:
        raise ValueError("max_gap_days must be >= 0")
    series = {instrument_id: _close_series(frame) for instrument_id, frame in bars.items()}
    ordered_ids = sorted(series)
    calendar_index = series[ordered_ids[0]].index
    for instrument_id in ordered_ids[1:]:
        other = series[instrument_id].index
        if calendar == "union":
            calendar_index = calendar_index.union(other)
        else:
            calendar_index = calendar_index.intersection(other)
    calendar_index = pd.DatetimeIndex(sorted(pd.DatetimeIndex(calendar_index).unique()))
    if len(calendar_index) == 0:
        raise ValueError("aligned calendar is empty")
    prices = pd.DataFrame({instrument_id: series[instrument_id].reindex(calendar_index) for instrument_id in ordered_ids})
    levels = prices.to_numpy(dtype=np.float64)
    previous = np.full(levels.shape, np.nan, dtype=np.float64)
    if len(levels) > 1:
        previous[1:] = levels[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = levels / previous
        returns = np.log(ratio) if method == "log" else ratio - 1.0
    return_frame = pd.DataFrame(returns, index=calendar_index, columns=ordered_ids)
    stale = tuple(
        instrument_id
        for instrument_id in ordered_ids
        if _longest_missing(np.isnan(prices[instrument_id].to_numpy(dtype=np.float64))) > max_gap_days
    )
    return AlignedReturns(
        returns=return_frame,
        prices=prices,
        stale=stale,
        calendar=calendar,
        method=method,
        max_gap_days=max_gap_days,
    )


def premium_discount(price: pd.Series, nav: pd.Series, *, threshold: float = 0.01) -> pd.Series:
    """Premium or discount to NAV, ``price / nav - 1`` (spec C10, §6.1).

    The two series are aligned on the union of their dates; a missing price or NAV stays NaN and
    is not filled. ``Series.attrs['flag']`` is ``premium_discount`` when any finite observation
    has absolute premium above ``threshold``, and ``attrs['breach_dates']`` lists those dates.
    The default threshold of 0.01 (100 bp) is a monitoring default, not a mandate parameter.
    """
    if threshold < 0.0:
        raise ValueError("threshold must be non-negative")
    _, price_series = values_on_dates(price)
    _, nav_series = values_on_dates(nav)
    calendar_index = price_series.index.union(nav_series.index)
    calendar_index = pd.DatetimeIndex(sorted(pd.DatetimeIndex(calendar_index).unique()))
    price_v = price_series.reindex(calendar_index).to_numpy(dtype=np.float64)
    nav_v = nav_series.reindex(calendar_index).to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        premium = price_v / nav_v - 1.0
    out = pd.Series(premium, index=calendar_index, name="premium_discount")
    finite = np.isfinite(premium)
    breach = finite & (np.abs(premium) > threshold)
    breach_dates = [ts.date() for ts in calendar_index[breach]]
    out.attrs["threshold"] = float(threshold)
    out.attrs["breach_dates"] = breach_dates
    out.attrs["flag"] = PREMIUM_DISCOUNT_FLAG if breach_dates else None
    return out
