"""Date-index normalization shared by the series edges (returns, FX, corporate actions)."""

from __future__ import annotations

import pandas as pd


def as_naive_midnight(index: pd.Index) -> pd.DatetimeIndex:
    """Naive midnight timestamps so date keys compare equal across tz-aware and date inputs."""
    idx = pd.DatetimeIndex(pd.to_datetime(index))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.normalize()


def values_on_dates(series: pd.Series) -> tuple[pd.DatetimeIndex, pd.Series]:
    """Sort a series onto a unique naive date index. Raises if two rows share a date."""
    idx = as_naive_midnight(series.index)
    out = pd.Series(series.to_numpy(), index=idx, name=series.name)
    if not out.index.is_unique:
        raise ValueError("series has duplicate dates")
    out = out.sort_index()
    return pd.DatetimeIndex(out.index), out
