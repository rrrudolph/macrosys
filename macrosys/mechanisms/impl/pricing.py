"""Market-pricing and no-arbitrage relations (spec §3.2, §3.3 C)."""

from __future__ import annotations

import math

from macrosys.core.quantity import Quantity
from macrosys.core.units import convert
from macrosys.mechanisms.impl._util import Ungated, num, q, require_param
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
        basis = q(convert(num(inputs["basis"]), "bp", "pp") / 100.0, "share", "C01")
        one = q(1.0, "share", "C01")
        numer = one.add(inputs["i_d"].mul(inputs["tau"], "share", "C01"), provenance="C01")
        denom = one.add(inputs["i_f"].add(basis).mul(inputs["tau"], "share", "C01"), provenance="C01")
        if num(denom) == 0.0:
            raise ZeroDivisionError("C01 denominator is 0")
        factor = q(num(numer) / num(denom), "share", "C01")
        return {"forward": inputs["spot"].mul(factor, inputs["spot"].unit, "C01")}


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
        roll = q(math.log(spot / futures) / tenor, "share", "C07")
        convenience = inputs["rate"].add(inputs["storage"]).add(roll, provenance="C07")
        return {"roll_yield": roll, "implied_convenience": convenience}


@mechanism("C12")
class Fisher(Ungated):
    """Fisher relation i ≈ r + π^e (spec §3.3 C12)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Real rate implied by the nominal rate and expected inflation (spec §3.3 C12)."""
        del params
        real = inputs["nominal"].add(inputs["expected_inflation"].scale(-1), provenance="C12")
        return {"real_rate": real}


@mechanism("C02")
class UncoveredInterest(Ungated):
    """UIP-implied depreciation, flagged as not a forecast (spec §3.3 C02).

    Risk input for carry (B19), not a depreciation forecast. The hypothesis is E[Δs] = i_d − i_f;
    empirically high-rate currencies do not depreciate on average over short horizons. ``use_as_forecast``
    is identically zero so a path cannot treat the gap as an expected spot move.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """uip_depreciation = i_d − i_f, and use_as_forecast = 0 (spec §3.3 C02)."""
        del params
        gap = inputs["i_d"].add(inputs["i_f"].scale(-1), provenance="C02")
        return {"uip_depreciation": gap, "use_as_forecast": q(0.0, "share", "C02")}


@mechanism("C04")
class Breakeven(Ungated):
    """Breakeven inflation and the same gap net of model premia (spec §3.3 C04).

    Pricing-gap input for an inflation thesis: the hurdle is the breakeven, not the latest CPI print.
    The inflation risk premium and the TIPS liquidity premium are ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """breakeven = nominal − real; ex-premia subtracts the risk premium and adds the liquidity premium (spec §3.3 C04)."""
        breakeven = inputs["nominal"].add(inputs["real_yield"].scale(-1), provenance="C04")
        adjusted = breakeven.add(require_param(params, "irp").scale(-1)).add(
            require_param(params, "liquidity"), provenance="C04"
        )
        return {"breakeven": breakeven, "inflation_ex_premia": adjusted}


@mechanism("C05")
class EquityDuration(Ungated):
    """Approximate price change from a discount-rate move (spec §3.3 C05).

    Pricing-gap input. An earnings thesis has to be sized against the discount-rate move the same
    shock causes. Duration is ``TODO_estimate``; the spec says price-ratio variation is dominated
    by discount rates and does not give a duration.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_price_pct = −duration × discount-rate change (spec §3.3 C05)."""
        raw = require_param(params, "duration").mul(inputs["d_discount"], "pct", "C05")
        return {"d_price": raw.scale(-1, provenance="C05")}


@mechanism("C06")
class CreditSpread(Ungated):
    """Split an option-adjusted spread into expected loss, risk premium, and liquidity (spec §3.3 C06).

    Pricing-gap input. A default thesis competes with the expected loss already in the spread.
    The risk-premium residual is what remains after the two other inputs.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """risk_premium = OAS − expected loss − liquidity premium (spec §3.3 C06)."""
        del params
        premium = inputs["oas"].add(inputs["expected_loss"].scale(-1)).add(inputs["liquidity"].scale(-1), provenance="C06")
        return {"risk_premium": premium}


@mechanism("C08")
class VariancePremium(Ungated):
    """Implied variance minus expected realized variance (spec §3.3 C08).

    Risk input and a pricing-gap input. A thesis that volatility will rise competes with a premium
    already paid. The premium is the residual of the two inputs, not a hardcoded positive constant.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """variance_risk_premium = implied variance − expected realized variance (spec §3.3 C08)."""
        del params
        premium = inputs["implied_variance"].add(inputs["expected_realized"].scale(-1), provenance="C08")
        return {"variance_risk_premium": premium}


@mechanism("C09")
class PolicyPath(Ungated):
    """Futures-implied policy rate minus a risk premium (spec §3.3 C09).

    Pricing-gap input. A policy-reaction view is tradable only as a deviation from this path.
    The premium is described as small and is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """policy_rate = futures rate − risk premium (spec §3.3 C09)."""
        rate = inputs["futures_rate"].add(require_param(params, "risk_premium").scale(-1), provenance="C09")
        return {"policy_rate": rate}


@mechanism("C10")
class EtfPremium(Ungated):
    """ETF price premium or discount to NAV (spec §3.3 C10).

    Execution-quality guard and a data-alignment check, not a thesis. Persistent premium or discount
    is illiquidity, a market-hours mismatch, or a creation halt.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """premium = (price − NAV) / NAV (spec §3.3 C10)."""
        del params
        nav = num(inputs["nav"])
        if nav == 0.0:
            raise ZeroDivisionError("C10 premium is undefined when NAV is 0")
        gap = inputs["price"].add(inputs["nav"].scale(-1), provenance="C10")
        return {"premium": q(num(gap) / nav, "share", "C10")}


@mechanism("C11")
class PurchasingPowerParity(Ungated):
    """Partial closure of a real-exchange-rate gap over a stated horizon (spec §3.3 C11).

    Valuation input for multi-year FX exposure, not a weekly timing signal. The half-life is
    ``TODO_estimate``; Rogoff 1996 says several years and does not give a number used here.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """expected_change = −(1 − 0.5^(horizon/half_life)) × real gap (spec §3.3 C11)."""
        half = num(require_param(params, "half_life"))
        if half == 0.0:
            raise ZeroDivisionError("C11 half-life is 0")
        frac = 1.0 - 0.5 ** (num(inputs["horizon_y"]) / half)
        return {"expected_change": inputs["real_gap"].scale(-frac, provenance="C11")}


@mechanism("C13")
class EquityRiskPremium(Ungated):
    """Earnings yield minus the real yield (spec §3.3 C13).

    Pricing-gap input for a cross-asset comparison of what is priced. The spec says every ERP
    approximation is model-dependent and is carried as a range, never treated as a point forecast.
    This record is the earnings-yield gap; pass ranges in when the method is uncertain.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """erp = earnings yield − real yield (spec §3.3 C13)."""
        del params
        erp = inputs["earnings_yield"].add(inputs["real_yield"].scale(-1), provenance="C13")
        return {"erp": erp}
