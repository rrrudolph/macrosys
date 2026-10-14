"""FX conversion rules and the base/local return identity (spec A09, §5.4, §9.1).

Currency translation is an explicit operation with a named rule. It is not a unit conversion:
USD and EUR are different units, and the rule that picked the rate stays on the result.
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np
import pandas as pd

from macrosys.instruments._index import values_on_dates


class FxConversionRule(StrEnum):
    """Which clock a converted number used (spec §9.1).

    ``ECB_1600CET`` is the ECB 16:00 CET reference rate for European-close assets.
    ``FED_H10_NOON`` is the Fed H.10 noon buying rate for weekly official series.
    ``VENDOR_CLOSE`` is the price vendor's USD close lined up with the ETF close, for daily
    portfolio accounting.
    """

    ECB_1600CET = "ECB_1600CET"
    FED_H10_NOON = "FED_H10_NOON"
    VENDOR_CLOSE = "VENDOR_CLOSE"


def convert_series(values: pd.Series, fx: pd.Series, rule: FxConversionRule) -> pd.Series:
    """Convert a value series by multiplying by ``fx`` under a named rule (spec §9.1).

    ``fx`` is base-currency units per one unit of the series currency (USD per EUR, not EUR per
    USD). The product is reindexed to ``values``; a missing rate stays missing and is never
    forward-filled. The rule and the quote convention are stored on ``Series.attrs``
    (``fx_conversion_rule``, ``provenance``). Pandas does not keep attrs through every later
    arithmetic step, so callers that store the number should copy the provenance off immediately.
    """
    if not isinstance(rule, FxConversionRule):
        raise ValueError(f"rule must be an FxConversionRule, got {rule!r}")
    value_index, value_series = values_on_dates(values)
    _, fx_series = values_on_dates(fx)
    rate = fx_series.reindex(value_index).to_numpy(dtype=np.float64)
    converted = np.asarray(value_series.to_numpy(), dtype=np.float64) * rate
    out = pd.Series(converted, index=value_index, name=values.name)
    out.attrs["fx_conversion_rule"] = rule
    out.attrs["provenance"] = {
        "spec": "§9.1",
        "fx_conversion_rule": rule.value,
        "operation": "values * fx",
        "fx_quote": "base-currency units per 1 unit of the series currency",
    }
    return out


def _operands(
    left: float | np.ndarray | pd.Series,
    right: float | np.ndarray | pd.Series,
) -> tuple[np.ndarray, np.ndarray, pd.Index | None]:
    left_series = left if isinstance(left, pd.Series) else None
    right_series = right if isinstance(right, pd.Series) else None
    if left_series is not None and right_series is not None:
        _, aligned_left = values_on_dates(left_series)
        _, aligned_right = values_on_dates(right_series)
        index = aligned_left.index
        return (
            np.asarray(aligned_left.to_numpy(), dtype=np.float64),
            np.asarray(aligned_right.reindex(index).to_numpy(), dtype=np.float64),
            index,
        )
    if left_series is not None:
        _, aligned_left = values_on_dates(left_series)
        return (
            np.asarray(aligned_left.to_numpy(), dtype=np.float64),
            np.asarray(right, dtype=np.float64),
            aligned_left.index,
        )
    if right_series is not None:
        _, aligned_right = values_on_dates(right_series)
        return (
            np.asarray(left, dtype=np.float64),
            np.asarray(aligned_right.to_numpy(), dtype=np.float64),
            aligned_right.index,
        )
    return np.asarray(left, dtype=np.float64), np.asarray(right, dtype=np.float64), None


def _wrap(values: np.ndarray, index: pd.Index | None) -> float | np.ndarray | pd.Series:
    if index is not None:
        return pd.Series(np.asarray(values, dtype=np.float64), index=index)
    array = np.asarray(values, dtype=np.float64)
    if array.ndim == 0:
        return float(array)
    return array


def compose_base_return(
    r_local: float | np.ndarray | pd.Series,
    r_fx: float | np.ndarray | pd.Series,
) -> float | np.ndarray | pd.Series:
    """Base-currency return from local and FX returns (spec A09).

    Implements ``1 + r_base = (1 + r_local) * (1 + r_fx)``, which keeps the cross term
    ``r_local * r_fx``. ``r_fx`` is the return of the local currency versus the base currency
    (positive when the local currency appreciates). ``decompose_base_return`` is the inverse
    wherever ``r_fx != -1``.
    """
    local, fx, index = _operands(r_local, r_fx)
    return _wrap((1.0 + local) * (1.0 + fx) - 1.0, index)


def decompose_base_return(
    r_base: float | np.ndarray | pd.Series,
    r_fx: float | np.ndarray | pd.Series,
) -> float | np.ndarray | pd.Series:
    """Local-currency return from base and FX returns (spec A09).

    Inverts ``1 + r_base = (1 + r_local) * (1 + r_fx)`` to
    ``r_local = (1 + r_base) / (1 + r_fx) - 1``. A currency return of -100% has no inverse and
    raises ``ValueError``. Missing inputs stay missing.
    """
    base, fx, index = _operands(r_base, r_fx)
    dead = np.isfinite(fx) & (fx == -1.0)
    if bool(np.any(dead)):
        raise ValueError("r_fx of -1 has no local-return inverse")
    with np.errstate(divide="ignore", invalid="ignore"):
        local = (1.0 + base) / (1.0 + fx) - 1.0
    return _wrap(local, index)
