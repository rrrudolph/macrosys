"""Remaining empirical mechanisms from spec §3.3 B. Coefficients without a literature range refuse to run."""

from __future__ import annotations

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.impl._util import Ungated, require_param
from macrosys.mechanisms.registry import mechanism


def _neg(qty: Quantity, provenance: str) -> Quantity:
    return qty.scale(-1, provenance=provenance)


@mechanism("B07")
class Substitution(Ungated):
    """Quantity reallocation when a relative price moves (spec §3.3 B07).

    Thesis step. Short-run substitution for a specific input is near zero; the elasticity is
    ``TODO_estimate``. Complements amplify rather than relieve a bottleneck.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_quantity_pct = −σ × relative price change (spec §3.3 B07)."""
        moved = require_param(params, "sigma").mul(inputs["d_relative_price"], "pct", "B07")
        return {"d_quantity": _neg(moved, "B07")}


@mechanism("B08")
class PassThrough(Ungated):
    """Share of a cost change retained at a stage, and the share passed on (spec §3.3 B08).

    Thesis step. This is where a growing-industry story dies if the stage does not keep the margin.
    The capture share is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Retained and passed-on cost change from a capture share (spec §3.3 B08)."""
        capture = require_param(params, "capture")
        retained = capture.mul(inputs["d_cost"], "pct", "B08")
        passed_share = Quantity(value=1.0, unit="share", provenance="B08").add(capture.scale(-1))
        return {
            "retained": retained,
            "passed_on": passed_share.mul(inputs["d_cost"], "pct", "B08"),
        }


@mechanism("B10")
class Phillips(Ungated):
    """Wage and core pressure from slack, expectations, and a supply shock (spec §3.3 B10).

    Input to a policy reaction, not a standalone inflation forecast. ``slope_wage`` is
    the local-projection range on the card. ``slope_core`` is still ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """expectations − slope × unemployment gap + supply shock, for wages and for core (spec §3.3 B10)."""
        gap = inputs["unemployment_gap"]
        base = inputs["inflation_expectations"].add(inputs["supply_shock"], provenance="B10")
        wage_slope = require_param(params, "slope_wage").mul(gap, "pct", "B10")
        core_slope = require_param(params, "slope_core").mul(gap, "pct", "B10")
        return {
            "d_wage": base.add(wage_slope.scale(-1), provenance="B10"),
            "d_core": base.add(core_slope.scale(-1), provenance="B10"),
        }


@mechanism("B11")
class MonetaryTransmission(Ungated):
    """Demand response to a policy-rate change (spec §3.3 B11).

    Thesis step for a policy path, with the sign able to flip in the short run when the
    announcement is news about the economy (Nakamura and Steinsson). The multiplier is
    ``TODO_estimate``; Ramey 2016 surveys identification and does not give one number.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_demand_pct = −multiplier × policy-rate change (spec §3.3 B11)."""
        raw = require_param(params, "multiplier").mul(inputs["d_policy"], "pct", "B11")
        return {"d_demand": _neg(raw, "B11")}


@mechanism("B12")
class BankCredit(Ungated):
    """Credit growth when lending standards tighten (spec §3.3 B12).

    Thesis step from bank health to spending, with a lag of a few quarters. The elasticity
    is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_credit_pct = −elasticity × standards tightening (spec §3.3 B12)."""
        raw = require_param(params, "elasticity").mul(inputs["standards"], "pct", "B12")
        return {"d_credit": _neg(raw, "B12")}


@mechanism("B13")
class FinancialAccelerator(Ungated):
    """Borrowing-capacity change from an asset-price move, scaled by leverage (spec §3.3 B13).

    Thesis step. Amplification is larger when leverage is high. The elasticity is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_borrowing_pct = elasticity × leverage × asset-price change (spec §3.3 B13)."""
        scaled = require_param(params, "elasticity").mul(inputs["leverage"], "ratio", "B13")
        return {"d_borrowing": scaled.mul(inputs["d_asset_price"], "pct", "B13")}


@mechanism("B14")
class Housing(Ungated):
    """Residential-investment response to a permits change (spec §3.3 B14).

    Thesis step from housing into the expenditure identity and into materials demand. The
    elasticity is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_residential_pct = elasticity × permits change (spec §3.3 B14)."""
        return {"d_residential": require_param(params, "elasticity").mul(inputs["d_permits"], "pct", "B14")}


@mechanism("B15")
class FiscalMultiplier(Ungated):
    """Demand change from a fiscal impulse, using the literature cluster 0.5–1.5 (spec §3.3 B15).

    Thesis step. The range is Ramey 2019 (state dependence also Auerbach and Gorodnichenko 2012);
    it is not a point. Endpoint propagation turns the range into a demand range.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_demand_pct = multiplier × fiscal change, multiplier in [0.5, 1.5] (spec §3.3 B15)."""
        return {"d_demand": require_param(params, "multiplier").mul(inputs["d_fiscal"], "pct", "B15")}


@mechanism("B17")
class TermsOfTrade(Ungated):
    """Income transfer from a terms-of-trade change times the trade share (spec §3.3 B17).

    Thesis step for a commodity exporter: the income effect is the product in the spec, not an
    estimated elasticity.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_income_pct = ΔToT_pct × trade share of GDP (spec §3.3 B17)."""
        del params
        return {"d_income": inputs["d_tot"].mul(inputs["trade_share"], "pct", "B17")}


@mechanism("B18")
class GlobalFinancialCycle(Ungated):
    """Cross-border credit response to a broad dollar move (spec §3.3 B18).

    Thesis step and a risk input. Dollar appreciation tightens dollar credit. The elasticity
    is ``TODO_estimate``; the spec gives the sign, not a coefficient.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_credit_pct = −elasticity × broad-dollar change (spec §3.3 B18)."""
        raw = require_param(params, "elasticity").mul(inputs["d_usd"], "pct", "B18")
        return {"d_credit": _neg(raw, "B18")}


@mechanism("B19")
class Carry(Ungated):
    """Quiet-period carry excess return, and a skewed loss when risk-off is on (spec §3.3 B19).

    Risk input, not a thesis. The spec describes a return distribution. UIP's failure is why the
    differential is not a depreciation forecast (C02). Both coefficients are ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Excess return = carry coefficient × differential; skewed loss applies only if risk-off (spec §3.3 B19)."""
        differential = inputs["interest_differential"]
        excess = require_param(params, "carry_coeff").mul(differential, "share", "B19")
        loss = require_param(params, "crash_coeff").mul(differential, "share", "B19").mul(
            inputs["risk_off"], "share", "B19"
        )
        return {"excess_return": excess, "skewed_loss": _neg(loss, "B19")}


@mechanism("B20")
class Momentum(Ungated):
    """Continuation at an intermediate horizon and reversal at a long horizon (spec §3.3 B20).

    Pricing-gap timing and a risk input: is the market already moving toward the thesis. It is
    not the thesis. Both weights are ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """signal = continuation × intermediate return − reversal × long-horizon return (spec §3.3 B20)."""
        cont = require_param(params, "continuation").mul(inputs["r_intermediate"], "pct", "B20")
        rev = require_param(params, "reversal").mul(inputs["r_long"], "pct", "B20")
        return {"signal": cont.add(rev.scale(-1), provenance="B20")}


