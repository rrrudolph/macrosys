"""Breakeven decomposition keeps unknown premia as ranges (spec §3.3 C04)."""

from __future__ import annotations

import pytest

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.impl.pricing import Breakeven
from macrosys.pricing.curves import ZeroCurve
from macrosys.pricing.inflation import breakeven, decompose_breakeven


def test_zero_breakeven_is_nominal_minus_real():
    nominal = ZeroCurve.from_pairs([(5.0, 4.0), (10.0, 4.0)], compounding="semi_annual", source="nominal")
    real = ZeroCurve.from_pairs([(5.0, 1.5), (10.0, 1.5)], compounding="semi_annual", source="real")
    gap = breakeven(nominal, real, 10.0)
    assert gap.unit == "pct"
    assert gap.value == pytest.approx(2.5)


def test_missing_premia_widen_and_are_not_zero():
    level = Quantity(value=2.5, unit="pct", provenance="FRED:T10YIE")
    path = decompose_breakeven(level, None, None, horizon_q=40.0)
    assert path[0][0] == pytest.approx(40.0)
    got = path[0][1]
    assert got.value == pytest.approx(2.5)
    assert got.low is not None and got.high is not None
    assert got.low < 2.5 < got.high
    assert got.provenance is not None and "TODO_estimate" in got.provenance
    # A silent zero premium would have left a point at the breakeven.
    assert got.high - got.low > 0.0


def test_explicit_premia_match_the_c04_mechanism():
    level = Quantity(value=2.5, unit="pct", provenance="test")
    irp = Quantity(value=0.2, unit="pct", provenance="scenario")
    liquidity = Quantity(value=0.1, unit="pct", provenance="scenario")
    got = decompose_breakeven(level, irp, liquidity, horizon_q=40.0)[0][1]
    via_mechanism = Breakeven().apply(
        {
            "nominal": Quantity(value=4.0, unit="pct"),
            "real_yield": Quantity(value=1.5, unit="pct"),
        },
        {"irp": irp, "liquidity": liquidity},
    )
    assert got.value == pytest.approx(2.4)
    assert got.value == pytest.approx(via_mechanism["inflation_ex_premia"].value)
