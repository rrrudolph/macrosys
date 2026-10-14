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

        Spec §3.3 A05. ``volume`` is an index and ``price`` is currency, and the product is declared
        as that currency unit at the ``mul`` call.
        """
        del params
        revenue = inputs["volume"].mul(inputs["price"], "USD_bn", "A05")
        ebit = revenue.add(inputs["variable_costs"].scale(-1)).add(inputs["fixed_costs"].scale(-1), provenance="A05")
        fcf = (
            ebit.scale(1.0 - num(inputs["tax_rate"]))
            .add(inputs["da"])
            .add(inputs["capex"].scale(-1))
            .add(inputs["dnwc"].scale(-1), provenance="A05")
        )
        gap = inputs["revenue_reported"].add(revenue.scale(-1)).add(
            inputs["fcf_reported"].add(fcf.scale(-1)), provenance="A05"
        )
        return {"revenue": revenue, "ebit": ebit, "fcf": fcf, "residual": gap}


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
        coeff = q((num(inputs["r"]) - g) / (1.0 + g), "share", "A06")
        delta = coeff.mul(inputs["d_lag"], "share", "A06").add(inputs["pb"].scale(-1)).add(inputs["sfa"], provenance="A06")
        return {
            "delta_d": delta,
            "residual": inputs["delta_d_observed"].add(delta.scale(-1), provenance="A06"),
        }


@mechanism("A09")
class EtfReturn(Ungated):
    """Base-currency ETF return = local + fx + local·fx − fees (spec §3.3 A09)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """One-holding base-currency return and the residual against an observed return (spec §3.3 A09)."""
        del params
        cross = inputs["r_local"].mul(inputs["r_fx"], "share", "A09")
        r_base = inputs["r_local"].add(inputs["r_fx"]).add(cross).add(inputs["fees"].scale(-1), provenance="A09")
        return {
            "r_base": r_base,
            "residual": inputs["r_observed"].add(r_base.scale(-1), provenance="A09"),
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
        implied = inputs["sales"].add(inputs["d_inventories"], provenance="A11")
        return {
            "implied_production": implied,
            "residual": inputs["production_observed"].add(implied.scale(-1), provenance="A11"),
        }


@mechanism("A02")
class SectoralBalances(Ungated):
    """Private, public, and external balances sum to zero (spec §3.3 A02)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Residual of (S−I) + (T−G) + (M−X + net primary income) (spec §3.3 A02).

        A fiscal shift that does not show up in the private balance or the external balance fails
        this check. It is not a forecast of who will dis-save.
        """
        del params
        private = inputs["s_private"].add(inputs["i_private"].scale(-1), provenance="A02")
        public = inputs["taxes"].add(inputs["government"].scale(-1), provenance="A02")
        external = inputs["imports"].add(inputs["exports"].scale(-1)).add(inputs["net_primary_income"], provenance="A02")
        return {"residual": private.add(public).add(external, provenance="A02")}


@mechanism("A03")
class BalanceOfPayments(Ungated):
    """Balance of payments and the IIP change identity (spec §3.3 A03)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """CA + KA + FA + E&O = 0 and ΔIIP = FA + valuation (spec §3.3 A03).

        ``residual`` is the sum of the two gaps, so either identity failing trips the tolerance.
        Gross versus net positions are a use of this check, not an extra estimated term.
        """
        del params
        bop = inputs["ca"].add(inputs["ka"]).add(inputs["fa"]).add(inputs["eo"], provenance="A03")
        iip = inputs["diip"].add(inputs["fa"].add(inputs["valuation"]).scale(-1), provenance="A03")
        return {
            "bop_gap": bop,
            "iip_gap": iip,
            "residual": bop.add(iip, provenance="A03"),
        }


@mechanism("A07")
class CentralBankBalanceSheet(Ungated):
    """Central-bank assets equal the liability side (spec §3.3 A07)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Assets − (reserves + currency + government deposits + other) (spec §3.3 A07).

        ``reserve_supply`` is the reserve stock the identity is built from. It is the input to
        funding-squeeze and intervention questions, not a forecast.
        """
        del params
        liabilities = (
            inputs["reserves"].add(inputs["currency"]).add(inputs["gov_deposits"]).add(inputs["other"], provenance="A07")
        )
        return {
            "reserve_supply": inputs["reserves"],
            "residual": inputs["assets"].add(liabilities.scale(-1), provenance="A07"),
        }


@mechanism("A08")
class BankBalanceSheet(Ungated):
    """Bank assets equal deposits, wholesale funding, and equity (spec §3.3 A08)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """(Loans + securities + reserves + other) − (deposits + wholesale + equity) (spec §3.3 A08).

        The residual is the check. Credit-growth stories that do not fit this balance are data
        errors, not a funding mix.
        """
        del params
        assets = inputs["loans"].add(inputs["securities"]).add(inputs["reserves"]).add(inputs["other"], provenance="A08")
        funding = inputs["deposits"].add(inputs["wholesale"]).add(inputs["equity"], provenance="A08")
        return {"residual": assets.add(funding.scale(-1), provenance="A08")}
