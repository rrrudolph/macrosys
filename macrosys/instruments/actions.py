"""Corporate actions and the total-return close rebuilt from raw prices (spec §6.1, §9.1).

Vendor adjusted-close columns are a cross-check, not the record. The series built here is the one
portfolio accounting uses. Dates where it disagrees with a vendor series feed ``price_discrepancy``.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
from pydantic import ConfigDict, field_validator, model_validator

from macrosys.core.contracts import ActionType
from macrosys.core.contracts import CorporateAction as _CoreCorporateAction
from macrosys.instruments._index import values_on_dates
from macrosys.instruments.holdings import normalize_ccy

PRICE_DISCREPANCY_FLAG = "price_discrepancy"

__all__ = [
    "PRICE_DISCREPANCY_FLAG",
    "ActionType",
    "CorporateAction",
    "compare_with_vendor_adjusted",
    "total_return_series",
]


class CorporateAction(_CoreCorporateAction):
    """Core corporate-action record, plus the split and distribution checks (spec §9.1).

    The fields are ``macrosys.core.contracts.CorporateAction``. ``ratio`` is new shares per old
    share for a split (2.0 means 2-for-1) and is otherwise null. ``amount`` is cash per share for
    ``cash_div`` and ``capital_gain``, in ``ccy``, quoted in the same share units as the raw close
    on ``ex_date``. ``ccy`` is normalised to ISO 4217. Unknown fields are rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("ccy", mode="before")
    @classmethod
    def _ccy(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return normalize_ccy(value)

    @model_validator(mode="after")
    def _shape(self) -> CorporateAction:
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        if self.type == "split":
            if self.ratio is None or self.ratio <= 0.0:
                raise ValueError("split requires a positive ratio (new shares per old share)")
        elif self.amount is None or self.ccy is None:
            raise ValueError(f"{self.type} requires amount and ccy")
        return self


def total_return_series(closes: pd.Series, actions: list[CorporateAction]) -> pd.Series:
    """Build a split-adjusted total-return close from raw closes and corporate actions (spec §6.1, §9.1).

    Raw ``closes`` are unadjusted last prices. A split's ``ratio`` is new shares per old share
    (2.0 for a 2-for-1); the raw close on the ex-date is already the post-split price, and every
    close strictly before that ex-date is divided by ``ratio`` (later splits compound). A
    ``cash_div`` or ``capital_gain`` ``amount`` is cash per share in the same share units as the
    raw ex-date close; later splits scale it down when it is expressed per split-adjusted share.
    On the ex-date the cash is reinvested at that day's split-adjusted close, so the simple return
    that day is ``(P_sa + D_sa) / P_sa_prev - 1``. The series is backward-adjusted so the last
    value equals the raw close: every date strictly before the ex-date is multiplied by
    ``P_sa_ex / (P_sa_ex + D_sa)``. When a split and a distribution share an ex-date, the close is
    post-split and the cash amount is per post-split share. Each ex-date must appear in ``closes``.
    This is the economic reinvestment identity, not the vendor shortcut ``(P_ex - D) / P_ex``.
    """
    idx, ordered = values_on_dates(closes)
    if ordered.empty:
        raise ValueError("closes are empty")
    instrument_ids = {action.instrument_id for action in actions}
    if len(instrument_ids) > 1:
        raise ValueError("corporate actions span multiple instruments")
    levels = np.asarray(ordered.to_numpy(), dtype=np.float64).copy()
    if not np.isfinite(levels).all():
        raise ValueError("closes must be finite")

    def position(ex_date: date) -> int:
        loc = int(idx.get_indexer([pd.Timestamp(ex_date)])[0])
        if loc < 0:
            raise ValueError(f"ex_date {ex_date.isoformat()} is not in the close series")
        return loc

    splits = [action for action in actions if action.type == "split"]
    distributions = [action for action in actions if action.type != "split"]
    for split in splits:
        ratio = split.ratio
        if ratio is None:
            raise ValueError("split requires a ratio")
        levels[: position(split.ex_date)] /= ratio
    adjusted = levels.copy()
    for distribution in sorted(distributions, key=lambda action: action.ex_date, reverse=True):
        if distribution.amount is None:
            raise ValueError("distribution requires an amount")
        later = 1.0
        for split in splits:
            ratio = split.ratio
            if ratio is None:
                raise ValueError("split requires a ratio")
            if split.ex_date > distribution.ex_date:
                later *= ratio
        amount = float(distribution.amount) / later
        i = position(distribution.ex_date)
        price = float(levels[i])
        denominator = price + amount
        if denominator == 0.0:
            raise ValueError(f"dividend reinvestment undefined on {distribution.ex_date.isoformat()}")
        adjusted[:i] *= price / denominator
    out = pd.Series(adjusted, index=idx, name="adj_close")
    out.attrs["convention"] = "reinvest_at_ex_date_close_backward_adjusted"
    return out


def compare_with_vendor_adjusted(ours: pd.Series, vendor: pd.Series, tol: float) -> list[date]:
    """Dates where our total-return close and a vendor adjusted close disagree (spec §9.1, §9.6).

    Comparison is on the intersection of dates. The relative difference is
    ``abs(ours - vendor) / abs(vendor)``. A date is returned when that ratio exceeds ``tol``,
    when one side is missing, or when the vendor print is 0 and ours is not. A non-empty result
    is the input to a ``price_discrepancy`` quality flag; this function does not write the flag.
    """
    if not math.isfinite(tol) or tol < 0.0:
        raise ValueError("tol must be a non-negative finite number")
    _, left = values_on_dates(ours)
    _, right = values_on_dates(vendor)
    common = left.index.intersection(right.index)
    ours_v = left.reindex(common).to_numpy(dtype=np.float64)
    vendor_v = right.reindex(common).to_numpy(dtype=np.float64)
    one_missing = np.isnan(ours_v) ^ np.isnan(vendor_v)
    comparable = ~np.isnan(ours_v) & ~np.isnan(vendor_v)
    vendor_zero = comparable & (vendor_v == 0.0) & (ours_v != 0.0)
    relative = np.zeros(len(common), dtype=bool)
    usable = comparable & (vendor_v != 0.0)
    relative[usable] = np.abs(ours_v[usable] - vendor_v[usable]) / np.abs(vendor_v[usable]) > tol
    mask = one_missing | vendor_zero | relative
    return [ts.date() for ts in common[mask]]
