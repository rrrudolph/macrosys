"""Default first-step paths, keyed by shock type (spec §4.1, §13 S0.2).

Only this module consults ``shock.type``. The three demo shocks differ by the ``Shock`` record
and by the binding this table returns. Path construction does not special-case a shock id.

B03's elasticities are still ``TODO_estimate`` in the library. The overrides below are the
example scenario already written in ``B03_bottleneck_pricing.yaml``, relabelled ``stub:S0.1``.
They are not estimates. B16 is left without overrides so it refuses to run (spec §3.1). [J]
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
    """The default path for one shock type (spec §4.1)."""

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


# Shock-type key → default path. Spec §4.1: each class has a default set of first-step mechanisms.
_ROUTES: dict[str, Route] = {
    "demand": _demand(),
    "price": _price(),
    "supply": _supply(),
}


def route_for(shock: Shock) -> Route:
    """Return the skeleton path bound to ``shock.type`` (spec §4.1, §13 S0.2).

    Unknown types raise ``KeyError``. The lookup is the only place a shock type selects a path.
    """
    try:
        return _ROUTES[shock.type]
    except KeyError:
        known = ", ".join(sorted(_ROUTES))
        raise KeyError(f"no skeleton route for shock type {shock.type!r} (known: {known})") from None
