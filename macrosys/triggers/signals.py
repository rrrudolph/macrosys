"""Signals on a dated series and the crossing test (spec §4.1; owner decision 2026-10-15).

Everything here is a pure function of a ``pd.Series`` indexed by date. The series is carried
forward onto a calendar-day grid so a lookback in calendar days reads the same window for a daily
close and a weekly print; the signal is then read back at the observed dates only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from macrosys.triggers.rules import SignalSpec, ThresholdSpec


@dataclass(frozen=True)
class Crossing:
    """A rule that holds at the last observation. ``crossed`` is the first date of the current run."""

    last_date: date
    value: float
    crossed: date


def compute_signal(series: pd.Series, spec: SignalSpec) -> pd.Series:
    """Return the signal at the series' observed dates. Undefined rows (not enough history) are NaN.

    ``drawdown`` is ``100 * (x / trailing_max - 1)`` with the trailing window ``lookback_days``
    calendar days inclusive of the row. ``change`` is ``x_t - x_{t-L}`` and ``pct_change`` is
    ``100 * (x_t / x_{t-L} - 1)``, with ``x_{t-L}`` the last value at or before ``t - L`` days.
    ``level`` is ``x``.
    """
    clean = _clean(series)
    if clean.empty:
        return clean
    if spec.kind == "level":
        return clean
    assert spec.lookback_days is not None
    lookback = spec.lookback_days
    grid = pd.date_range(clean.index.min(), clean.index.max(), freq="D")
    daily = clean.reindex(grid).ffill()
    # The first observation has no history: anything whose window reaches before it is undefined.
    first = clean.index.min()
    if spec.kind == "drawdown":
        trailing = daily.rolling(f"{lookback}D", min_periods=1).max()
        out = 100.0 * (daily / trailing - 1.0)
        defined = daily.index >= first + pd.Timedelta(days=lookback - 1)
    else:
        earlier = daily.shift(lookback)
        if spec.kind == "change":
            out = daily - earlier
        else:
            out = 100.0 * (daily / earlier - 1.0)
        defined = daily.index >= first + pd.Timedelta(days=lookback)
    out = out.where(defined)
    return out.reindex(clean.index)


def crossing(
    signal: pd.Series,
    threshold: ThresholdSpec,
    as_of: date,
    *,
    fresh_days: int,
    rearm_days: int,
    stale_days: int,
    published: pd.Series | None = None,
) -> Crossing | None:
    """The crossing that should fire at ``as_of``, or ``None``.

    Only observations at or before ``as_of`` are read. The rule fires when the condition holds at the
    last observation, that observation reached the store at most ``stale_days`` ago, and the fresh window — the
    ``fresh_days`` calendar days ending at ``as_of`` — contains a crossing: an observation where the
    condition holds and the previous observation is a defined false. Freshness is judged on the day
    the crossing became known: ``published`` gives each observation's publication date (aligned with
    ``signal``); without it the observation date is used, which is right for an exchange close. The
    earliest fresh crossing is the event, and every observation in the ``rearm_days`` before it must
    be a defined false. Weekly scans with ``fresh_days=7`` therefore see each crossing exactly once,
    a signal that stays past its threshold files once, and a signal that dips back and re-crosses
    inside the rearm window does not file again. An undefined (NaN) signal in the rearm window means
    the history is too short to say the rule was armed, so it does not fire.
    """
    dates = [pd.Timestamp(ts).date() for ts in signal.index]
    if published is None:
        known = dates
    else:
        aligned = published.reindex(signal.index)
        known = [dates[i] if pd.isna(ts) else pd.Timestamp(ts).date() for i, ts in enumerate(aligned)]
    keep = [index for index, day in enumerate(dates) if day <= as_of]
    visible = signal.iloc[keep]
    if visible.empty:
        return None
    days = [dates[index] for index in keep]
    seen = [known[index] for index in keep]
    last_date = days[-1]
    # Staleness is about the feed, so it is judged on when the last print arrived, not its period.
    if (as_of - seen[-1]).days > stale_days:
        return None
    values = visible.to_numpy(dtype=float)
    finite = np.isfinite(values)
    holds = np.array([finite[i] and threshold.holds(float(values[i])) for i in range(len(values))])
    if not holds[-1]:
        return None
    fresh_start = as_of - timedelta(days=fresh_days - 1)
    crossed_pos: int | None = None
    for pos in range(1, len(values)):
        if seen[pos] < fresh_start:
            continue
        if holds[pos] and finite[pos - 1] and not holds[pos - 1]:
            crossed_pos = pos
            break
    if crossed_pos is None:
        return None
    crossed = days[crossed_pos]
    window_start = crossed - timedelta(days=rearm_days)
    armed = [pos for pos in range(crossed_pos) if window_start <= days[pos] < crossed]
    if not armed or not all(finite[pos] and not holds[pos] for pos in armed):
        return None
    return Crossing(last_date=last_date, value=float(values[-1]), crossed=crossed)


def _clean(series: pd.Series) -> pd.Series:
    if series.empty:
        return pd.Series(dtype=float)
    out = series.copy()
    out.index = pd.to_datetime(out.index)
    out = out.astype(float).dropna().sort_index()
    return out[~out.index.duplicated(keep="last")]
