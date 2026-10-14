"""Curve shape, convenience yield, and ETF roll cost (spec §3.3 C07, A09)."""

from __future__ import annotations

import math
from datetime import date

import pytest

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.impl.pricing import CostOfCarry
from macrosys.pricing.commodities import (
    etf_roll_cost_estimate,
    futures_curve_shape,
    implied_convenience_yield,
)

_FRONT = date(2026, 1, 1)
_SECOND = date(2026, 7, 1)
_YEAR = date(2027, 1, 1)


def _curve() -> list[tuple[date, float]]:
    return [(_FRONT, 100.0), (_SECOND, 102.0), (_YEAR, 110.0)]


def test_contango_roll_yield_matches_hand_arithmetic():
    shape = futures_curve_shape(_curve())
    front_years = (_SECOND - _FRONT).days / 365.25
    year_days = (_YEAR - _FRONT).days
    hand_front = math.log(100.0 / 102.0) / front_years
    hand_year = math.log(100.0 / 110.0) / (year_days / 365.25)
    assert shape.shape == "contango"
    assert shape.front_roll_yield.unit == "share"
    assert shape.front_roll_yield.value == pytest.approx(hand_front)
    assert shape.year_roll_yield is not None
    assert shape.year_roll_yield.value == pytest.approx(hand_year)
    assert hand_front < 0.0


def test_backwardation_label():
    shape = futures_curve_shape([(date(2026, 1, 1), 100.0), (date(2026, 2, 1), 99.0)])
    assert shape.shape == "backwardation"
    assert shape.front_roll_yield.value is not None
    assert shape.front_roll_yield.value > 0.0


def test_convenience_yield_matches_cost_of_carry():
    spot = Quantity(value=100.0, unit="index", provenance="test")
    future = Quantity(value=90.0, unit="index", provenance="test")
    rate = Quantity(value=0.02, unit="share", provenance="test")
    storage = Quantity(value=0.01, unit="share", provenance="test")
    tenor = Quantity(value=1.0, unit="y", provenance="test")
    got = implied_convenience_yield(spot, future, rate, storage, tenor)
    via = CostOfCarry().apply(
        {"spot": spot, "futures": future, "tenor": tenor, "rate": rate, "storage": storage},
        {},
    )
    assert got.value == pytest.approx(0.13536051565782635)
    assert got.value == pytest.approx(via["implied_convenience"].value)


def test_etf_roll_cost_is_the_negative_of_roll_yield():
    shape = futures_curve_shape(_curve())
    cost = etf_roll_cost_estimate(_curve(), [(_FRONT, _SECOND, 1.0)])
    assert cost.unit == "share"
    assert shape.front_roll_yield.value is not None and cost.value is not None
    assert cost.value == pytest.approx(-shape.front_roll_yield.value)
    assert cost.value > 0.0
