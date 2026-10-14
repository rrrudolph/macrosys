"""Market-pricing and no-arbitrage relations (spec §3.2, §3.3 C)."""

from __future__ import annotations

import math

from macrosys.core.quantity import Quantity
from macrosys.core.units import convert
from macrosys.mechanisms.impl._util import Ungated, num, q
from macrosys.mechanisms.registry import mechanism


@mechanism("C01")
class CipBasis(Ungated):
    """Covered-interest forward with the cross-currency basis (spec §3.3 C01)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """F = S·(1+i_d·τ)/(1+(i_f+basis)·τ), basis converted from bp (spec §3.3 C01).

        Rates are decimal shares. Basis is added to the foreign rate, the Du, Tepper & Verdelhan
        convention: a negative basis raises the forward. The spot unit is preserved on the forward.
        """
        del params
        tau = num(inputs["tau"])
        basis = convert(num(inputs["basis"]), "bp", "pp") / 100.0
        i_d, i_f = num(inputs["i_d"]), num(inputs["i_f"])
        denom = 1.0 + (i_f + basis) * tau
        if denom == 0.0:
            raise ZeroDivisionError("C01 denominator is 0")
        forward = num(inputs["spot"]) * (1.0 + i_d * tau) / denom
        return {"forward": q(forward, inputs["spot"].unit, "C01")}


@mechanism("C03")
class ExpectationsHypothesis(Ungated):
    """Yield = average expected short rate + term premium (spec §3.3 C03)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Solve the expectations hypothesis for the implied term premium (spec §3.3 C03)."""
        del params
        premium = num(inputs["yield_n"]) - num(inputs["expected_short_avg"])
        return {"term_premium": q(premium, "pct", "C03")}


@mechanism("C07")
class CostOfCarry(Ungated):
    """Futures roll yield and the convenience yield implied by the curve (spec §3.3 C07)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Roll yield ln(S/F)/T and convenience yield r + storage + roll yield (spec §3.3 C07).

        The fair forward is F = S·e^{(r+storage−convenience)T}. Rearranged, the convenience yield
        implied by an observed curve is r + storage − ln(F/S)/T, which equals r + storage + roll yield.
        """
        del params
        tenor = num(inputs["tenor"])
        if tenor == 0.0:
            raise ZeroDivisionError("C07 tenor is 0")
        spot, futures = num(inputs["spot"]), num(inputs["futures"])
        if spot <= 0.0 or futures <= 0.0:
            raise ValueError("C07 spot and futures must be positive")
        roll = math.log(spot / futures) / tenor
        convenience = num(inputs["rate"]) + num(inputs["storage"]) + roll
        return {
            "roll_yield": q(roll, "share", "C07"),
            "implied_convenience": q(convenience, "share", "C07"),
        }


@mechanism("C12")
class Fisher(Ungated):
    """Fisher relation i ≈ r + π^e (spec §3.3 C12)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Real rate implied by the nominal rate and expected inflation (spec §3.3 C12)."""
        del params
        real = num(inputs["nominal"]) - num(inputs["expected_inflation"])
        return {"real_rate": q(real, "pct", "C12")}
