"""Shrinkage covariance, PSD repair, pairwise helper (spec §6.2)."""

from __future__ import annotations

import numpy as np
import pytest

from macrosys.risk import ewma_covariance, ledoit_wolf, nearest_psd, pairwise_covariance, to_correlation


def _sample_cov(returns: np.ndarray) -> np.ndarray:
    centered = returns - returns.mean(axis=0)
    sigma = (centered.T @ centered) / returns.shape[0]
    return 0.5 * (sigma + sigma.T)


def _constant_correlation_target(sample: np.ndarray) -> np.ndarray:
    n = sample.shape[0]
    variances = np.diag(sample)
    outer = np.outer(np.sqrt(variances), np.sqrt(variances))
    corr = sample / outer
    r_bar = (corr.sum() - n) / (n * (n - 1))
    target = r_bar * outer
    np.fill_diagonal(target, variances)
    return target


def test_ledoit_wolf_intensity_in_unit_interval_and_shrinks_on_small_samples():
    rng = np.random.default_rng(7)
    small = rng.normal(size=(30, 25))
    for target in ("constant_correlation", "identity", "diagonal"):
        sigma, intensity = ledoit_wolf(small, target)
        assert 0.0 <= intensity <= 1.0
        assert sigma.shape == (25, 25)
        assert np.allclose(sigma, sigma.T)

    sample = _sample_cov(small)
    shrunk, intensity = ledoit_wolf(small, "constant_correlation")
    assert intensity > 0.0
    target = _constant_correlation_target(sample)
    # Variances are held at the sample variances, so the correlation is a
    # convex combination of the sample correlation and the constant target.
    assert np.linalg.norm(shrunk - target) < np.linalg.norm(sample - target)

    ident, ident_intensity = ledoit_wolf(small, "identity")
    assert ident_intensity > 0.2
    mu = np.trace(sample) / sample.shape[0]
    eye = np.eye(sample.shape[0]) * mu
    assert np.linalg.norm(ident - eye) < np.linalg.norm(sample - eye)


def test_ledoit_wolf_recovers_sample_covariance_as_sample_grows():
    rng = np.random.default_rng(11)
    truth = np.array([[1.0, 0.6, 0.1], [0.6, 1.5, -0.2], [0.1, -0.2, 0.8]])
    big = rng.multivariate_normal(np.zeros(3), truth, size=8000)
    sample = _sample_cov(big)
    for target in ("constant_correlation", "identity", "diagonal"):
        sigma, intensity = ledoit_wolf(big, target)
        assert intensity < 0.05
        rel = np.linalg.norm(sigma - sample) / np.linalg.norm(sample)
        assert rel < 0.05


def test_ledoit_wolf_listwise_not_pairwise():
    rng = np.random.default_rng(0)
    returns = rng.normal(size=(40, 2))
    returns[0, 0] = np.nan
    sigma, _ = ledoit_wolf(returns, "identity")
    sigma_complete, _ = ledoit_wolf(returns[1:], "identity")
    assert np.allclose(sigma, sigma_complete)
    pairwise = pairwise_covariance(returns)
    assert not np.allclose(pairwise, sigma, equal_nan=True)


def test_nearest_psd_repairs_indefinite_matrix_and_flags_it():
    indefinite = np.array([[1.0, 2.0], [2.0, 1.0]])
    repaired, flag = nearest_psd(indefinite)
    assert flag is True
    assert np.linalg.eigvalsh(repaired).min() >= -1e-10
    # Higham projection: eigenvalues 3 and -1 clip to 3 and 0.
    expected = np.array([[1.5, 1.5], [1.5, 1.5]])
    assert np.allclose(repaired, expected)

    rng = np.random.default_rng(1)
    pd_matrix = np.cov(rng.normal(size=(80, 3)), rowvar=False)
    untouched, flag_pd = nearest_psd(pd_matrix)
    assert flag_pd is False
    assert np.allclose(untouched, pd_matrix)


def test_pairwise_covariance_can_be_indefinite_and_is_not_repaired():
    rng = np.random.default_rng(0)
    base = rng.normal(size=300)
    returns = np.full((300, 3), np.nan)
    returns[:100, 0] = base[:100]
    returns[:100, 1] = base[:100]
    returns[100:200, 0] = base[100:200]
    returns[100:200, 2] = base[100:200]
    returns[200:, 1] = base[200:]
    returns[200:, 2] = -base[200:]
    sigma = pairwise_covariance(returns)
    assert np.linalg.eigvalsh(to_correlation(sigma)).min() < -0.1
    repaired, flag = nearest_psd(sigma)
    assert flag is True
    assert np.linalg.eigvalsh(repaired).min() >= -1e-8


def test_to_correlation_and_ewma_puts_weight_on_the_recent_shock():
    sigma = np.array([[4.0, 2.0], [2.0, 1.0]])
    corr = to_correlation(sigma)
    assert np.allclose(corr, [[1.0, 1.0], [1.0, 1.0]])
    with pytest.raises(ValueError):
        to_correlation(np.array([[1.0, 0.0], [0.0, 0.0]]))

    rng = np.random.default_rng(0)
    calm = rng.normal(scale=0.01, size=(200, 1))
    shock = rng.normal(scale=0.05, size=(30, 1))
    series = np.vstack([calm, shock])
    ewma = ewma_covariance(series, halflife_days=10.0)
    assert ewma[0, 0] > np.var(series)
