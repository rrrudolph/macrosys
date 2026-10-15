"""Default first-step paths, keyed by shock type and subtype (spec §4.1, §13 S0.2).

Only this module consults ``shock.type`` and ``shock.subtype``. The demo shocks differ by the
``Shock`` record and by the binding this table returns. Path construction does not special-case
a shock id.

B03's elasticities are still ``TODO_estimate`` in the library. The overrides on the demand and
supply routes are the example scenario already written in ``B03_bottleneck_pricing.yaml``,
relabelled ``stub:S0.1``. They are not estimates. B16 is left without overrides so it refuses
to run (spec §3.1). [J]

Triggered shocks select a pair. ``price`` / ``fx`` is origin-aware: a USD currency origin is the
broad-dollar route, and any other currency keeps the EM pass-through route. Demand and supply
subtypes with no dedicated pair keep the skeleton route for that type.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.decide.shock import Shock
from macrosys.mechanisms import (
    AcceptedCompeting,
    MechanismInstance,
    PropagationPath,
    UnitConverter,
    Wire,
)
from macrosys.mechanisms.library import Library
from macrosys.skeleton.stubs import STUB_PROVENANCE, UTIL_ENTITY

_B03_BINDINGS = {"entity_type": UTIL_ENTITY.entity_type, "entity_id": UTIL_ENTITY.entity_id}


def _q(value: float, unit: str) -> Quantity:
    return Quantity(value=value, unit=unit, provenance=STUB_PROVENANCE)


def _usd(value: float) -> Quantity:
    return _q(value, "USD_bn")


def _accept(mechanism: str, observable: str) -> AcceptedCompeting:
    return AcceptedCompeting(
        mechanism=mechanism,
        observable=observable,
        threshold=_q(0.01, "share"),
    )


def _level_to_pct() -> UnitConverter:
    """Stub bridge from a USD billion flow to a percent change (spec §3.4 rule 1).

    The coefficient is not an estimated elasticity. It only lets the skeleton compose a path
    whose ports do not share a unit. Provenance is ``stub:S0.1``.
    """
    return UnitConverter(
        from_unit="USD_bn",
        to_unit="pct",
        coefficient=_q(0.04, "pct"),
    )


def _b03_overrides() -> dict[str, Quantity]:
    """Example-scenario elasticities from the B03 record, labelled as stubs (spec §3.1)."""
    return {
        "eps_s_slack": _q(2.0, "ratio"),
        "eps_s_tight": _q(0.0, "ratio"),
        "eps_d": _q(1.0, "ratio"),
        "u_cap": _q(0.9, "share"),
    }


def _supply_use_fixed(*, shocked: str) -> dict[str, Quantity]:
    """Balanced supply-use levels from the A04 example, minus the input the shock replaces.

    With the shocked input at the omitted level (investment 15, or inventory change 5, USD bn),
    the identity residual is zero. Those levels are stubs, not a measured use table.
    """
    levels = {
        "domestic_output": _usd(80),
        "imports": _usd(20),
        "intermediate_use": _usd(30),
        "household": _usd(25),
        "government": _usd(10),
        "investment": _usd(15),
        "d_inventories": _usd(5),
        "exports": _usd(15),
    }
    del levels[shocked]
    return levels


@dataclass(frozen=True)
class StepBinding:
    """One mechanism on a skeleton path: bindings, fixed inputs, and wires (spec §3.4)."""

    mechanism_id: str
    bindings: dict[str, str] = field(default_factory=dict)
    parameter_overrides: dict[str, Quantity] = field(default_factory=dict)
    fixed_inputs: dict[str, Quantity] = field(default_factory=dict)
    consumes: tuple[Wire, ...] = ()


@dataclass(frozen=True)
class Route:
    """The default path for one shock type and subtype (spec §4.1)."""

    target_input: str
    steps: tuple[StepBinding, ...]
    converters: tuple[UnitConverter, ...]
    effect_output: str
    effect_unit: str
    effect_variable: str
    effect_entity: EntityKey
    instrument: str
    structure_notes: str

    def mechanism_ids(self) -> list[str]:
        """Full mechanism ids in path order."""
        return [step.mechanism_id for step in self.steps]

    def propagation_path(self, library: Library) -> PropagationPath:
        """Build the path and close competing effects the library lists (spec §3.4 rule 3).

        A competing id that is not a later step is accepted, with a stub observable. The
        materiality threshold is zero shares, so a non-zero effect is not stopped. That is a
        skeleton setting, not a materiality judgment about a live thesis. Closure is computed
        from the loaded records, so a newly listed competitor does not need a new branch.
        """
        accepted: list[AcceptedCompeting] = []
        seen: set[str] = set()
        for index, step in enumerate(self.steps):
            spec = library.get(step.mechanism_id)
            later = {self.steps[j].mechanism_id for j in range(index + 1, len(self.steps))}
            for competitor in spec.competing:
                if competitor in later or competitor in seen:
                    continue
                accepted.append(_accept(competitor, "not on this skeleton path"))
                seen.add(competitor)
        return PropagationPath(
            steps=[
                MechanismInstance(
                    mechanism_id=step.mechanism_id,
                    bindings=dict(step.bindings),
                    parameter_overrides=dict(step.parameter_overrides),
                    fixed_inputs=dict(step.fixed_inputs),
                    consumes=list(step.consumes),
                )
                for step in self.steps
            ],
            accepted_competing=accepted,
            materiality_threshold=_q(0.0, "share"),
        )


def _b03(from_step: int, output: str) -> StepBinding:
    return StepBinding(
        mechanism_id="B03_bottleneck_pricing",
        bindings=dict(_B03_BINDINGS),
        parameter_overrides=_b03_overrides(),
        fixed_inputs={"u": _q(0.80, "share")},
        consumes=(Wire(input="d_demand", from_step=from_step, output=output),),
    )


def _demand() -> Route:
    """Investment demand: A04 supply-use, then B03 bottleneck pricing (spec §12.1, §13 S0.1)."""
    return Route(
        target_input="investment",
        steps=(
            StepBinding(
                mechanism_id="A04_supply_use",
                fixed_inputs=_supply_use_fixed(shocked="investment"),
            ),
            _b03(0, "total_use"),
        ),
        converters=(_level_to_pct(),),
        effect_output="d_price",
        effect_unit="pct",
        effect_variable="price",
        effect_entity=EntityKey(entity_type="commodity", entity_id="Cu"),
        instrument="CPER",
        structure_notes="stub expression: futures-based copper ETF; A09 exposure check not run",
    )


def _price() -> Route:
    """Currency move: B16 pass-through, with no parameter override (spec §12.3).

    ``pt_import`` and ``pt_cpi`` stay ``TODO_estimate``, so the propagator refuses. That refusal
    is the result the skeleton records.
    """
    return Route(
        target_input="d_fx",
        steps=(
            StepBinding(
                mechanism_id="B16_fx_passthrough",
                fixed_inputs={"invoicing_share": _q(0.5, "share")},
            ),
        ),
        converters=(),
        effect_output="d_import_price",
        effect_unit="pct",
        effect_variable="import_price",
        effect_entity=EntityKey(entity_type="currency", entity_id="BRL"),
        instrument="EMLC",
        structure_notes="stub expression: local-currency EM debt; not a sized position",
    )


def _supply() -> Route:
    """Transport disruption: A04, then B04 inventory draw, then B03 (spec §12.5)."""
    return Route(
        target_input="d_inventories",
        steps=(
            StepBinding(
                mechanism_id="A04_supply_use",
                fixed_inputs=_supply_use_fixed(shocked="d_inventories"),
            ),
            StepBinding(
                mechanism_id="B04_inventory_cycle",
                fixed_inputs={
                    "inventory": _usd(100),
                    "sales": _usd(200),
                    "target_ratio": _q(0.5, "share"),
                },
                consumes=(Wire(input="d_sales", from_step=0, output="total_use"),),
            ),
            _b03(1, "production_response"),
        ),
        converters=(_level_to_pct(),),
        effect_output="d_price",
        effect_unit="pct",
        effect_variable="price",
        effect_entity=EntityKey(entity_type="commodity", entity_id="freight"),
        instrument="BDRY",
        structure_notes="stub expression: shipping ETF; fleet utilization is not measured here",
    )


_USA = {"entity_type": "economy", "entity_id": "USA"}
_SPY = {"entity_type": "instrument", "entity_id": "SPY"}


def _broad_dollar(shock: Shock) -> bool:
    """True when a price/fx shock is the broad dollar, not a non-USD currency (spec §5.2, §12.4)."""
    return shock.origin.entity_type == "currency" and shock.origin.entity_id == "USD"


def _collateral() -> Route:
    """Equity drawdown as a collateral shock: B13 borrowing capacity (spec §3.3 B13, §4.5).

    The size is a percent drawdown and feeds ``d_asset_price``. The accelerator's elasticity is
    still ``TODO_estimate`` (the local projection kept the wrong sign), and ``leverage`` is not a
    world-state variable, so the propagator refuses before it can read leverage. Consumption,
    credit, activity, the policy reaction, and equity duration are not later steps: no card
    consumes ``d_borrowing``, and wiring that percent into another percent input would be an
    unestimated pass-through of one. SPY is the broad-equity expression of the wealth shock.
    """
    return Route(
        target_input="d_asset_price",
        steps=(
            StepBinding(
                mechanism_id="B13_financial_accelerator",
                bindings=dict(_USA),
            ),
        ),
        converters=(),
        effect_output="d_borrowing",
        effect_unit="pct",
        effect_variable="borrowing",
        effect_entity=EntityKey(entity_type="economy", entity_id="USA"),
        instrument="SPY",
        structure_notes="broad equity expresses the wealth shock; borrowing capacity is the B13 output",
    )


def _credit() -> Route:
    """High-yield OAS widening fed to the bank-credit channel (spec §3.3 B12).

    The shock is a change in percentage points and B12's only input, lending-standards
    tightening, is the same unit, so the size feeds ``standards``. The elasticity on the card
    is the installed SLOOS loan-growth range, not an OAS elasticity: treating one point of
    high-yield OAS as one point of SLOOS is a composition judgment, not a second estimate.
    C06 cannot take the change — it decomposes a spread level and needs an expected loss and
    a liquidity premium that are not in world state. Activity and the policy reaction are not
    successors; B09 would require calling credit growth an output change, and B11 takes a
    policy-rate change. HYG is the high-yield expression of the credit effect.
    """
    return Route(
        target_input="standards",
        steps=(
            StepBinding(
                mechanism_id="B12_bank_credit",
                bindings=dict(_USA),
            ),
        ),
        converters=(),
        effect_output="d_credit",
        effect_unit="pct",
        effect_variable="credit",
        effect_entity=EntityKey(entity_type="economy", entity_id="USA"),
        instrument="HYG",
        structure_notes="high-yield credit; the coefficient is the SLOOS elasticity applied to an OAS change",
    )


def _flows() -> Route:
    """VIX stress along the funding-squeeze sketch (spec §12.4, §3.3 B21, B27, C08).

    The filed size is a VIX level in index units. B21 reads a positioning percentile (share),
    B27 reads funding stress (basis points), and C08 reads two variances (share). None of those
    ports is an index, and §3.4 rule 1 does not allow a converter without a sourced coefficient:
    a VIX point is not a share or a basis point. The path still names B21 then B27 and wires
    B21's asymmetry (ratio) into B27's funding stress (bp), so validation fails that break and
    the week never multiplies the index. C08 is not a step; it also needs an expected realized
    variance that world state does not carry. HYG is the spread expression §12.4 compares with
    the dollar.
    """
    return Route(
        target_input="positioning_percentile",
        steps=(
            StepBinding(mechanism_id="B21_positioning", bindings=dict(_SPY)),
            StepBinding(
                mechanism_id="B27_funding_squeeze",
                bindings=dict(_USA),
                consumes=(Wire(input="funding_stress", from_step=0, output="asymmetry"),),
            ),
        ),
        converters=(),
        effect_output="d_spread",
        effect_unit="bp",
        effect_variable="spread",
        effect_entity=EntityKey(entity_type="economy", entity_id="USA"),
        instrument="HYG",
        structure_notes="unit break stops the path; high-yield is the spread expression, not a sized position",
    )


def _rates() -> Route:
    """A front-end yield change priced as equity duration (spec §3.3 C05, C09, B11).

    The shock is a change in the two-year yield, in percentage points, which is C05's discount-rate
    input. C09 cannot take it: the policy path reads a futures level in percent, and its risk
    premium is ``TODO_estimate``. B11 also takes a policy-rate change in percentage points, but
    its output is a demand percent, and C05's discount input is not that percent, so the two
    cannot follow each other without an unestimated converter. This path is the equity-price
    branch. Duration on the card is ``TODO_estimate``. SPY is the equity the duration prices;
    no card prices Treasury duration, so IEF is not the effect.
    """
    return Route(
        target_input="d_discount",
        steps=(
            StepBinding(mechanism_id="C05_equity_duration", bindings=dict(_SPY)),
        ),
        converters=(),
        effect_output="d_price",
        effect_unit="pct",
        effect_variable="price",
        effect_entity=EntityKey(entity_type="instrument", entity_id="SPY"),
        instrument="SPY",
        structure_notes="equity duration; Treasury duration (IEF, TLT) is not a mechanism output",
    )


def _dollar() -> Route:
    """Broad-dollar appreciation as a global-financial-cycle shock (spec §12.4, §3.3 B18, §5.2).

    The size is a percent change in the broad dollar and feeds ``d_usd``. Cross-border credit is
    the effect; the elasticity is ``TODO_estimate`` (the local projection kept the wrong sign).
    B16 stays the route for a non-USD currency depreciation (§12.3): its input is that currency's
    move against the dollar, not the credit change B18 would emit, and dominant-currency
    pass-through is the wrong first step for a dollar surge. EMB is the dollar-EM credit
    expression of the credit effect.
    """
    return Route(
        target_input="d_usd",
        steps=(
            StepBinding(mechanism_id="B18_global_financial_cycle", bindings=dict(_USA)),
        ),
        converters=(),
        effect_output="d_credit",
        effect_unit="pct",
        effect_variable="credit",
        effect_entity=EntityKey(entity_type="instrument", entity_id="EMB"),
        instrument="EMB",
        structure_notes="dollar EM debt expresses the cross-border credit effect; not a sized position",
    )


# (type, subtype) → path. price/fx for a non-USD currency is the EM pass-through route;
# route_for swaps in the dollar route when the origin is currency USD.
_DEMAND = _demand()
_EM_FX = _price()
_SUPPLY = _supply()

_BY_PAIR: dict[tuple[str, str], Route] = {
    ("demand", "investment"): _DEMAND,
    ("supply", "input_disruption"): _SUPPLY,
    ("financial", "collateral"): _collateral(),
    ("financial", "credit"): _credit(),
    ("financial", "flows"): _flows(),
    ("price", "rates"): _rates(),
    ("price", "fx"): _EM_FX,
}

# Subtypes with no pair of their own. Price is not here: commodity is not an FX path.
_TYPE_FALLBACK: dict[str, Route] = {
    "demand": _DEMAND,
    "supply": _SUPPLY,
}


def route_for(shock: Shock) -> Route:
    """Return the path for ``(shock.type, shock.subtype)`` (spec §4.1, §13 S0.2).

    ``price`` / ``fx`` with origin currency USD uses the broad-dollar route; any other currency
    keeps the EM pass-through route. A demand or supply subtype with no dedicated pair uses
    that type's skeleton route. Any other unknown combination raises ``KeyError``.
    """
    if shock.type == "price" and shock.subtype == "fx" and _broad_dollar(shock):
        return _dollar()
    found = _BY_PAIR.get((shock.type, shock.subtype))
    if found is not None:
        return found
    fallback = _TYPE_FALLBACK.get(shock.type)
    if fallback is not None:
        return fallback
    known = ", ".join(f"{family}/{subtype}" for family, subtype in sorted(_BY_PAIR))
    raise KeyError(
        f"no skeleton route for shock {shock.type}/{shock.subtype} "
        f"(known pairs: {known}; demand and supply use the type route)"
    ) from None
