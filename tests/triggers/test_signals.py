"""Signals and the crossing test (owner decision 2026-10-15). Pure functions, no store."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from macrosys.triggers.rules import SignalSpec, ThresholdSpec
from macrosys.triggers.signals import compute_signal, crossing

FRESH = dict(fresh_days=7, rearm_days=20, stale_days=7)


def _series(values: list[float], start: date = date(2026, 1, 1)) -> pd.Series:
    index = pd.bdate_range(pd.Timestamp(start), periods=len(values))
    return pd.Series(values, index=index, dtype=float)


def _day(series: pd.Series, position: int) -> date:
    return pd.Timestamp(series.index[position]).date()


def test_drawdown_change_pct_change_and_level() -> None:
    series = _series([100, 110, 99, 88, 90])
    drawdown = compute_signal(series, SignalSpec(kind="drawdown", lookback_days=3))
    # Thursday 88 against the three-calendar-day high (Tue 110, Wed 99, Thu 88) is -20%.
    assert drawdown.iloc[3] == pytest.approx(-20.0)
    assert np.isnan(drawdown.iloc[0])
    long_window = compute_signal(series, SignalSpec(kind="drawdown", lookback_days=10))
    assert long_window.isna().all()  # five business days cannot fill a ten-day window

    change = compute_signal(series, SignalSpec(kind="change", lookback_days=1))
    assert change.iloc[1] == pytest.approx(10.0)
    assert np.isnan(change.iloc[0])
    # Across a weekend the lookback reads the last value at or before t - 1 day (Friday for Monday).
    assert change.iloc[4] == pytest.approx(2.0)

    pct = compute_signal(series, SignalSpec(kind="pct_change", lookback_days=1))
    assert pct.iloc[1] == pytest.approx(10.0)

    level = compute_signal(series, SignalSpec(kind="level"))
    assert level.tolist() == series.tolist()


def test_crossing_fires_once_per_crossing() -> None:
    threshold = ThresholdSpec(op=">=", value=30.0, unit="index")
    # Thirty quiet business days, then a spike that stays up.
    signal = _series([20.0] * 30 + [35.0] * 15)
    crossed = _day(signal, 30)

    first = crossing(signal, threshold, crossed, **FRESH)
    assert first is not None and first.crossed == crossed and first.value == 35.0
    # Later in the same fresh window the same crossing is still the event.
    later = crossing(signal, threshold, _day(signal, 33), **FRESH)
    assert later is not None and later.crossed == crossed
    # A week on the condition still holds but there is no new crossing: no second fire.
    assert crossing(signal, threshold, crossed + timedelta(days=7), **FRESH) is None


def test_flicker_inside_the_rearm_window_does_not_refile() -> None:
    threshold = ThresholdSpec(op="<=", value=-10.0, unit="pct")
    # Quiet, then cross, dip back above, cross again two days later.
    signal = _series([0.0] * 30 + [-12.0, -9.0, -14.0, -15.0])
    hit = crossing(signal, threshold, _day(signal, -1), **FRESH)
    # The event is the first crossing in the fresh window, not the re-cross.
    assert hit is not None and hit.crossed == _day(signal, 30)
    # A week later another dip and re-cross: the earlier crossing sits in the rearm window, so no fire.
    later = _series([0.0] * 30 + [-12.0, -9.0, -14.0, -15.0, -8.0, -11.0])
    assert crossing(later, threshold, _day(later, -1) + timedelta(days=6), **FRESH) is None


def test_undefined_history_in_the_rearm_window_does_not_arm() -> None:
    threshold = ThresholdSpec(op=">=", value=30.0, unit="index")
    short = _series([35.0, 36.0, 37.0])
    assert crossing(short, threshold, _day(short, -1), **FRESH) is None
    with_nan = _series([np.nan] * 5 + [20.0] * 3 + [35.0])
    assert crossing(with_nan, threshold, _day(with_nan, -1), **FRESH) is None


def test_stale_last_observation_does_not_fire() -> None:
    threshold = ThresholdSpec(op=">=", value=30.0, unit="index")
    signal = _series([20.0] * 30 + [35.0])
    last = _day(signal, -1)
    assert crossing(signal, threshold, last, **FRESH) is not None
    assert crossing(signal, threshold, last + timedelta(days=8), **FRESH) is None


def test_freshness_follows_publication_not_reference_date() -> None:
    threshold = ThresholdSpec(op=">=", value=30.0, unit="index")
    signal = _series([20.0] * 30 + [35.0] * 3)
    crossed = _day(signal, 30)
    # Every print reached the store ten days after its reference date.
    published = pd.Series(signal.index + pd.Timedelta(days=10), index=signal.index)
    late_as_of = crossed + timedelta(days=12)
    assert crossing(signal, threshold, late_as_of, **FRESH) is None
    by_publication = crossing(signal, threshold, late_as_of, published=published, **FRESH)
    assert by_publication is not None and by_publication.crossed == crossed
