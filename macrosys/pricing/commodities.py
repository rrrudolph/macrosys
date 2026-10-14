"""Futures curve shape, convenience yield, and ETF roll cost (spec §3.3 C07, A09).

Roll yield is the C07 form ``ln(near / far) / tenor``, via ``CostOfCarry``, so the
sign stays the one the mechanism uses: positive in backwardation. Year fractions
are actual days over 365.25. [J] That is not an exchange day-count.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible
from macrosys.mechanisms.impl.pricing import CostOfCarry
from macrosys.pricing._interval import add
from macrosys.pricing.errors import CurveError

_YEAR = 365.25
_CARRY = CostOfCarry()


class CurveShape(BaseModel):
    """Front-spread label plus annualised roll yields in share (spec §3.3 C07)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: Literal["contango", "backwardation", "flat"]
    front_roll_yield: Quantity
    year_roll_yield: Quantity | None
    notes: str


class CurvePoint(BaseModel):
    """One futures settlement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expiry: date
    price: Quantity


class Curve(BaseModel):
    """A commodity futures curve (spec §8.5 ``Curve``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    commodity: str
    points: tuple[CurvePoint, ...]


def futures_curve_shape(curve: list[tuple[date, float]]) -> CurveShape:
    """Contango or backwardation from the front two contracts, plus roll yields (spec §3.3 C07).

    The curve is ``(expiry, price)``. The front roll yield is the annualised
    ``ln(front / second) / Δt`` between the first two expiries, in share, actual
    days over 365.25. Positive roll yield is backwardation (the later contract is
    cheaper); negative is contango. The one-year roll yield is the same ratio from
    the front contract to the log-linear price one year later, when the curve
    reaches that date, otherwise null. The label uses only the front spread.
    """
    ordered = _ordered(curve)
    if len(ordered) < 2:
        raise CurveError("curve shape needs two contracts")
    near_dt, near_px = ordered[0]
    nxt_dt, nxt_px = ordered[1]
    front = _roll(near_px, nxt_px, _years(near_dt, nxt_dt))
    year = _year_roll(ordered)
    if nxt_px > near_px:
        shape: Literal["contango", "backwardation", "flat"] = "contango"
    elif nxt_px < near_px:
        shape = "backwardation"
    else:
        shape = "flat"
    return CurveShape(
        shape=shape,
        front_roll_yield=front,
        year_roll_yield=year,
        notes=(
            "Roll yield is ln(near/far) over actual/365.25 years, the C07 sign: "
            "positive in backwardation. The label is the front two contracts only."
        ),
    )


def implied_convenience_yield(
    spot: Quantity,
    future: Quantity,
    r: Quantity,
    storage: Quantity,
    tenor: Quantity,
) -> Quantity:
    """Convenience yield implied by the curve, ``r + storage − ln(F/S)/T`` (spec §3.3 C07).

    This calls ``CostOfCarry``. Spot and the future must share a unit and be positive.
    ``r`` and ``storage`` are shares (a percent is divided by 100). ``tenor`` is in
    years. The fair forward is ``F = S·e^{(r + storage − convenience)T}``.
    """
    if not compatible(spot.unit, future.unit):
        raise CurveError(f"spot unit {spot.unit} and future unit {future.unit} differ")
    rate = _as_share(r)
    cost = _as_share(storage)
    years = tenor.to("y") if tenor.unit != "y" else tenor
    out = _CARRY.apply(
        {"spot": spot, "futures": future, "tenor": years, "rate": rate, "storage": cost},
        {},
    )
    return out["implied_convenience"]


def etf_roll_cost_estimate(
    curve: list[tuple[date, float]],
    roll_schedule: list[tuple[date, date, float]],
) -> Quantity:
    """Annualised roll cost of a long futures ETF (spec §3.3 A09, C07).

    Each leg of ``roll_schedule`` is ``(near expiry, far expiry, weight)``. Weights
    must sum to 1. The leg's roll yield is the C07 yield between those two prices,
    and the cost is the weight-average of the negative of those yields: contango is
    a positive cost, backwardation a negative cost (a credit). Rate and storage are
    not estimated here; they are zero because roll yield does not use them. Year
    fractions are actual/365.25.
    """
    if not roll_schedule:
        raise CurveError("roll schedule is empty")
    prices = {expiry: price for expiry, price in _ordered(curve)}
    weight_sum = 0.0
    total: Quantity | None = None
    for near, far, weight in roll_schedule:
        if near not in prices or far not in prices:
            raise CurveError(f"roll leg {near.isoformat()} → {far.isoformat()} is not on the curve")
        if weight < 0.0:
            raise CurveError("roll weight is negative")
        weight_sum += weight
        roll = _roll(prices[near], prices[far], _years(near, far))
        leg = roll.scale(-weight, provenance="C07|A09")
        total = leg if total is None else add(total, leg, provenance="C07|A09")
    if abs(weight_sum - 1.0) > 1e-9:
        raise CurveError(f"roll weights sum to {weight_sum}, not 1")
    if total is None:
        raise CurveError("roll schedule produced no cost")
    return total


def _ordered(curve: list[tuple[date, float]]) -> list[tuple[date, float]]:
    if not curve:
        raise CurveError("futures curve is empty")
    ordered = sorted(curve, key=lambda item: item[0])
    seen: set[date] = set()
    for expiry, price in ordered:
        if expiry in seen:
            raise CurveError(f"duplicate expiry {expiry.isoformat()}")
        seen.add(expiry)
        if not math.isfinite(price) or price <= 0.0:
            raise CurveError(f"price {price} at {expiry.isoformat()} must be positive")
    return ordered


def _years(start: date, end: date) -> float:
    days = (end - start).days
    if days <= 0:
        raise CurveError(f"expiry {end.isoformat()} is not after {start.isoformat()}")
    return days / _YEAR


def _roll(near: float, far: float, tenor_y: float) -> Quantity:
    """C07 roll yield between two positive prices."""
    out = _CARRY.apply(
        {
            "spot": Quantity(value=near, unit="index", provenance="C07"),
            "futures": Quantity(value=far, unit="index", provenance="C07"),
            "tenor": Quantity(value=tenor_y, unit="y", provenance="C07"),
            "rate": Quantity(value=0.0, unit="share", provenance="C07"),
            "storage": Quantity(value=0.0, unit="share", provenance="C07"),
        },
        {},
    )
    return out["roll_yield"]


def _year_roll(ordered: list[tuple[date, float]]) -> Quantity | None:
    front_dt, front_px = ordered[0]
    target = front_dt + timedelta(days=365)
    if target > ordered[-1][0] or target < ordered[0][0]:
        return None
    price = _log_price(ordered, target)
    return _roll(front_px, price, _years(front_dt, target))


def _log_price(ordered: list[tuple[date, float]], when: date) -> float:
    for expiry, price in ordered:
        if expiry == when:
            return price
    for (left_dt, left_px), (right_dt, right_px) in zip(ordered, ordered[1:], strict=False):
        if left_dt <= when <= right_dt:
            span = (right_dt - left_dt).days
            weight = 0.0 if span == 0 else (when - left_dt).days / span
            return math.exp((1.0 - weight) * math.log(left_px) + weight * math.log(right_px))
    raise CurveError(f"no futures price at {when.isoformat()}")


def _as_share(q: Quantity) -> Quantity:
    if q.unit == "share":
        return q
    if q.unit == "pct":
        return Quantity(
            value=None if q.value is None else q.value / 100.0,
            unit="share",
            low=None if q.low is None else q.low / 100.0,
            high=None if q.high is None else q.high / 100.0,
            provenance=q.provenance,
        )
    raise CurveError(f"rate unit {q.unit} is not share or pct")
