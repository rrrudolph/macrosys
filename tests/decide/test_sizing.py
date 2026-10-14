"""Sizing caps, the Kelly ceiling, and correlated grouping (spec §11.4)."""

from __future__ import annotations

import pytest

from macrosys.decide.mandate import MandateIncompleteError, load_example_mandate
from macrosys.decide.sizing import (
    conviction_multiplier,
    fractional_kelly_ceiling,
    group_correlated,
    scenario_budget_cap,
    size_position,
    vol_parity_weight,
)
from macrosys.decide.thesis import EvidenceQuality
from tests.decide.support import make_thesis

HIGH = EvidenceQuality(observations="high", claims="high", inferences="high")


def test_example_mandate_lists_missing_paper_fields() -> None:
    mandate = load_example_mandate()
    assert mandate.sizing.fractional_kelly_ceiling == 0.25
    assert mandate.sizing.conviction_multiplier_bounds == (0.5, 1.5)
    with pytest.raises(MandateIncompleteError) as raised:
        mandate.require_complete()
    assert "risk.vol_target_annual_pct" in raised.value.missing
    assert "gap_gate.min_expected_gap_after_costs_bp" in raised.value.missing
    assert "promotion_gates.research_to_paper.min_sign_accuracy" in raised.value.missing


def test_conviction_stays_inside_mandate_bounds() -> None:
    floor = conviction_multiplier(-5.0, 5.0, HIGH)
    ceiling = conviction_multiplier(100.0, 100.0, HIGH)
    assert floor == 0.5
    assert ceiling == 1.5
    mid = conviction_multiplier(10.0, 12.0, HIGH)
    assert 0.5 <= mid <= 1.5


def test_size_respects_caps_and_kelly_ceiling() -> None:
    assert vol_parity_weight(0.20, 0.10) == 0.5
    assert scenario_budget_cap(0.20, 0.05) == 0.25
    # 0.25 * 0.02 / 0.20^2 = 0.125. That is below the scaled vol-parity weight (~0.67).
    kelly_binds = size_position(
        vol_annual=0.20,
        target_vol=0.10,
        gap_low=10.0,
        gap_high=12.0,
        evidence_quality=HIGH,
        marginal_scenario_loss=0.01,
        remaining_budget=0.50,
        position_cap=1.0,
        factor_cap=1.0,
        gap_lower_edge_annual=0.02,
        kelly_fraction=0.25,
    )
    assert kelly_binds.binding == "kelly_ceiling"
    assert kelly_binds.weight == fractional_kelly_ceiling(0.02, 0.20, 0.25)
    assert kelly_binds.weight <= kelly_binds.position_cap
    assert kelly_binds.weight <= kelly_binds.factor_cap
    assert kelly_binds.weight <= kelly_binds.scenario_cap
    assert kelly_binds.weight <= kelly_binds.vol_parity_scaled

    position_binds = size_position(
        vol_annual=0.20,
        target_vol=0.10,
        gap_low=10.0,
        gap_high=12.0,
        evidence_quality=HIGH,
        marginal_scenario_loss=0.20,
        remaining_budget=0.10,
        position_cap=0.05,
        factor_cap=0.50,
        gap_lower_edge_annual=0.50,
        kelly_fraction=0.25,
    )
    assert position_binds.binding == "position_cap"
    assert position_binds.weight == 0.05
    assert position_binds.weight <= position_binds.kelly_ceiling
    assert position_binds.weight <= position_binds.scenario_cap


def test_correlated_theses_are_grouped() -> None:
    copper = make_thesis(thesis_id="T-cu", mechanism_path=["B01", "B03"])
    aluminium = make_thesis(
        thesis_id="T-al",
        mechanism_path=["B03", "B08"],
        expression={
            "instrument": "JJU",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "aluminium ETF",
            "cost_estimate_bp": 10.0,
        },
    )
    rates = make_thesis(
        thesis_id="T-rates",
        mechanism_path=["C03"],
        sizing_inputs={"vol_annual": 0.08, "scenario_pnl": {"stress": -0.02}, "factor_loadings": {"duration": 0.1}},
        expression={
            "instrument": "IEF",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "treasury ETF",
            "cost_estimate_bp": 2.0,
        },
    )
    growth_a = make_thesis(
        thesis_id="T-g1",
        mechanism_path=["B11"],
        sizing_inputs={"vol_annual": 0.16, "scenario_pnl": {}, "factor_loadings": {"growth": 0.8}},
        expression={
            "instrument": "SPY",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "broad equity",
            "cost_estimate_bp": 2.0,
        },
    )
    growth_b = make_thesis(
        thesis_id="T-g2",
        mechanism_path=["B19"],
        sizing_inputs={"vol_annual": 0.16, "scenario_pnl": {}, "factor_loadings": {"growth": 0.6}},
        expression={
            "instrument": "EFA",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "developed equity",
            "cost_estimate_bp": 3.0,
        },
    )
    path_groups = group_correlated([copper, rates, aluminium], shared_factor_threshold=0.5)
    grouped = {frozenset(item.thesis_id for item in group) for group in path_groups}
    assert frozenset({"T-cu", "T-al"}) in grouped
    assert frozenset({"T-rates"}) in grouped

    factor_groups = group_correlated([growth_a, growth_b, rates], shared_factor_threshold=0.5)
    factor_sets = {frozenset(item.thesis_id for item in group) for group in factor_groups}
    assert frozenset({"T-g1", "T-g2"}) in factor_sets
    assert frozenset({"T-rates"}) in factor_sets
