"""No-trade default and the four gates (spec §11.3)."""

from __future__ import annotations

from macrosys.decide.gates import decide
from tests.decide.support import make_thesis


def _decide(thesis, **overrides: object):
    args = {
        "run_valid": True,
        "identity_residuals_ok": True,
        "min_gap_bp": 20.0,
        "exposure_match": True,
        "horizon_ok": True,
        "post_trade_ok": True,
        "regime_flag": "stable",
    }
    args.update(overrides)
    return decide(thesis, **args)  # type: ignore[arg-type]


def test_gap_straddling_zero_is_no_trade() -> None:
    thesis = make_thesis(gap={"unit": "bp", "low": -15.0, "high": 40.0})
    decision = _decide(thesis)
    assert decision.action == "no_trade"
    assert decision.failing_gate == "gap"
    assert "straddles zero" in decision.reason


def test_gap_below_cost_plus_margin_is_no_trade() -> None:
    # Lower edge 15 bp, cost 10 bp, margin 20 bp. 15 < 30.
    thesis = make_thesis(gap={"unit": "bp", "low": 15.0, "high": 80.0})
    decision = _decide(thesis)
    assert decision.action == "no_trade"
    assert decision.failing_gate == "gap"


def test_blocking_missing_evidence_is_no_trade() -> None:
    thesis = make_thesis(missing_evidence=[{"description": "no off-exchange inventory", "blocking": True}])
    decision = _decide(thesis)
    assert decision.action == "no_trade"
    assert decision.failing_gate == "mechanism"
    assert "off-exchange" in decision.reason


def test_all_gates_pass_is_trade() -> None:
    decision = _decide(make_thesis())
    assert decision.action == "trade"
    assert decision.failing_gate is None
    assert all(gate.passed for gate in decision.gates)


def test_deteriorating_regime_is_no_trade() -> None:
    decision = _decide(make_thesis(), regime_flag="deteriorating")
    assert decision.action == "no_trade"
    assert decision.failing_gate == "risk"
