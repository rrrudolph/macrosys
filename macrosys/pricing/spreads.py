"""Mean-reversion-free change in a credit spread (spec §3.3 C06, §7.3).

[M] The OAS print is the market spread. [J] The point change over the window is zero:
the level is what prices assume, and this reading does not forecast a return to a
historical mean or extend the sample's drift. [E] The range is a percentile band of
overlapping changes of the same calendar length in the visible history. [J] The
percentiles, the minimum number of changes, and the match tolerance are declared
next to the series in ``series_map.yaml``. A band that does not contain zero is
extended until it does, so the zero point stays inside the reported range.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import date, timedelta

import numpy as np

from macrosys.core.quantity import Quantity
from macrosys.pricing.errors import NoPricedInError

_QUARTER_DAYS = 365.25 / 4.0


def quarter_window_days(start_q: float, end_q: float) -> int:
    """Calendar days in a quarter-window, ``round((end − start) × 365.25 / 4)`` (spec §7.3).

    Four quarters is 365 days. The fraction 365.25 is the same year length the
    futures horizon uses. A zero-length window is zero days.
    """
    if end_q < start_q:
        raise ValueError(f"window end {end_q} is before start {start_q}")
    return int(round((end_q - start_q) * _QUARTER_DAYS))


def implied_spread_change(
    history: list[tuple[date, float]],
    *,
    window_days: int,
    level_unit: str,
    low_percentile: float,
    high_percentile: float,
    min_changes: int,
    tolerance_days: int,
    series_id: str,
) -> Quantity:
    """Implied spread change in basis points over ``window_days`` (spec §3.3 C06, §7.3).

    ``history`` is ``(date, level)`` already limited to prints visible at the as-of
    date. A percent level (the FRED OAS unit) differs in percentage points, and one
    percentage point is 100 bp. The point is 0 bp. Each start date is paired with
    the first later print on or after ``window_days`` calendar days, and the pair
    is kept when that print is at most ``tolerance_days`` late, so a weekend does
    not drop it and a much longer gap is not called the window. The range is the
    linear ``low_percentile`` to ``high_percentile`` of those overlapping changes.
    Overlap means the band describes this sample; it is not a standard error.
    Fewer than ``min_changes`` pairs raises ``NoPricedInError``. A zero-day window
    is the point 0 with range [0, 0].
    """
    if window_days < 0:
        raise ValueError(f"window_days must be >= 0, got {window_days}")
    if not 0.0 <= low_percentile < high_percentile <= 100.0:
        raise ValueError(f"percentiles {low_percentile} and {high_percentile} are not an ordered band inside 0–100")
    if min_changes < 2:
        raise ValueError(f"min_changes must be at least 2, got {min_changes}")
    if tolerance_days < 0:
        raise ValueError(f"tolerance_days must be >= 0, got {tolerance_days}")
    if window_days == 0:
        return Quantity(value=0.0, unit="bp", low=0.0, high=0.0, provenance=f"C06|{series_id}|zero_window")

    dates, levels = _collapse(history)
    changes = _paired_changes(dates, levels, window_days, tolerance_days, level_unit, series_id)
    if len(changes) < min_changes:
        span = f"{dates[0].isoformat()} to {dates[-1].isoformat()}" if dates else "no prints"
        raise NoPricedInError(
            f"{series_id} has {len(changes)} changes of {window_days} days "
            f"in the visible history ({span}); need at least {min_changes} "
            f"to quote the {low_percentile:g}th–{high_percentile:g}th percentile. "
            "The OAS level is the market spread, and the window is longer than the usable history."
        )
    raw_low, raw_high = np.percentile(changes, [low_percentile, high_percentile], method="linear")
    # Four decimal places in bp drops binary residue from a percent-level difference.
    low, high = round(float(raw_low), 4), round(float(raw_high), 4)
    extended = False
    if low > 0.0:
        low = 0.0
        extended = True
    if high < 0.0:
        high = 0.0
        extended = True
    code = f"C06|{series_id}|zero_change"
    if extended:
        code += "|extended_to_zero"
    return Quantity(value=0.0, unit="bp", low=low, high=high, provenance=code)


def _collapse(history: list[tuple[date, float]]) -> tuple[list[date], list[float]]:
    collapsed: dict[date, float] = {}
    for day, level in history:
        collapsed[day] = float(level)
    dates = sorted(collapsed)
    return dates, [collapsed[day] for day in dates]


def _paired_changes(
    dates: list[date],
    levels: list[float],
    window_days: int,
    tolerance_days: int,
    level_unit: str,
    series_id: str,
) -> list[float]:
    changes: list[float] = []
    for index, start in enumerate(dates):
        target = start + timedelta(days=window_days)
        matched = bisect_left(dates, target)
        if matched >= len(dates):
            continue
        if (dates[matched] - start).days > window_days + tolerance_days:
            continue
        changes.append(_to_bp(levels[matched] - levels[index], level_unit, series_id))
    return changes


def _to_bp(level_change: float, level_unit: str, series_id: str) -> float:
    """A difference of percent levels is percentage points; 1 pp = 100 bp (spec §2)."""
    if level_unit == "pct":
        return level_change * 100.0
    if level_unit == "pp":
        return level_change * 100.0
    if level_unit == "bp":
        return level_change
    raise NoPricedInError(
        f"{series_id} level unit {level_unit} is not pct, pp, or bp, so the change cannot be stated in bp"
    )
