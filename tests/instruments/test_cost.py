import pytest

from macrosys.instruments import round_trip_cost_bp


def test_round_trip_cost_arithmetic():
    # 2*2 + 1 + 0.002*(6/12)*10000 + (10+20)*(6/12) = 4 + 1 + 10 + 15 = 30 bp.
    assert round_trip_cost_bp(
        2, 0.002, 6, roll_cost_annual_bp=10, hedge_cost_annual_bp=20, impact_bp=1
    ) == 30.0
    assert round_trip_cost_bp(1.5, 0.0, 0.0, impact_bp=0.5) == 3.5
    # Borrow on an outright short is an annual fee scaled by the holding period: 40 bp/yr over 6 months = 20 bp.
    assert round_trip_cost_bp(2, 0.0, 6, borrow_cost_annual_bp=40) == 24.0
    with pytest.raises(ValueError):
        round_trip_cost_bp(-1, 0.0, 1)
    with pytest.raises(ValueError):
        round_trip_cost_bp(1, 0.0, 1, borrow_cost_annual_bp=-5)
