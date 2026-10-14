"""Reverse Gordon, reverse DCF, ERP proxies, and equity duration (spec §3.3 C05, C13).

Growth and returns are percent. A payout is a share. A price-earnings input is a
``ratio``. Ranges use interval arithmetic; the DCF inverts the two-stage model at
each corner of the discount and terminal-growth box.
"""

from __future__ import annotations

from macrosys.core.quantity import Quantity
from macrosys.core.units import UnitError, compatible
from macrosys.pricing._interval import add, edges, mul, provenance_code, sub, to_pct, to_share
from macrosys.pricing.errors import CurveError


def implied_growth_from_multiple(
    pe_or_ey: Quantity,
    required_return_range: Quantity,
    payout: Quantity,
) -> Quantity:
    """Perpetuity growth implied by a multiple, ``g = r − payout × earnings yield`` (spec §3.3 C05).

    Assumptions: constant growth forever, constant payout, and a flat required
    return, all nominal. A ``ratio`` input is a price-earnings multiple and the
    forward earnings yield is ``1 / PE``. A ``pct`` or ``share`` input is already
    the forward earnings yield. Payout is a share of earnings. There is no fade,
    no leverage adjustment, and no split between real growth and inflation. The
    algebra is the reverse of the Gordon price ``P = payout × E / (r − g)``.
    Ranges on the required return and the payout are treated as independent.
    """
    earnings_yield = _earnings_yield_pct(pe_or_ey)
    required = to_pct(required_return_range)
    payout_share = to_share(payout)
    code = provenance_code("C05", earnings_yield, required, payout_share)
    dividend_yield = mul(earnings_yield, payout_share, unit="pct", provenance=code)
    return sub(required, dividend_yield, provenance=code)


def dcf_price(
    fcf0: Quantity,
    growth: Quantity,
    horizon_years: int,
    terminal_growth: Quantity,
    discount: Quantity,
) -> Quantity:
    """Two-stage DCF price in ``fcf0``'s unit (spec §3.3 C05).

    Cash flow in year ``t`` is ``fcf0 × (1+g)^t``, discounted annually. The
    terminal value at the horizon is next year's cash flow, grown at the terminal
    rate, divided by ``discount − terminal growth``. Point values are used; a
    range uses its point, or its midpoint when it has none. This is the map
    ``reverse_dcf`` inverts.
    """
    level = _dcf(
        _amount(fcf0),
        _decimal(growth),
        _decimal(terminal_growth),
        _decimal(discount),
        horizon_years,
    )
    return Quantity(value=level, unit=fcf0.unit, provenance="C05")


def reverse_dcf(
    price: Quantity,
    fcf0: Quantity,
    horizon_years: int,
    terminal_growth_range: Quantity,
    discount_range: Quantity,
) -> Quantity:
    """Implied constant growth that recovers ``price``, in percent (spec §3.3 C05).

    The price model is ``dcf_price``: annual cash flows for ``horizon_years``, then
    a Gordon terminal value. Growth during the explicit years is solved by
    bisection. The discount rate and the terminal growth are a box; because the
    price rises with growth and with terminal growth and falls with the discount
    rate, the implied-growth extremes are at the corners, and each corner is
    bisected separately. The point is the solution at the inputs' own points (or
    midpoints). Terminal growth must stay below the discount rate. Cash flow and
    price must be positive and in the same currency.
    """
    if not compatible(price.unit, fcf0.unit):
        raise UnitError(f"price unit {price.unit} and cash-flow unit {fcf0.unit} differ")
    target = _amount(price)
    cash = _amount(fcf0.to(price.unit) if fcf0.unit != price.unit else fcf0)
    corners: list[float] = []
    for terminal in _edge_values(terminal_growth_range):
        for discount in _edge_values(discount_range):
            corners.append(_solve_growth(target, cash, terminal, discount, horizon_years))
    low = min(corners) * 100.0
    high = max(corners) * 100.0
    centre = _solve_growth(
        target,
        cash,
        _decimal(terminal_growth_range),
        _decimal(discount_range),
        horizon_years,
    ) * 100.0
    code = provenance_code("C05", terminal_growth_range, discount_range)
    if abs(high - low) <= 1e-10:
        return Quantity(value=centre, unit="pct", provenance=code)
    return Quantity(value=centre, unit="pct", low=low, high=high, provenance=code)


