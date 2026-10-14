"""The three v0 feedback loops resolve against the library (spec §4.5)."""

from __future__ import annotations

from pathlib import Path

from macrosys.mechanisms import load_library, load_loops

ROOT = Path(__file__).resolve().parents[2] / "mechanisms"


def test_feedback_loops_resolve():
    library = load_library(ROOT)
    loops = load_loops(ROOT / "loops.yaml")
    assert [loop.id for loop in loops] == ["cobweb", "accelerator", "fx_inflation_policy"]
    seen = []
    for loop in loops:
        assert loop.max_iter >= 1
        assert 0.0 <= loop.damping <= 1.0
        assert loop.tol >= 0.0
        for mechanism_id in loop.steps:
            library.get(mechanism_id)
            seen.append(mechanism_id)
    assert "B03_bottleneck_pricing" in seen
    assert "B05_capacity_pipeline" in seen
    assert "B12_bank_credit" in seen
    assert "B13_financial_accelerator" in seen
    assert "C02_uip" in seen
    assert "B19_carry" in seen
