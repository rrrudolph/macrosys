"""Regime indicators: absorption, interval overlap, Hamilton variance, DCC (spec §6.3–6.4)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from macrosys.risk import (
    absorption_ratio,
    correlation_changed,
    correlation_interval,
    dcc,
    ewma_correlation,
    rolling_correlation,
    two_state_variance_regime,
)


def test_absorption_ratio_rank_one_and_independent():
    rng = np.random.default_rng(0)
    factor = rng.normal(size=(800, 1))
    loadings = np.array([1.0, -2.0, 0.5, 3.0])
    rank_one = factor @ loadings.reshape(1, -1)
    assert absorption_ratio(rank_one, n_components=1, window=None) == pytest.approx(1.0)

    independent = rng.normal(size=(12000, 4))
    share = absorption_ratio(independent, n_components=1, window=None)
    assert share == pytest.approx(0.25, abs=0.03)


def test_correlation_changed_respects_interval_overlap():
    assert correlation_changed((0.0, 0.2), (0.3, 0.5)) is True
    assert correlation_changed((0.0, 0.2), (0.2, 0.5)) is False  # touching is overlap
    assert correlation_changed((0.0, 0.4), (0.3, 0.5)) is False
    assert correlation_changed((-0.2, 0.1), (-0.5, -0.3)) is True


def test_rolling_and_ewma_correlation_of_a_copied_series():
    rng = np.random.default_rng(0)
    x = rng.normal(size=120)
    rolled = rolling_correlation(x, x, windows=(30,))
    assert rolled.iloc[-1, 0] == pytest.approx(1.0)
    ewma = ewma_correlation(x, 2.0 * x + 0.01, halflife_days=10)
    assert ewma.iloc[-1] == pytest.approx(1.0, abs=1e-6)
    low, high = correlation_interval(x, x, n_boot=20, block_len=5, seed=1)
    assert low <= high
    assert low == pytest.approx(1.0, abs=1e-9)


def test_two_state_regime_finds_a_planted_high_variance_segment():
    rng = np.random.default_rng(5)
    calm = rng.normal(scale=0.5, size=250)
    stress = rng.normal(scale=3.0, size=150)
    y = np.concatenate([calm, stress, calm])
    probs, params = two_state_variance_regime(y, max_iter=80, tol=1e-4)
    assert params.converged
    assert params.variance_high > 4.0 * params.variance_low
    stress_slice = slice(250, 400)
    calm_slice = slice(0, 200)
    assert probs.smoothed[stress_slice].mean() > 0.8
    assert probs.smoothed[calm_slice].mean() < 0.25
    assert np.all((probs.filtered >= 0.0) & (probs.filtered <= 1.0))


def test_dcc_returns_a_correlation_path():
    rng = np.random.default_rng(3)
    t = 400
    latent = rng.normal(size=t)
    rho = 0.6
    left = latent
    right = rho * latent + np.sqrt(1.0 - rho**2) * rng.normal(size=t)
    # Common volatility bursts, predetermined, so the correlation stays rho.
    scale = np.ones(t)
    for i in range(1, t):
        scale[i] = np.sqrt(0.05 + 0.10 * (left[i - 1] ** 2) + 0.85 * scale[i - 1] ** 2)
    returns = pd.DataFrame(
        np.column_stack([left * scale, right * scale]), columns=["a", "b"]
    )
    result = dcc(returns, max_iter=40)
    assert result.correlation.shape == (t, 2, 2)
    assert np.allclose(result.correlation[:, 0, 0], 1.0)
    assert np.allclose(result.correlation[:, 0, 1], result.correlation[:, 1, 0])
    assert 0.0 <= result.a
    assert 0.0 <= result.b
    assert result.a + result.b < 1.0
    assert result.correlation[:, 0, 1].mean() == pytest.approx(rho, abs=0.15)
