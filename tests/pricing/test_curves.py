"""Par bootstrap, forwards, and store loaders (spec §3.3 C03, C04)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import pytest

from macrosys.pricing.curves import (
    ParCurve,
    ZeroCurve,
    bootstrap_zero,
    fit_nelson_siegel,
    forward_rate,
    load_zero_curve,
    nelson_siegel_rate,
)
from tests.pricing.conftest import obs


def test_flat_par_curve_bootstraps_to_itself():
    par = ParCurve.from_pairs([(1 / 12, 3.5), (0.5, 3.5), (1.0, 3.5), (2.0, 3.5), (10.0, 3.5)])
    zero = bootstrap_zero(par)
    for point in zero.points:
        assert point.rate.value == pytest.approx(3.5, abs=1e-9)
    assert zero.compounding == "semi_annual"
    assert forward_rate(zero, 1.0, 2.0).value == pytest.approx(3.5, abs=1e-9)


def test_two_point_upward_curve_matches_hand_bootstrap():
    # 6-month par 4%, 1-year par 5%. Semi-annual coupons.
    # DF(0.5) = 1 / 1.02
    # coupon = 0.025; DF(1) = (1 - 0.025 * DF(0.5)) / 1.025
    # z(1) = 200 * (DF(1)^(-1/2) - 1)
    par = ParCurve.from_pairs([(0.5, 4.0), (1.0, 5.0)])
    # Linear par between the quotes: at 0.75y the par yield is 4.5%.
    assert par.yield_at(0.75).value == pytest.approx(4.5)

    df_6m = 1.0 / (1.0 + 0.04 / 2.0)
    coupon = 0.05 / 2.0
    df_1y = (1.0 - coupon * df_6m) / (1.0 + coupon)
    z_1y = 200.0 * (df_1y ** (-0.5) - 1.0)

    zero = bootstrap_zero(par)
    assert zero.zero_at(0.5).value == pytest.approx(4.0, abs=1e-12)
    assert zero.zero_at(1.0).value == pytest.approx(z_1y, abs=1e-9)

    fwd = forward_rate(zero, 0.5, 1.0)
    z6 = zero.zero_at(0.5).value
    z1 = zero.zero_at(1.0).value
    assert z6 is not None and z1 is not None and fwd.value is not None
    # (1+z1/200)^(2) == (1+z6/200)^(1) * (1+f/200)^(1)
    left = (1.0 + z1 / 200.0) ** 2.0
    right = (1.0 + z6 / 200.0) ** 1.0 * (1.0 + fwd.value / 200.0) ** 1.0
    assert left == pytest.approx(right, rel=1e-12)


def test_continuous_forward_on_a_flat_gsw_curve():
    zero = ZeroCurve.from_pairs([(1.0, 4.0), (2.0, 4.0)], compounding="continuous", source="FEDGSW")
    assert forward_rate(zero, 1.0, 2.0).value == pytest.approx(4.0)


def test_nelson_siegel_recovers_a_known_curve():
    beta0, beta1, beta2, tau = 4.0, -1.0, 0.5, 2.0

    def ns(tenor: float) -> float:
        x = tenor / tau
        factor = (1.0 - math.exp(-x)) / x
        return beta0 + beta1 * factor + beta2 * (factor - math.exp(-x))

    tenors = [0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
    zero = ZeroCurve.from_pairs([(t, ns(t)) for t in tenors], compounding="continuous", source="synthetic")
    fit = fit_nelson_siegel(zero)
    assert fit.beta0.value == pytest.approx(beta0, abs=1e-4)
    assert fit.beta1.value == pytest.approx(beta1, abs=1e-4)
    assert fit.beta2.value == pytest.approx(beta2, abs=1e-4)
    assert fit.tau.value == pytest.approx(tau, abs=1e-3)
    assert fit.rmse.value == pytest.approx(0.0, abs=1e-6)
    assert nelson_siegel_rate(2.0, beta0, beta1, beta2, tau) == pytest.approx(ns(2.0))


def test_gsw_zeros_are_preferred_over_par(store):
    day = date(2024, 1, 2)
    store.append(
        [
            obs("TREASURY:PAR:6M", day, 9.0),
            obs("TREASURY:PAR:1Y", day, 9.0),
            obs("FEDGSW:SVENY01", day, 3.0),
        ]
    )
    snap = store.snapshot(datetime(2024, 1, 3, tzinfo=UTC))
    zero = load_zero_curve(snap, ccy="USD")
    assert zero.source == "FEDGSW"
    assert zero.compounding == "continuous"
    assert zero.zero_at(1.0).value == pytest.approx(3.0)
    assert zero.zero_at(1.0).provenance == "FEDGSW:SVENY01"


def test_par_bootstrap_used_when_gsw_is_absent(store):
    day = date(2024, 1, 2)
    store.append([obs("TREASURY:PAR:6M", day, 9.0), obs("TREASURY:PAR:1Y", day, 9.0)])
    snap = store.snapshot(datetime(2024, 1, 3, tzinfo=UTC))
    zero = load_zero_curve(snap)
    assert zero.compounding == "semi_annual"
    assert zero.zero_at(0.5).value == pytest.approx(9.0, abs=1e-9)
    assert zero.zero_at(1.0).value == pytest.approx(9.0, abs=1e-9)


def test_ecb_spot_series_is_the_mapped_zero(store):
    day = date(2024, 6, 3)
    series_id = "ECB:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"
    store.append([obs(series_id, day, 2.5)])
    snap = store.snapshot(datetime(2024, 6, 4, tzinfo=UTC))
    zero = load_zero_curve(snap, ccy="EUR")
    assert zero.compounding == "annual"
    assert zero.zero_at(10.0).value == pytest.approx(2.5)
    assert zero.zero_at(10.0).provenance == series_id
