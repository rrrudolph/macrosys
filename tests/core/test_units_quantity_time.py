from datetime import UTC, date, datetime

import pytest

from macrosys.core import (
    Horizon,
    Quantity,
    Timestamps,
    UnitError,
    assert_unit,
    compatible,
    compose_horizons,
    convert,
)


def test_units_vocabulary_and_currency_parsing():
    assert assert_unit("USD_bn").dimension == "currency"
    assert assert_unit("EUR_mn_real2017").real is True
    assert assert_unit("t").dimension == "mass"
    with pytest.raises(UnitError):
        assert_unit("widgets")
    assert "USD_bn" in __import__("macrosys.core.units", fromlist=["UNITS"]).UNITS


def test_compatibility_rules():
    assert compatible("USD_bn", "USD_mn")
    assert not compatible("USD_bn", "EUR_bn")
    assert not compatible("USD_bn", "USD_bn_real")
    assert compatible("ccy_bn", "USD_bn")  # placeholder binds to any currency
    assert compatible("bp", "pp")
    assert not compatible("pct", "pp")


def test_convert_scales_only():
    assert convert(1.0, "USD_bn", "USD_mn") == pytest.approx(1000.0)
    assert convert(25.0, "bp", "pp") == pytest.approx(0.25)
    with pytest.raises(UnitError):
        convert(1.0, "USD", "EUR")


def test_quantity_ranges_and_gate_helpers():
    q = Quantity(value=None, unit="pct", low=-1.0, high=3.0)
    assert q.is_range and q.mid == 1.0 and q.straddles_zero()
    assert not Quantity(value=2.0, unit="pct", low=1.0, high=3.0).straddles_zero()
    with pytest.raises(ValueError):
        Quantity(value=5.0, unit="pct", low=1.0, high=3.0)
    scaled = q.scale(-2.0)
    assert (scaled.low, scaled.high) == (-6.0, 2.0)
    total = Quantity(value=1.0, unit="USD_bn").add(Quantity(value=500.0, unit="USD_mn"))
    assert total.value == pytest.approx(1.5) and total.unit == "USD_bn"


def test_quantity_mul_declares_unit_and_uses_interval_arithmetic():
    volume = Quantity(value=None, unit="t", low=90.0, high=110.0)
    price = Quantity(value=None, unit="USD", low=-2.0, high=3.0)  # sign change exercises the corners
    revenue = volume.mul(price, unit="USD")
    assert (revenue.low, revenue.high) == (-220.0, 330.0) and revenue.value is None
    point = Quantity(value=2.0, unit="share").mul(Quantity(value=5.0, unit="USD_mn"), unit="USD_mn")
    assert point.value == 10.0 and not point.is_range
    with pytest.raises(UnitError):
        volume.mul(price, unit="not_a_unit")


def test_quantity_sub_and_div():
    expected = Quantity(value=None, unit="pct", low=2.0, high=6.0)
    priced = Quantity(value=None, unit="pct", low=1.0, high=4.0)
    gap = expected.sub(priced)
    assert (gap.low, gap.high) == (-2.0, 5.0) and gap.straddles_zero()
    days = Quantity(value=120.0, unit="t").div(Quantity(value=None, unit="t", low=2.0, high=4.0), unit="d")
    assert (days.low, days.high) == (30.0, 60.0)
    with pytest.raises(ZeroDivisionError):
        expected.div(Quantity(value=None, unit="pct", low=-1.0, high=1.0), unit="ratio")


def test_timestamps_visibility():
    ts = Timestamps(
        ref_period_start=date(2026, 6, 30),
        ref_period_end=date(2026, 6, 30),
        published_at=datetime(2026, 7, 30, 12, 30, tzinfo=UTC),
        ingested_at=datetime(2026, 7, 30, 12, 31, tzinfo=UTC),
    )
    assert not ts.visible_at(datetime(2026, 7, 30, 12, 0, tzinfo=UTC))
    assert ts.visible_at(datetime(2026, 7, 30, 12, 30, tzinfo=UTC))


def test_compose_horizons_adds_impacts_and_keeps_latest_peak():
    path = [Horizon(impact_q=0, peak_q=1, decay_q=4), Horizon(impact_q=1, peak_q=3, decay_q=None)]
    h = compose_horizons(path)
    assert h.impact_q == 1 and h.peak_q == 3 and h.decay_q is None
