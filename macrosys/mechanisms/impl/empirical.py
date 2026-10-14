"""Empirical and behavioral mechanisms (spec §3.2, §3.3 B)."""

from __future__ import annotations

from macrosys.core.contracts import Gate, StateView
from macrosys.core.quantity import Quantity
from macrosys.core.units import UnitError, assert_unit
from macrosys.mechanisms.arrays import (
    ArrayQuantity,
    complement_hadamard,
    hadamard,
    matvec,
)
from macrosys.mechanisms.impl._util import Ungated, entity_from_bindings, num, q, require_param
from macrosys.mechanisms.registry import mechanism

# Spec §3.4 rule 5 names ~70% utilization as the point where B03's price effect is ~0.
# applicable() receives no parameters, so the threshold lives here and is repeated as u_kink in the YAML.
_LOW_UTILIZATION = 0.70

_PATH_QUARTERS = ("q0", "q1", "q2", "q3")


def _eps_s(u: float, slack: float, tight: float, u_kink: float, u_cap: float) -> float:
    """Piecewise supply elasticity, declining from ``slack`` at ``u_kink`` to ``tight`` at ``u_cap``."""
    if u_cap <= u_kink:
        raise ValueError("B03 u_cap must be above u_kink")
    if u <= u_kink:
        return slack
    if u >= u_cap:
        return tight
    weight = (u - u_kink) / (u_cap - u_kink)
    return slack + (tight - slack) * weight


@mechanism("B01")
class DerivedDemand(Ungated):
    """Gross-output change from a provided Leontief inverse (spec §3.3 B01)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity | ArrayQuantity]:
        """dX = L dF. If ``import_share`` is present, also split domestic and imported (spec §3.3 B01)."""
        del params
        demand = inputs["d_final_demand"]
        inverse = inputs["leontief"]
        if not isinstance(demand, ArrayQuantity) or not isinstance(inverse, ArrayQuantity):
            raise TypeError("B01 d_final_demand and leontief must be ArrayQuantity")
        gross = matvec(inverse, demand)
        out: dict[str, Quantity | ArrayQuantity] = {"d_gross_output": gross}
        shares = inputs.get("import_share")
        if shares is not None:
            if not isinstance(shares, ArrayQuantity):
                raise TypeError("B01 import_share must be an ArrayQuantity")
            out["d_domestic"] = complement_hadamard(gross, shares)
            out["d_imported"] = hadamard(gross, shares)
        return out


@mechanism("B02")
class CostShare(Ungated):
    """A price change in an input moves unit cost by the cost share (spec §3.3 B02)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """d_unit_cost_pct = s_ij × d_price_pct (spec §3.3 B02). Contemporaneous cost channel."""
        del params
        return {"d_unit_cost": q(num(inputs["s_ij"]) * num(inputs["d_price"]), "pct", "B02")}


