"""Reverse Gordon, reverse DCF, ERP ranges, and duration (spec §3.3 C05, C13)."""

from __future__ import annotations

import pytest

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.impl.pricing import EquityDuration
from macrosys.pricing.equity import (
    dcf_price,
    equity_duration,
    erp_proxies,
    implied_growth_from_multiple,
    reverse_dcf,
)


def test_reverse_gordon_round_trip():
    # PE 10, required return 8%, payout 0.5 → earnings yield 10% → g = 8 - 0.5*10 = 3%.
    pe = Quantity(value=10.0, unit="ratio", provenance="test")
    required = Quantity(value=8.0, unit="pct", provenance="test")
    payout = Quantity(value=0.5, unit="share", provenance="test")
    growth = implied_growth_from_multiple(pe, required, payout)
    assert growth.unit == "pct"
    assert growth.value == pytest.approx(3.0)
    earnings_yield_pct = (required.value - growth.value) / payout.value  # type: ignore[operator]
    assert 100.0 / earnings_yield_pct == pytest.approx(10.0)


def test_dcf_hand_price_and_growth_round_trip():
    # One year, no growth: cash flow 100, terminal = 100 / 0.10, price = 1100 / 1.10 = 1000.
    fcf = Quantity(value=100.0, unit="USD", provenance="test")
    flat = Quantity(value=0.0, unit="pct", provenance="test")
    discount = Quantity(value=10.0, unit="pct", provenance="test")
    price = dcf_price(fcf, flat, 1, flat, discount)
    assert price.value == pytest.approx(1000.0)

    fcf0 = Quantity(value=10.0, unit="USD", provenance="test")
    growth = Quantity(value=4.0, unit="pct", provenance="test")
    terminal = Quantity(value=2.0, unit="pct", provenance="test")
    rate = Quantity(value=8.0, unit="pct", provenance="test")
    level = dcf_price(fcf0, growth, 5, terminal, rate)
    solved = reverse_dcf(level, fcf0, 5, terminal, rate)
    assert solved.value == pytest.approx(4.0, abs=1e-6)


def test_reverse_dcf_corner_box():
    fcf0 = Quantity(value=10.0, unit="USD", provenance="test")
    growth = Quantity(value=4.0, unit="pct", provenance="test")
    terminal = Quantity(value=2.0, unit="pct", provenance="test")
    rate = Quantity(value=8.0, unit="pct", provenance="test")
    level = dcf_price(fcf0, growth, 5, terminal, rate)
    box = Quantity(value=8.0, unit="pct", low=7.0, high=9.0, provenance="test")
    solved = reverse_dcf(level, fcf0, 5, terminal, box)
    at_low = reverse_dcf(level, fcf0, 5, terminal, Quantity(value=7.0, unit="pct", provenance="test"))
    at_high = reverse_dcf(level, fcf0, 5, terminal, Quantity(value=9.0, unit="pct", provenance="test"))
    assert solved.low == pytest.approx(at_low.value)
    assert solved.high == pytest.approx(at_high.value)
    assert solved.value is not None and solved.low is not None and solved.high is not None
    assert solved.low < solved.value < solved.high


def test_erp_dividend_discount_keeps_the_growth_range():
    proxies = erp_proxies(
        Quantity(value=5.0, unit="pct", provenance="test"),
        Quantity(value=2.0, unit="pct", provenance="test"),
        Quantity(value=3.0, unit="pct", provenance="test"),
        Quantity(value=2.0, unit="pct", low=1.0, high=3.0, provenance="test"),
    )
    gap = proxies["earnings_yield_minus_real"]
    gordon = proxies["dividend_discount"]
    assert gap.value == pytest.approx(3.0)
    # 3 + growth − 2, growth in [1, 3] → [2, 4], point 3.
    assert gordon.value == pytest.approx(3.0)
    assert gordon.low == pytest.approx(2.0)
    assert gordon.high == pytest.approx(4.0)


def test_equity_duration_matches_c05_units():
    # Trailing yield 5%, no growth → forward yield 5% → duration 20 years.
    duration = equity_duration(
        Quantity(value=5.0, unit="pct", provenance="test"),
        Quantity(value=0.0, unit="pct", provenance="test"),
    )
    assert duration.unit == "y"
    assert duration.value == pytest.approx(20.0)
    price = EquityDuration().apply(
        {"d_discount": Quantity(value=1.0, unit="pp", provenance="test")},
        {"duration": Quantity(value=duration.value, unit="y", provenance="scenario")},
    )
    assert price["d_price"].value == pytest.approx(-20.0)
