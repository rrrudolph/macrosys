"""What the market already prices, for one entity and variable (spec §7.3, §9.8).

Resolution order: a direct market quantity when one is mapped (policy rate via C09,
inflation via C04, a commodity price via the C07 curve, equity growth via C05);
otherwise the nearest instrument's implied growth with a wider range and low
confidence; otherwise ``NoPricedInError`` naming the missing series or mapping.
The horizon has to sit on the path. Nothing is extrapolated.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.pricing._interval import edges, lerp, relabel, sub
from macrosys.pricing._map import (
    commodity_for_entity,
    inflation_for_entity,
    instrument_block,
    nearest_instrument,
    policy_for_entity,
    variable_kind,
)
from macrosys.pricing._params import band
from macrosys.pricing.errors import CurveError, NoPricedInError
from macrosys.pricing.implied import ImpliedPaths
from macrosys.pricing.types import PathWithRange

_MONTH = re.compile(r"(\d{4}-\d{2})")
Confidence = Literal["low", "medium", "high"]


class PricedIn(BaseModel):
    """Market-implied change for one thesis variable (spec §7.3, §7.7).

    This is the pricing-layer result. The thesis record in ``decide.thesis`` has a
    different ``PricedIn`` (a ``source`` field and no confidence). Mapping one onto
    the other is left to the skeleton.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    implied_change: Quantity
    sources: list[str] = Field(min_length=1)
    method_notes: str = Field(min_length=1)
    confidence: Confidence

    def to_gap(self, expected: Quantity) -> Quantity:
        """Interval gap ``expected − implied``, via ``Quantity.sub`` (spec §7.7).

        A point is closed to ``[value, value]`` first, so a point expected against
        a ranged implied change keeps the interval. Units must be compatible.
        """
        return _closed(expected).sub(_closed(self.implied_change), provenance="priced_in")


def priced_in(
    entity: EntityKey,
    variable: str,
    horizon_q: tuple[float, float],
    implied: ImpliedPaths,
    as_of: date,
) -> PricedIn:
    """Market-implied change for ``variable`` on ``entity`` over ``horizon_q`` (spec §7.3).

    ``horizon_q`` is ``(start, end)`` in quarters. Policy is the change in the C09
    path between those endpoints, in percentage points. Inflation is the C04
    breakeven at the end of the window (the priced inflation rate, not a change
    from the latest CPI), in percent. A commodity price is the percent change
    along the futures curve. Equity growth is the C05 reverse-Gordon rate, in
    percent. An industry uses the mapped nearest instrument's growth, widened by
    the declared scale in ``params.yaml``, with confidence ``low``. Anything else
    raises ``NoPricedInError`` naming the missing mapping or series.
    """
    start, end = _window(horizon_q)
    kind = variable_kind(variable)
    policy = policy_for_entity(entity.entity_id)
    inflation = inflation_for_entity(entity.entity_id)
    commodity = commodity_for_entity(entity.entity_id) if entity.entity_type == "commodity" else None
    try:
        if kind == "policy" and policy is not None:
            return _policy(entity, start, end, implied, as_of, policy)
        if kind == "inflation" and inflation is not None:
            return _inflation(entity, end, implied, as_of, inflation)
        if kind == "price" and commodity is not None:
            return _commodity(entity, start, end, implied, as_of, commodity)
        if kind == "growth" and entity.entity_type == "instrument":
            return _instrument_growth(entity, implied, as_of)
    except CurveError as exc:
        raise _gap(entity, variable, str(exc)) from exc
    if entity.entity_type == "industry":
        return _industry(entity, variable, implied, as_of)
    raise _gap(
        entity,
        variable,
        "no direct market quantity is mapped, and the entity is not an industry with a nearest instrument",
    )