@mechanism("B03")
class BottleneckPricing:
    """Price change ≈ Δdemand / (ε_s(u) + ε_d), with ε_s declining in utilization (spec §3.3 B03)."""

    def applicable(self, state: StateView, bindings: dict[str, str]) -> Gate:
        """Scale the price effect to 0 when capacity utilization is below 70% (spec §3.4 rule 5).

        The threshold is the figure given in §3.4. ``applicable`` cannot see YAML parameters, so
        0.70 is fixed here; the piecewise elasticity kink is the ``u_kink`` parameter.
        """
        entity = entity_from_bindings(bindings)
        if entity is None or not state.has(entity, "capacity_utilization"):
            return Gate(status="on", factor=1.0, reason="capacity_utilization absent; bottleneck not scaled")
        observed = state.get(entity, "capacity_utilization")
        if observed.unit != "share":
            raise UnitError(f"capacity_utilization must be share, got {observed.unit}")
        if observed.mid < _LOW_UTILIZATION:
            return Gate(
                status="scaled",
                factor=0.0,
                reason=f"capacity_utilization {observed.mid:.2f} below {_LOW_UTILIZATION:.2f}; price effect scaled to 0",
            )
        return Gate(
            status="on",
            factor=1.0,
            reason=f"capacity_utilization {observed.mid:.2f} at or above {_LOW_UTILIZATION:.2f}",
        )

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Δp% = Δd% / (ε_s(u) + ε_d) with the declared piecewise ε_s (spec §3.3 B03).

        ε_s(u) equals ``eps_s_slack`` at and below ``u_kink``, declines linearly to ``eps_s_tight``
        at ``u_cap``, and stays there above ``u_cap``. Elasticities without a literature magnitude
        stay ``TODO_estimate`` and this method refuses to run until an override supplies them.
        """
        slack = num(require_param(params, "eps_s_slack"))
        tight = num(require_param(params, "eps_s_tight"))
        eps_d = num(require_param(params, "eps_d"))
        u_kink = num(require_param(params, "u_kink"))
        u_cap = num(require_param(params, "u_cap"))
        denom = _eps_s(num(inputs["u"]), slack, tight, u_kink, u_cap) + eps_d
        if denom == 0.0:
            raise ZeroDivisionError("B03 denominator ε_s(u)+ε_d is 0")
        return {"d_price": q(num(inputs["d_demand"]) / denom, "pct", "B03")}


@mechanism("B04")
class InventoryCycle(Ungated):
    """Days of cover and the production response to a sales change (spec §3.3 B04)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Days of cover and production = Δsales + Δ(target stock) − excess stock (spec §3.3 B04).

        ``sales`` is a quarterly flow in the same unit as the inventory stock. Days of cover use the
        quarter length in the unit registry (``q`` relative to ``d``), not a hand-set 90.
        """
        del params
        inventory, sales = num(inputs["inventory"]), num(inputs["sales"])
        target = num(inputs["target_ratio"])
        d_sales = num(inputs["d_sales"])
        if sales == 0.0:
            raise ZeroDivisionError("B04 days of cover are undefined when sales are 0")
        days_per_quarter = assert_unit("q").scale / assert_unit("d").scale
        days = (inventory / sales) * days_per_quarter
        excess = inventory - target * sales
        production = d_sales + target * d_sales - excess
        return {
            "days_of_cover": q(days, "d", "B04"),
            "production_response": q(production, "USD_bn", "B04"),
        }


@mechanism("B05")
class CapacityPipeline(Ungated):
    """Dated supply from a pipeline after a slippage share (spec §3.3 B05)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Split the pipeline into on-schedule and slipped supply at ``lead_time`` (spec §3.3 B05).

        Tightness at the scheduled date is demand minus the on-schedule portion. The slipped portion
        is not given an extra lag: the slip duration is not in the literature and is not invented.
        ``slippage`` is ``TODO_estimate`` until an override supplies it.
        """
        slip = num(require_param(params, "slippage"))
        pipeline = num(inputs["pipeline"])
        on_time = pipeline * (1.0 - slip)
        delayed = pipeline * slip
        return {
            "arrival_q": q(num(inputs["lead_time"]), "q", "B05"),
            "supply_on_schedule": q(on_time, "USD_bn", "B05"),
            "supply_delayed": q(delayed, "USD_bn", "B05"),
            "tightness": q(num(inputs["demand"]) - on_time, "USD_bn", "B05"),
        }


@mechanism("B06")
class DemandElasticity(Ungated):
    """Short-run and long-run demand response, and a partial-adjustment path (spec §3.3 B06)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity | ArrayQuantity]:
        """Demand changes and a four-quarter path from short-run toward long-run (spec §3.3 B06).

        d_sr = ε_sr × Δp, d_lr = ε_lr × Δp, then d_{k} = d_{k-1} + λ (d_lr − d_{k-1}). The four-quarter
        window is a reporting choice; the elasticities and λ are ``TODO_estimate`` until overridden.
        The path length is not an estimated lag.
        """
        eps_sr = num(require_param(params, "eps_sr"))
        eps_lr = num(require_param(params, "eps_lr"))
        speed = num(require_param(params, "speed"))
        d_price = num(inputs["d_price"])
        d_sr = eps_sr * d_price
        d_lr = eps_lr * d_price
        levels = [d_sr]
        level = d_sr
        for _ in range(len(_PATH_QUARTERS) - 1):
            level = level + speed * (d_lr - level)
            levels.append(level)
        return {
            "d_demand_sr": q(d_sr, "pct", "B06"),
            "d_demand_lr": q(d_lr, "pct", "B06"),
            "demand_path": ArrayQuantity(values=levels, unit="pct", rows=_PATH_QUARTERS),
        }


@mechanism("B09")
class Okun(Ungated):
    """Okun: unemployment change from an output deviation (spec §3.3 B09)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Δu (pp) = −κ × Δy (pct), with κ the Ball, Leigh & Loungani 2017 range (spec §3.3 B09).

        κ is dimensionless on the percent scale: 0.4 means 0.4 pp of unemployment per 1% of output.
        A range for κ is propagated by the endpoint harness, not inside this point function.
        """
        kappa = num(require_param(params, "okun"))
        return {"d_unemployment": q(-kappa * num(inputs["d_output"]), "pp", "B09")}


@mechanism("B16")
class FxPassThrough(Ungated):
    """Exchange-rate pass-through. Coefficients are local estimates, so the default record refuses (spec §3.3 B16)."""

    def apply(self, inputs: dict[str, Quantity], params: dict[str, Quantity]) -> dict[str, Quantity]:
        """Import-price and CPI changes once pass-through coefficients are supplied (spec §3.3 B16).

        The spec gives the sign and the timing, not a magnitude, so ``pt_import`` and ``pt_cpi`` are
        ``TODO_estimate`` and this method raises ``MissingParameterError`` until an override with
        provenance replaces them. The linear form, when it runs, is
        Δp = pt × invoicing_share × Δe. That form is a [J] placeholder for the missing estimate.
        """
        pt_import = num(require_param(params, "pt_import"))
        pt_cpi = num(require_param(params, "pt_cpi"))
        scaled = num(inputs["invoicing_share"]) * num(inputs["d_fx"])
        return {
            "d_import_price": q(pt_import * scaled, "pct", "B16"),
            "d_cpi": q(pt_cpi * scaled, "pct", "B16"),
        }