@mechanism("B21")
class Positioning(Ungated):
    """Map a positioning percentile to an asymmetry multiplier (spec §3.3 B21).

    Risk input. Crowded positioning raises the asymmetry of the reaction to news. The multiplier
    per unit of distance from the median is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """asymmetry = multiplier × (percentile − 0.5) (spec §3.3 B21)."""
        gap = inputs["positioning_percentile"].add(Quantity(value=-0.5, unit="share", provenance="B21"), provenance="B21")
        return {"asymmetry": require_param(params, "multiplier").mul(gap, "ratio", "B21")}


@mechanism("B22")
class IntermediaryCapacity(Ungated):
    """Risk-premium widening when intermediary capital is scarce (spec §3.3 B22).

    Risk input. It explains why a pricing relation can drift and when it will not snap back.
    The elasticity is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_premium_bp = elasticity × capital scarcity (spec §3.3 B22)."""
        return {"d_premium": require_param(params, "elasticity").mul(inputs["capital_scarcity"], "bp", "B22")}


@mechanism("B23")
class ExpectationsFormation(Ungated):
    """Revision scaled by an overreaction coefficient (spec §3.3 B23).

    Pricing-gap input. It tempers "what is priced": consensus is not the market, and long-horizon
    macro forecasts overreact. The coefficient is ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """revision_pct = overreaction × surprise (spec §3.3 B23)."""
        return {"revision": require_param(params, "overreaction").mul(inputs["surprise"], "pct", "B23")}


@mechanism("B24")
class SupplyChainPropagation(Ungated):
    """Customer output loss after a supplier disruption (spec §3.3 B24).

    Thesis step. Loss is larger for specific inputs and thinner inventories. Both coefficients
    are ``TODO_estimate``; the spec gives the signs, not magnitudes.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """loss = elasticity × disruption × specificity − cushion × inventory days (spec §3.3 B24)."""
        gross = require_param(params, "loss_coeff").mul(inputs["disruption"], "pct", "B24").mul(
            inputs["specificity"], "pct", "B24"
        )
        buffer = require_param(params, "cushion").mul(inputs["inventory_days"], "pct", "B24")
        return {"output_loss": gross.add(buffer.scale(-1), provenance="B24")}


@mechanism("B25")
class TransportDisruption(Ungated):
    """Delivered cost, and a freight change that rises with utilization (spec §3.3 B25).

    Thesis step for a chokepoint. Delivered cost is FOB + freight + insurance. The utilization
    elasticity of freight is ``TODO_estimate``; convexity is the same idea as B03, without a number.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """delivered = FOB + freight + insurance; d_freight_pct = elasticity × utilization (spec §3.3 B25)."""
        delivered = inputs["fob"].add(inputs["freight"]).add(inputs["insurance"], provenance="B25")
        d_freight = require_param(params, "freight_elasticity").mul(inputs["utilization_pct"], "pct", "B25")
        return {"delivered_cost": delivered, "d_freight": d_freight}


@mechanism("B26")
class Productivity(Ungated):
    """Unit-labor-cost decline from TFP, and the part that shows up in prices (spec §3.3 B26).

    Thesis step. Lower unit requirements are not the same thing as lower prices; the price
    pass-through is ``TODO_estimate`` and is where B08 decides who keeps the margin.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_ulc = −ΔTFP; d_price = −pass-through × ΔTFP (spec §3.3 B26)."""
        d_ulc = _neg(inputs["d_tfp"], "B26")
        d_price = _neg(require_param(params, "pass_through").mul(inputs["d_tfp"], "pct", "B26"), "B26")
        return {"d_ulc": d_ulc, "d_price": d_price}


@mechanism("B27")
class FundingSqueeze(Ungated):
    """Spread widening when funding stress rises (spec §3.3 B27).

    Thesis step for a funding shock and a risk input: liquid assets are sold first and
    correlations rise. The pass-through from funding stress into the asset spread is
    ``TODO_estimate``.
    """

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_spread_bp = elasticity × funding stress (spec §3.3 B27)."""
        return {"d_spread": require_param(params, "elasticity").mul(inputs["funding_stress"], "bp", "B27")}
