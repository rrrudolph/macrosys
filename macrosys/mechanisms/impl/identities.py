"""Accounting-identity implementations. They return a residual; they do not explain it (spec §3.2, §3.3 A)."""

from __future__ import annotations

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.impl._util import Ungated, num, q
from macrosys.mechanisms.registry import mechanism


@mechanism("A01")
class ExpenditureIdentity(Ungated):
    """Expenditure identity Y = C + I + G + X − M, plus import leakage M/(C+I+G) (spec §3.3 A01)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Residual of the expenditure identity and the import-leakage share (spec §3.3 A01).

        Absorption is C+I+G. The residual is Y − (C+I+G+X−M). Tolerance is enforced by the
        propagator after the run (spec §3.4 rule 7), not inside this pure function, so a sign test
        can perturb one component without the check throwing.
        """
        del params
        c, i, g = num(inputs["C"]), num(inputs["I"]), num(inputs["G"])
        x, m, y = num(inputs["X"]), num(inputs["M"]), num(inputs["Y"])
        residual = y - (c + i + g + x - m)
        absorption = c + i + g
        if absorption == 0.0:
            raise ZeroDivisionError("A01 import leakage is undefined when C+I+G is 0")
        return {
            "residual": q(residual, "USD_bn", "A01"),
            "import_leakage": q(m / absorption, "share", "A01"),
        }


@mechanism("A04")
class SupplyUse(Ungated):
    """Supply–use balance for one product (spec §3.3 A04)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Residual of domestic output + imports against the use side (spec §3.3 A04)."""
        del params
        supply = num(inputs["domestic_output"]) + num(inputs["imports"])
        use = (
            num(inputs["intermediate_use"])
            + num(inputs["household"])
            + num(inputs["government"])
            + num(inputs["investment"])
            + num(inputs["d_inventories"])
            + num(inputs["exports"])
        )
        return {
            "total_supply": q(supply, "USD_bn", "A04"),
            "total_use": q(use, "USD_bn", "A04"),
            "residual": q(supply - use, "USD_bn", "A04"),
        }


@mechanism("A05")
class FirmCashFlow(Ungated):
    """Firm cash-flow identity from volume, price, and the cash items (spec §3.3 A05)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Revenue = volume × price, then EBIT and FCF, with a residual against the reported figures.

        Spec §3.3 A05. ``volume`` is an index and ``price`` is in the same currency unit as revenue,
        so the product is that currency unit. ``Quantity`` has no unit-checked product; the product
        is formed from the floats and re-wrapped.
        """
        del params
        revenue = num(inputs["volume"]) * num(inputs["price"])
        ebit = revenue - num(inputs["variable_costs"]) - num(inputs["fixed_costs"])
        fcf = ebit * (1.0 - num(inputs["tax_rate"])) + num(inputs["da"]) - num(inputs["capex"]) - num(inputs["dnwc"])
        gap = (num(inputs["revenue_reported"]) - revenue) + (num(inputs["fcf_reported"]) - fcf)
        return {
            "revenue": q(revenue, "USD_bn", "A05"),
            "ebit": q(ebit, "USD_bn", "A05"),
            "fcf": q(fcf, "USD_bn", "A05"),
            "residual": q(gap, "USD_bn", "A05"),
        }


@mechanism("A06")
class DebtDynamics(Ungated):
    """Government debt dynamics Δd = ((r−g)/(1+g))·d₋₁ − pb + sfa (spec §3.3 A06)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Compute the debt-ratio change and the residual against the observed change (spec §3.3 A06).

        Rates and ratios are shares (0.03 = 3 percent), which is the form the identity is written in.
        """
        del params
        g = num(inputs["g"])
        if g == -1.0:
            raise ZeroDivisionError("A06 is undefined when 1+g is 0")
        delta = ((num(inputs["r"]) - g) / (1.0 + g)) * num(inputs["d_lag"]) - num(inputs["pb"]) + num(inputs["sfa"])
        return {
            "delta_d": q(delta, "share", "A06"),
            "residual": q(num(inputs["delta_d_observed"]) - delta, "share", "A06"),
        }


@mechanism("A09")
class EtfReturn(Ungated):
    """Base-currency ETF return = local + fx + local·fx − fees (spec §3.3 A09)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """One-holding base-currency return and the residual against an observed return (spec §3.3 A09)."""
        del params
        local, fx = num(inputs["r_local"]), num(inputs["r_fx"])
        r_base = local + fx + local * fx - num(inputs["fees"])
        return {
            "r_base": q(r_base, "share", "A09"),
            "residual": q(num(inputs["r_observed"]) - r_base, "share", "A09"),
        }


@mechanism("A10")
class RealExchangeRate(Ungated):
    """Real exchange rate in logs, q = e + p* − p (spec §3.3 A10)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Log real exchange rate and the residual against an observed q (spec §3.3 A10).

        ``e`` is domestic currency per foreign currency, in logs, as is each price level.
        """
        del params
        q_log = num(inputs["e"]) + num(inputs["p_foreign"]) - num(inputs["p_domestic"])
        return {
            "q": q(q_log, "index", "A10"),
            "residual": q(num(inputs["q_observed"]) - q_log, "index", "A10"),
        }


@mechanism("A11")
class InventoryProduction(Ungated):
    """Production = sales + Δinventories (spec §3.3 A11)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Implied production and the residual against observed production (spec §3.3 A11)."""
        del params
        implied = num(inputs["sales"]) + num(inputs["d_inventories"])
        return {
            "implied_production": q(implied, "t", "A11"),
            "residual": q(num(inputs["production_observed"]) - implied, "t", "A11"),
        }