def erp_proxies(
    earnings_yield: Quantity,
    real_yield: Quantity,
    dividend_yield: Quantity,
    growth_range: Quantity,
) -> dict[str, Quantity]:
    """Two ERP ranges: earnings yield minus real yield, and dividend yield plus growth minus real yield (spec §3.3 C13).

    Both are model-dependent. The earnings-yield gap is the C13 form. The dividend
    discount form takes the Gordon required return as ``dividend yield + growth``
    and subtracts the real yield, so a growth range stays a range. Inputs are
    percent or shares. Nothing here collapses a growth range to its midpoint.
    """
    earnings = to_pct(earnings_yield)
    real = to_pct(real_yield)
    dividend = to_pct(dividend_yield)
    growth = to_pct(growth_range)
    code = provenance_code("C13", earnings, real, dividend, growth)
    gordon_return = add(dividend, growth, provenance=code)
    return {
        "earnings_yield_minus_real": sub(earnings, real, provenance=code),
        "dividend_discount": sub(gordon_return, real, provenance=code),
    }


def equity_duration(dividend_yield: Quantity, growth: Quantity) -> Quantity:
    """Gordon duration in years, ``1 / (trailing yield × (1+g))`` (spec §3.3 C05).

    ``dividend_yield`` is the trailing yield ``D0/P``. The forward yield is that
    times ``1+g``. A one percentage-point discount-rate rise then changes the
    price by about ``−duration`` percent, which is the unit convention of C05
    (``d_price_pct = −duration × d_discount_pp``). Yield and growth are percent
    or shares. The forward yield must stay positive.
    """
    trailing = to_share(dividend_yield)
    g = to_share(growth)
    one = Quantity(value=1.0, unit="share", provenance="C05")
    forward = mul(trailing, add(one, g, provenance="C05"), unit="share", provenance="C05")
    lo, hi = edges(forward)
    if lo <= 0.0 or hi <= 0.0:
        raise CurveError("forward dividend yield must be positive to define equity duration")
    value = None if forward.value is None else 1.0 / float(forward.value)
    if forward.low is None and forward.high is None:
        return Quantity(value=value, unit="y", provenance="C05")
    return Quantity(value=value, unit="y", low=1.0 / hi, high=1.0 / lo, provenance="C05")


def _earnings_yield_pct(pe_or_ey: Quantity) -> Quantity:
    if pe_or_ey.unit in {"pct", "share"}:
        return to_pct(pe_or_ey)
    if pe_or_ey.unit != "ratio":
        raise UnitError(f"{pe_or_ey.unit} is not a PE ratio or an earnings yield")
    lo, hi = edges(pe_or_ey)
    if lo <= 0.0 or hi <= 0.0:
        raise CurveError("PE multiple must be positive")
    value = None if pe_or_ey.value is None else 100.0 / float(pe_or_ey.value)
    if pe_or_ey.low is None and pe_or_ey.high is None:
        return Quantity(value=value, unit="pct", provenance=pe_or_ey.provenance)
    return Quantity(
        value=value,
        unit="pct",
        low=100.0 / hi,
        high=100.0 / lo,
        provenance=pe_or_ey.provenance,
    )


def _dcf(fcf0: float, growth: float, terminal: float, discount: float, horizon: int) -> float:
    if horizon < 1:
        raise CurveError("DCF horizon must be at least 1 year")
    if discount <= terminal:
        raise CurveError(f"terminal growth {terminal} is not below the discount rate {discount}")
    if fcf0 <= 0.0:
        raise CurveError("DCF cash flow must be positive")
    total = 0.0
    for year in range(1, horizon + 1):
        cash = fcf0 * (1.0 + growth) ** year
        total += cash / (1.0 + discount) ** year
    following = fcf0 * (1.0 + growth) ** horizon * (1.0 + terminal)
    total += (following / (discount - terminal)) / (1.0 + discount) ** horizon
    return total


def _solve_growth(target: float, fcf0: float, terminal: float, discount: float, horizon: int) -> float:
    if target <= 0.0:
        raise CurveError("DCF price must be positive")

    def price_at(growth: float) -> float:
        return _dcf(fcf0, growth, terminal, discount, horizon)

    lo, hi = -0.5, 1.5
    for _ in range(12):
        low_price, high_price = price_at(lo), price_at(hi)
        if low_price <= target <= high_price:
            break
        if target > high_price:
            hi = hi + 1.0
        else:
            lo -= 0.25
        if lo <= -0.95 or hi > 8.0:
            raise CurveError("implied growth is outside a searchable bracket")
    else:
        if not price_at(lo) <= target <= price_at(hi):
            raise CurveError("implied growth is outside a searchable bracket")
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if price_at(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _edge_values(q: Quantity) -> list[float]:
    share = to_share(q)
    lo, hi = edges(share)
    if abs(hi - lo) <= 1e-15:
        return [lo]
    return [lo, hi]


def _decimal(q: Quantity) -> float:
    share = to_share(q)
    if share.value is not None:
        return float(share.value)
    return float(share.mid)


def _amount(q: Quantity) -> float:
    if q.value is not None:
        return float(q.value)
    return float(q.mid)