def _policy(entity: EntityKey, start: float, end: float, implied: ImpliedPaths, as_of: date, block: dict) -> PricedIn:
    ccy = str(block["ccy"])
    path = implied.policy_path(ccy, as_of)
    change = relabel(
        sub(at_horizon(path, end), at_horizon(path, start), provenance="C09"),
        "pp",
        provenance="C09",
    )
    if _todo(path):
        change = _stamp(change, "C09|TODO_estimate")
    prefix = str(block.get("futures_prefix") or "")
    sources = ["C09"]
    for _horizon, quantity in path:
        match = _MONTH.search(quantity.provenance or "")
        if match and prefix:
            series_id = f"{prefix}{match.group(1)}"
            if series_id not in sources:
                sources.append(series_id)
    return PricedIn(
        implied_change=change,
        sources=sources,
        method_notes=(
            f"C09 {block.get('convention')} futures for {ccy}. The rate is 100 minus the settlement, "
            "and a fed-funds contract is the month average. The implied change is the path at the end "
            "of the horizon minus the path at the start, in percentage points. The risk premium is a "
            "TODO_estimate band that widens each rate and does not shift the point."
        ),
        confidence="medium" if _todo(path) else "high",
    )


def _inflation(entity: EntityKey, end: float, implied: ImpliedPaths, as_of: date, block: dict) -> PricedIn:
    ccy = str(block["ccy"])
    path = implied.breakevens(ccy, as_of)
    rate = at_horizon(path, end)
    return PricedIn(
        implied_change=rate,
        sources=_path_sources(path, "C04"),
        method_notes=(
            f"C04 breakeven for {ccy} at the end of the horizon ({end:g} quarters). "
            "This is the priced inflation rate, the hurdle against a CPI print. "
            "Missing inflation-risk and liquidity premia are TODO_estimate bands that widen "
            "the range and do not replace the breakeven with zero. USD prints are the published "
            "par breakevens (T5YIE, T10YIE); a shorter tenor is not mapped."
        ),
        confidence="medium" if "TODO_estimate" in (rate.provenance or "") else "high",
    )


def _commodity(
    entity: EntityKey,
    start: float,
    end: float,
    implied: ImpliedPaths,
    as_of: date,
    block: dict,
) -> PricedIn:
    curve = implied.futures_curve(entity.entity_id, as_of)
    path: PathWithRange = [(_quarters(as_of, point.expiry), point.price) for point in curve.points]
    change = _percent_change(at_horizon(path, start), at_horizon(path, end))
    return PricedIn(
        implied_change=change,
        sources=_path_sources(path, "C07"),
        method_notes=(
            "C07 futures curve. The implied change is the percent change in the futures price "
            "between the horizon endpoints, actual days over 365.25 from the as-of date to each expiry. "
            "The curve is the forward price; a risk premium is not stripped off it. "
            f"Spot series {block.get('spot')} is not used as a substitute curve."
        ),
        confidence="high",
    )


def _instrument_growth(entity: EntityKey, implied: ImpliedPaths, as_of: date) -> PricedIn:
    growth = implied.implied_growth(entity.entity_id, as_of)
    block = instrument_block(entity.entity_id) or {}
    sources = ["C05", entity.entity_id]
    ey = block.get("earnings_yield")
    if ey:
        sources.append(str(ey))
    return PricedIn(
        implied_change=growth,
        sources=sources,
        method_notes=(
            "C05 reverse Gordon: perpetuity growth equals required return minus payout times the "
            "forward earnings yield. Required return is the TODO_estimate band. This is not a "
            "finite-horizon DCF."
        ),
        confidence="medium" if "TODO_estimate" in (growth.provenance or "") else "high",
    )


def _industry(entity: EntityKey, variable: str, implied: ImpliedPaths, as_of: date) -> PricedIn:
    link = nearest_instrument(entity.entity_id)
    if link is None or not link.get("instrument_id"):
        raise _gap(entity, variable, "nearest instrument in series_map.yaml")
    instrument_id = str(link["instrument_id"])
    growth = implied.implied_growth(instrument_id, as_of)
    widened = _widen(growth)
    block = instrument_block(instrument_id) or {}
    sources = ["C05", instrument_id, "nearest_instrument"]
    ey = block.get("earnings_yield")
    if ey:
        sources.append(str(ey))
    scale = band("fallback_half_width_scale")
    factor = scale.value if scale.value is not None else scale.mid
    return PricedIn(
        implied_change=widened,
        sources=sources,
        method_notes=(
            f"No direct market quantity for industry {entity.entity_id} variable {variable!r}. "
            f"Spec §7.3 uses the nearest instrument ({instrument_id}) implied growth and lowers "
            f"confidence. The range is widened by the declared factor {factor:g} (TODO_estimate) "
            "and the point is the centre of the instrument range. The link is a declared example, "
            "not a checked industry-to-ETF crosswalk."
        ),
        confidence="low",
    )


