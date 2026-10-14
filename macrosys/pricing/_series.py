"""Point-in-time series reads shared by the curve loaders and the path resolver (spec §9.2)."""

from __future__ import annotations

import math
from datetime import date

from macrosys.core.quantity import Quantity
from macrosys.store.observations import PointInTime


def latest(snapshot: PointInTime, series_id: str, as_of: date | None = None) -> Quantity | None:
    """Newest visible print of ``series_id``. ``as_of`` also drops later reference periods.

    The snapshot has already applied ``published_at``. A null value is treated as
    absent (spec §9.4), not as zero.
    """
    if as_of is None:
        return snapshot.latest_value(series_id)
    frame = snapshot.get(series_id, end=as_of)
    if frame.empty:
        return None
    ordered = frame.sort_values(["ref_period_end", "ref_period_start"], kind="mergesort")
    row = ordered.iloc[-1]
    value = row["value"]
    if value is None or _missing(value):
        return None
    return Quantity(value=float(value), unit=str(row["unit"]), provenance=series_id)


def month_starts(origin: date, count: int) -> list[date]:
    """``count`` month-start dates beginning with the month that contains ``origin``."""
    year, month = origin.year, origin.month
    out: list[date] = []
    for _ in range(count):
        out.append(date(year, month, 1))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return out


def _missing(value: object) -> bool:
    try:
        return math.isnan(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
