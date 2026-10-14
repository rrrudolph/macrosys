"""Breakeven inflation and the premium band around it (spec §3.3 C04, §7.3).

The identity is the C04 form ``expected = breakeven − inflation risk premium + TIPS
liquidity premium``. A missing premium is the ``TODO_estimate`` band in ``params.yaml``.
That band widens the result and does not replace the breakeven with a silent zero.
"""

from __future__ import annotations

from macrosys.core.quantity import Quantity
from macrosys.pricing._interval import add, provenance_code, sub, to_pct
from macrosys.pricing._params import band
from macrosys.pricing.curves import ZeroCurve, to_semi_annual_pct
from macrosys.pricing.types import PathWithRange


def breakeven(nominal_zero: ZeroCurve, real_zero: ZeroCurve, tenor: float) -> Quantity:
    """Zero-coupon breakeven at ``tenor`` years, semi-annually compounded percent (spec §3.3 C04).

    Each curve is read in its own compounding and converted to a semi-annual percent
    before the subtraction, so a continuous GSW nominal zero can be set against a
    semi-annual real zero. The result is ``nominal − real`` at that tenor. It is the
    raw breakeven, before premia.
    """
    nominal = to_semi_annual_pct(_rate(nominal_zero, tenor), nominal_zero.compounding)
    real = to_semi_annual_pct(_rate(real_zero, tenor), real_zero.compounding)
    gap = sub(
        Quantity(value=nominal, unit="pct", provenance=nominal_zero.source),
        Quantity(value=real, unit="pct", provenance=real_zero.source),
        provenance="C04",
    )
    return gap


def decompose_breakeven(
    be: Quantity,
    inflation_risk_premium: Quantity | None = None,
    tips_liquidity_premium: Quantity | None = None,
    horizon_q: float = 0.0,
) -> PathWithRange:
    """Expected inflation as ``breakeven − irp + liquidity``, one path point (spec §3.3 C04).

    ``be`` is a breakeven in percent (or a share, which is scaled). A premium left as
    ``None`` is the declared ``TODO_estimate`` band. Those bands contain zero, so the
    point stays on the raw breakeven and the range widens by both premia. A premium
    that has its own point value is applied, matching the C04 example
    ``2.5 − 0.2 + 0.1 = 2.4``. ``horizon_q`` is the point's horizon in quarters; pass
    ``tenor years × 4`` when the tenor is known. Zero is not written in for a missing
    premium.
    """
    level = to_pct(be)
    irp = inflation_risk_premium if inflation_risk_premium is not None else band("c04_inflation_risk_premium_pct")
    liquidity = tips_liquidity_premium if tips_liquidity_premium is not None else band("c04_tips_liquidity_premium_pct")
    irp = to_pct(irp)
    liquidity = to_pct(liquidity)
    code = provenance_code("C04", level, irp, liquidity)
    if level.provenance and level.provenance not in code:
        code = f"{level.provenance}|{code}"
    # A valueless premium widens and does not move the point already in hand.
    without_irp = sub(level, irp, provenance=code, unshifted=irp.value is None)
    expected = add(without_irp, liquidity, provenance=code, unshifted=liquidity.value is None)
    return [(horizon_q, expected)]


def _rate(curve: ZeroCurve, tenor: float) -> float:
    rate = curve.zero_at(tenor)
    if rate.value is not None:
        return float(rate.value)
    return float(rate.mid)