def at_horizon(path: PathWithRange, horizon_q: float) -> Quantity:
    """Value of a path at one horizon, linear between knots. No extrapolation."""
    if not path:
        raise CurveError("implied path is empty")
    points = sorted(path, key=lambda item: item[0])
    first, last = points[0][0], points[-1][0]
    if horizon_q < first - 1e-9 or horizon_q > last + 1e-9:
        raise CurveError(f"horizon {horizon_q:g}q is outside the path [{first:g}, {last:g}]q")
    for horizon, quantity in points:
        if abs(horizon - horizon_q) <= 1e-9:
            return quantity
    for (left_h, left_q), (right_h, right_q) in zip(points, points[1:], strict=False):
        if left_h <= horizon_q <= right_h:
            span = right_h - left_h
            weight = 0.0 if span == 0.0 else (horizon_q - left_h) / span
            return lerp(left_q, right_q, weight, provenance="path_interp")
    raise CurveError(f"horizon {horizon_q:g}q was not bracketed")


def _percent_change(start: Quantity, end: Quantity) -> Quantity:
    start_lo, start_hi = edges(start)
    end_lo, end_hi = edges(end)
    if min(start_lo, start_hi) <= 0.0:
        raise CurveError("futures price is not positive")
    corners = [(price / base - 1.0) * 100.0 for price in (end_lo, end_hi) for base in (start_lo, start_hi)]
    low, high = min(corners), max(corners)
    value = None
    if start.value is not None and end.value is not None:
        value = (float(end.value) / float(start.value) - 1.0) * 100.0
    provenance = "C07"
    if abs(high - low) <= 1e-12:
        return Quantity(value=value if value is not None else low, unit="pct", provenance=provenance)
    return Quantity(value=value, unit="pct", low=low, high=high, provenance=provenance)


def _widen(growth: Quantity) -> Quantity:
    factor = band("fallback_half_width_scale")
    scale = float(factor.value if factor.value is not None else factor.mid)
    low, high = edges(growth)
    centre = float(growth.value) if growth.value is not None else 0.5 * (low + high)
    half = 0.5 * (high - low) * scale
    return Quantity(
        value=centre,
        unit=growth.unit,
        low=centre - half,
        high=centre + half,
        provenance=f"{growth.provenance or 'C05'}|widened|TODO_estimate",
    )


def _window(horizon_q: tuple[float, float]) -> tuple[float, float]:
    start, end = float(horizon_q[0]), float(horizon_q[1])
    if end < start:
        raise ValueError(f"horizon_q end {end} is before start {start}")
    return start, end


def _quarters(as_of: date, expiry: date) -> float:
    return (expiry - as_of).days / (365.25 / 4.0)


def _todo(path: PathWithRange) -> bool:
    return any("TODO_estimate" in (quantity.provenance or "") for _horizon, quantity in path)


def _stamp(quantity: Quantity, provenance: str) -> Quantity:
    return Quantity(
        value=quantity.value,
        unit=quantity.unit,
        low=quantity.low,
        high=quantity.high,
        provenance=provenance,
    )


def _path_sources(path: PathWithRange, mechanism: str) -> list[str]:
    sources = [mechanism]
    for _horizon, quantity in path:
        prov = quantity.provenance or ""
        for part in prov.split("|"):
            if ":" in part and part not in sources and not part.startswith("C0"):
                sources.append(part)
        if prov and prov not in sources and ":" in prov and "|" not in prov:
            sources.append(prov)
    return sources


def _closed(quantity: Quantity) -> Quantity:
    if quantity.low is not None and quantity.high is not None:
        return quantity
    low, high = edges(quantity)
    value = float(quantity.value) if quantity.value is not None else 0.5 * (low + high)
    return Quantity(value=value, unit=quantity.unit, low=low, high=high, provenance=quantity.provenance)


def _gap(entity: EntityKey, variable: str, missing: str) -> NoPricedInError:
    return NoPricedInError(
        f"no market-implied quantity for {entity.entity_type} {entity.entity_id} "
        f"variable {variable!r}; missing: {missing}"
    )
