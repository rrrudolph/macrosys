"""Composition rules, gating, loops, and identity failures (spec §3.4, §4.5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.core.time import compose_horizons
from macrosys.mechanisms import (
    AcceptedCompeting,
    CompetingClosureError,
    IdentityViolationError,
    Loop,
    MechanismInstance,
    PropagationPath,
    Propagator,
    Shock,
    UnitContinuityError,
    UnitConverter,
    Wire,
    get_impl,
    validate_path,
)


class _State:
    def __init__(self, values: dict[tuple[str, str], Quantity] | None = None) -> None:
        self.as_of = datetime(2026, 1, 1, tzinfo=UTC)
        self._values = values or {}

    def has(self, entity: EntityKey, var: str) -> bool:
        return (entity.entity_id, var) in self._values

    def get(self, entity: EntityKey, var: str) -> Quantity:
        return self._values[(entity.entity_id, var)]


def _q(value: float, unit: str, provenance: str | None = None) -> Quantity:
    return Quantity(value=value, unit=unit, provenance=provenance)


def _accept(mechanism: str) -> AcceptedCompeting:
    return AcceptedCompeting(mechanism=mechanism, observable="observed series", threshold=_q(0.01, "share"))


def _threshold(value: float = 0.0) -> Quantity:
    return _q(value, "share")


def test_unit_mismatch_rejected_and_accepted_with_a_converter(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(mechanism_id="A01_expenditure_identity"),
            MechanismInstance(
                mechanism_id="B02_cost_share",
                consumes=[Wire(input="d_price", from_step=0, output="residual")],
            ),
        ],
        accepted_competing=[_accept("B07_substitution")],
        materiality_threshold=_threshold(),
    )
    with pytest.raises(UnitContinuityError):
        validate_path(path, library, [])
    converters = [
        UnitConverter(from_unit="USD_bn", to_unit="pct", coefficient=_q(0.5, "pct", provenance="test_bridge"))
    ]
    horizon = validate_path(path, library, converters).horizon
    assert horizon.impact_q == 0 and horizon.peak_q == 0


def test_converter_coefficient_is_applied_at_runtime(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id="B05_capacity_pipeline",
                fixed_inputs={"pipeline": _q(50, "USD_bn"), "lead_time": _q(8, "q")},
                parameter_overrides={"slippage": _q(0.2, "share", provenance="example_scenario")},
            ),
            MechanismInstance(
                mechanism_id="B02_cost_share",
                fixed_inputs={"s_ij": _q(0.2, "share")},
                consumes=[Wire(input="d_price", from_step=0, output="tightness")],
            ),
        ],
        accepted_competing=[_accept("B06_demand_elasticity"), _accept("B07_substitution")],
        materiality_threshold=_threshold(),
    )
    converters = [
        UnitConverter(from_unit="USD_bn", to_unit="pct", coefficient=_q(0.5, "pct", provenance="test_bridge"))
    ]
    shock = Shock(shock_id="demand", magnitude=_q(30, "USD_bn"), target_input="demand")
    run = Propagator(library, converters).run(shock, _State(), path)
    cost = run.steps[1].outputs["d_unit_cost"]
    assert cost.value == pytest.approx(-1.0)
    assert cost.unit == "pct"


def test_missing_competing_effect_rejected_until_accepted(library):
    path = PropagationPath(
        steps=[MechanismInstance(mechanism_id="B03_bottleneck_pricing")],
        materiality_threshold=_threshold(),
    )
    with pytest.raises(CompetingClosureError) as caught:
        validate_path(path, library, [])
    assert "B04_inventory_cycle" in caught.value.missing
    assert "B06_demand_elasticity" in caught.value.missing
    assert "B07_substitution" in caught.value.missing
    closed = path.model_copy(
        update={
            "accepted_competing": [
                _accept("B04_inventory_cycle"),
                _accept("B06_demand_elasticity"),
                _accept("B07_substitution"),
            ]
        }
    )
    validate_path(closed, library, [])


def test_horizon_composition_adds_impact_and_keeps_the_latest_peak(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(mechanism_id="B04_inventory_cycle"),
            MechanismInstance(
                mechanism_id="B05_capacity_pipeline",
                consumes=[Wire(input="pipeline", from_step=0, output="production_response")],
            ),
        ],
        accepted_competing=[_accept("B06_demand_elasticity"), _accept("B07_substitution")],
        materiality_threshold=_threshold(),
    )
    got = validate_path(path, library, []).horizon
    b04 = library.get("B04_inventory_cycle").horizon.to_core()
    b05 = library.get("B05_capacity_pipeline").horizon.to_core()
    assert got == compose_horizons([b04, b05])
    assert got.impact_q == 5
    assert got.peak_q == 13
    assert got.decay_q is None


def test_materiality_stop_records_the_downstream_branch(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id="A01_expenditure_identity",
                materiality_output="import_leakage",
                fixed_inputs={
                    "C": _q(50, "USD_bn"),
                    "I": _q(30, "USD_bn"),
                    "G": _q(20, "USD_bn"),
                    "X": _q(10, "USD_bn"),
                    "Y": _q(109.5, "USD_bn"),
                },
            ),
            MechanismInstance(
                mechanism_id="B02_cost_share",
                fixed_inputs={"d_price": _q(8, "pct")},
                consumes=[Wire(input="s_ij", from_step=0, output="import_leakage")],
            ),
        ],
        accepted_competing=[_accept("B07_substitution")],
        materiality_threshold=_q(0.01, "share"),
    )
    shock = Shock(shock_id="imports", magnitude=_q(0.5, "USD_bn"), target_input="M")
    run = Propagator(library).run(shock, _State(), path)
    assert [step.mechanism_id for step in run.steps] == ["A01_expenditure_identity"]
    assert any(item.mechanism_id == "B02_cost_share" for item in run.stopped_branches)
    assert run.identity_checks["A01_expenditure_identity"] == pytest.approx(0.0)


def test_b03_price_effect_is_scaled_to_zero_below_the_utilization_gate(library):
    impl = get_impl("B03_bottleneck_pricing")
    bindings = {"entity_type": "industry", "entity_id": "USA:C24"}
    low = _State({("USA:C24", "capacity_utilization"): _q(0.50, "share")})
    high = _State({("USA:C24", "capacity_utilization"): _q(0.85, "share")})
    kink = {"u_kink": _q(0.70, "share", "spec_3.4_rule5")}
    assert impl.applicable(low, bindings, kink).factor == 0.0
    assert impl.applicable(low, bindings, kink).status == "scaled"
    assert impl.applicable(high, bindings, kink).factor == 1.0
    assert impl.applicable(high, bindings, {"u_kink": _q(0.95, "share", "scenario")}).factor == 0.0

    path = PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id="B03_bottleneck_pricing",
                bindings=bindings,
                fixed_inputs={"u": _q(0.8, "share")},
                parameter_overrides={
                    "eps_s_slack": _q(2.0, "ratio", "example_scenario"),
                    "eps_s_tight": _q(0.0, "ratio", "example_scenario"),
                    "eps_d": _q(1.0, "ratio", "example_scenario"),
                    "u_cap": _q(0.9, "share", "example_scenario"),
                },
            )
        ],
        accepted_competing=[
            _accept("B04_inventory_cycle"),
            _accept("B06_demand_elasticity"),
            _accept("B07_substitution"),
        ],
        materiality_threshold=_threshold(),
    )
    shock = Shock(shock_id="demand", magnitude=_q(4.0, "pct"), target_input="d_demand")
    muted = Propagator(library).run(shock, low, path)
    live = Propagator(library).run(shock, high, path)
    assert muted.steps[0].outputs["d_price"].value == pytest.approx(0.0)
    assert live.steps[0].outputs["d_price"].value == pytest.approx(2.0)


def test_nonconvergent_loop_is_reported_not_raised(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id="B02_cost_share",
                fixed_inputs={"s_ij": _q(2.0, "share", provenance="loop_test")},
            )
        ],
        accepted_competing=[_accept("B07_substitution")],
        materiality_threshold=_threshold(),
        loops=[
            Loop(
                steps=[0],
                max_iter=5,
                damping=1.0,
                tol=1.0e-6,
                feedback_output="d_unit_cost",
                feedback_input="d_price",
            )
        ],
    )
    shock = Shock(shock_id="price", magnitude=_q(1.0, "pct"), target_input="d_price")
    run = Propagator(library).run(shock, _State(), path)
    assert len(run.unstable_loops) == 1
    assert run.unstable_loops[0].note == "unstable under current parameters"
    assert run.unstable_loops[0].iterations == 5


def test_damped_loop_that_settles_is_not_unstable(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id="B02_cost_share",
                fixed_inputs={"s_ij": _q(0.0, "share", provenance="loop_test")},
            )
        ],
        accepted_competing=[_accept("B07_substitution")],
        materiality_threshold=_threshold(),
        loops=[
            Loop(
                steps=[0],
                max_iter=5,
                damping=1.0,
                tol=1.0e-6,
                feedback_output="d_unit_cost",
                feedback_input="d_price",
            )
        ],
    )
    shock = Shock(shock_id="price", magnitude=_q(1.0, "pct"), target_input="d_price")
    run = Propagator(library).run(shock, _State(), path)
    assert run.unstable_loops == []


def test_identity_residual_beyond_tolerance_fails_the_run(library):
    path = PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id="A01_expenditure_identity",
                fixed_inputs={
                    "C": _q(60, "USD_bn"),
                    "I": _q(20, "USD_bn"),
                    "G": _q(20, "USD_bn"),
                    "X": _q(10, "USD_bn"),
                    "M": _q(10, "USD_bn"),
                },
            )
        ],
        materiality_threshold=_threshold(),
    )
    shock = Shock(shock_id="output", magnitude=_q(110, "USD_bn"), target_input="Y")
    with pytest.raises(IdentityViolationError) as caught:
        Propagator(library).run(shock, _State(), path)
    assert caught.value.mechanism_id == "A01_expenditure_identity"
    assert caught.value.residual == pytest.approx(10.0)
