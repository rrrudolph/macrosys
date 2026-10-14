"""Covariance estimators for the risk layer (spec §6.2).

Ledoit–Wolf analytical shrinkage, EWMA, a flagged PSD repair, and an explicit
pairwise-complete helper. Pairwise completion is not used anywhere else: the
estimators below drop incomplete rows (listwise) and never rewrite individual
correlations to force a matrix to be positive semi-definite.
"""

from __future__ import annotations

from typing import Literal

import numpy as np

# [J] Floor is 0. Higham (1988, Linear Algebra Appl.) shows that the Frobenius-nearest
# symmetric PSD matrix is the eigenvalue clip at zero. Negatives smaller than
# ``ROUND_OFF_REL`` times the spectral scale are treated as roundoff and do not
# set the repaired flag; they are still clipped so the result is PSD.
EIGENVALUE_FLOOR = 0.0
ROUND_OFF_REL = 1e-10

Target = Literal["constant_correlation", "identity", "diagonal"]


def ledoit_wolf(returns: np.ndarray, target: Target) -> tuple[np.ndarray, float]:
    """Analytical Ledoit–Wolf shrinkage covariance (spec §6.2).

    Implements ``Σ = intensity · F + (1 − intensity) · S`` with ``intensity`` clipped
    to [0, 1], where ``S`` is the maximum-likelihood sample covariance (divisor ``T``,
    columns demeaned). ``target='constant_correlation'`` is the estimator in Ledoit &
    Wolf (2004), "Honey, I Shrunk the Sample Covariance Matrix", *J. Portfolio
    Management* 30(4), also Ledoit & Wolf (2003), *J. Empirical Finance*.
    ``target='identity'`` is the scaled-identity estimator of Ledoit & Wolf (2004),
    "A well-conditioned estimator for large-dimensional covariance matrices",
    *J. Multivariate Analysis*. ``target='diagonal'`` uses the same analytical
    intensity with ``F = diag(S)``. Rows with any non-finite return are dropped
    (listwise); pairwise deletion is only available from ``pairwise_covariance``.
    The result is not silently projected onto the PSD cone — call ``nearest_psd``.
    """
    x = _listwise_demean(returns)
    t, n = x.shape
    sample = _gram_covariance(x)
    if target == "constant_correlation":
        sigma, intensity = _shrink_constant_correlation(x, sample, t, n)
    elif target == "identity":
        sigma, intensity = _shrink_identity(x, sample, t, n)
    elif target == "diagonal":
        sigma, intensity = _shrink_diagonal(x, sample, t, n)
    else:
        raise ValueError(
            "target must be 'constant_correlation', 'identity', or 'diagonal', "
            f"got {target!r}"
        )
    return sigma, intensity


def ewma_covariance(returns: np.ndarray, halflife_days: float) -> np.ndarray:
    """Exponentially weighted covariance (spec §6.2, EWMA vol overlay).

    Weights are proportional to ``λ**age`` with ``λ = 2**(−1/halflife_days)``,
    normalized to sum to 1, so the most recent row has the largest weight and the
    weight halves every ``halflife_days`` observations (trading rows, not calendar
    days). The weighted mean is removed. Incomplete rows are dropped listwise, which
    closes gaps rather than treating them as zero. The matrix is a Gram matrix and
    therefore PSD up to roundoff; it is not passed through ``nearest_psd``.
    """
    if halflife_days <= 0:
        raise ValueError("halflife_days must be positive")
    x = _listwise_rows(returns)
    t, _n = x.shape
    lam = 0.5 ** (1.0 / float(halflife_days))
    age = np.arange(t - 1, -1, -1, dtype=np.float64)
    weights = lam**age
    weights /= weights.sum()
    mean = weights @ x
    centered = x - mean
    sigma = (centered * weights[:, None]).T @ centered
    return 0.5 * (sigma + sigma.T)


def nearest_psd(sigma: np.ndarray) -> tuple[np.ndarray, bool]:
    """Eigenvalue-clip a symmetric matrix onto the PSD cone (spec §6.2).

    The floor is ``EIGENVALUE_FLOOR`` (0), the Frobenius projection of Higham (1988).
    The input is symmetrized first. ``repaired`` is True when a negative eigenvalue
    exceeded roundoff or the input was materially asymmetric, so a repair is always
    visible and is applied to the whole matrix rather than to selected correlations.
    """
    a = np.asarray(sigma, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise ValueError("Sigma must be square")
    if not np.isfinite(a).all():
        raise ValueError("Sigma must be finite")
    scale = max(1.0, float(np.max(np.abs(a))))
    asymmetric = float(np.max(np.abs(a - a.T))) > ROUND_OFF_REL * scale
    a = 0.5 * (a + a.T)
    evals, evecs = np.linalg.eigh(a)
    roundoff = ROUND_OFF_REL * max(1.0, float(np.max(np.abs(evals))))
    indefinite = bool(np.any(evals < EIGENVALUE_FLOOR - roundoff))
    clipped = np.maximum(evals, EIGENVALUE_FLOOR)
    repaired = bool(indefinite or asymmetric)
    out = (evecs * clipped) @ evecs.T
    out = 0.5 * (out + out.T)
    return out, repaired


def to_correlation(sigma: np.ndarray) -> np.ndarray:
    """Map a covariance to a correlation, ``D**−1/2 Σ D**−1/2`` (spec §6.2).

    Raises if any variance is non-positive or the matrix is materially asymmetric.
    This rescales; it does not change the correlation structure and does not repair
    indefiniteness.
    """
    a = np.asarray(sigma, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise ValueError("Sigma must be square")
    if not np.isfinite(a).all():
        raise ValueError("Sigma must be finite")
    scale = max(1.0, float(np.max(np.abs(a))))
    if float(np.max(np.abs(a - a.T))) > 1e-8 * scale:
        raise ValueError("Sigma is not symmetric")
    a = 0.5 * (a + a.T)
    variances = np.diag(a).copy()
    if np.any(variances <= 0):
        raise ValueError("correlation is undefined when a variance is non-positive")
    inv = 1.0 / np.sqrt(variances)
    corr = a * np.outer(inv, inv)
    np.fill_diagonal(corr, 1.0)
    return corr


def pairwise_covariance(returns: np.ndarray) -> np.ndarray:
    """Pairwise-complete sample covariance (spec §6.1–6.2). Not guaranteed PSD.

    Entry ``(i, j)`` uses only rows where both series are finite, demeaned on that
    pair, divided by the pair's count (the Ledoit–Wolf maximum-likelihood divisor,
    not ``n − 1``). Different pairs can rest on different dates, so the matrix can
    be indefinite. This function does not repair it and does not edit individual
    correlations; callers that need a PSD matrix must call ``nearest_psd`` and
    keep the ``repaired`` flag. Every other estimator in this package uses listwise
    deletion instead.
    """
    x = _as_2d(returns)
    _t, n = x.shape
    sigma = np.full((n, n), np.nan, dtype=np.float64)
    for i in range(n):
        for j in range(i, n):
            mask = np.isfinite(x[:, i]) & np.isfinite(x[:, j])
            count = int(mask.sum())
            if count < 2:
                continue
            left = x[mask, i]
            right = x[mask, j]
            left = left - left.mean()
            right = right - right.mean()
            value = float(np.dot(left, right) / count)
            sigma[i, j] = value
            sigma[j, i] = value
    return sigma


def _as_2d(returns: np.ndarray) -> np.ndarray:
    x = np.asarray(returns, dtype=np.float64)
    if x.ndim != 2:
        raise ValueError("returns must be a T×N array")
    if x.shape[0] < 1 or x.shape[1] < 1:
        raise ValueError("returns must have at least one row and one column")
    return x


def _listwise_rows(returns: np.ndarray) -> np.ndarray:
    x = _as_2d(returns)
    keep = np.isfinite(x).all(axis=1)
    x = x[keep]
    if x.shape[0] < 2:
        raise ValueError("need at least 2 finite rows after listwise deletion")
    return x


def _listwise_demean(returns: np.ndarray) -> np.ndarray:
    x = _listwise_rows(returns)
    return x - x.mean(axis=0)


def _gram_covariance(demeaned: np.ndarray) -> np.ndarray:
    t = demeaned.shape[0]
    sigma = (demeaned.T @ demeaned) / t
    return 0.5 * (sigma + sigma.T)


def _entry_variance(demeaned: np.ndarray, sample: np.ndarray) -> np.ndarray:
    """``φ_ij = mean_t (x_it x_jt − s_ij)²``, the LW asymptotic-variance estimator."""
    squares = demeaned**2
    t = demeaned.shape[0]
    return (squares.T @ squares) / t - sample**2


def _clip_intensity(raw: float) -> float:
    if not np.isfinite(raw):
        return 0.0
    return float(np.clip(raw, 0.0, 1.0))


def _shrink_identity(
    demeaned: np.ndarray, sample: np.ndarray, t: int, n: int
) -> tuple[np.ndarray, float]:
    # Ledoit & Wolf (2004) JMA: weight on μI is (π / T) / ||S − μI||²_F.
    mu = float(np.trace(sample) / n)
    target = np.eye(n) * mu
    phi = float(np.sum(_entry_variance(demeaned, sample)))
    gamma = float(np.sum((sample - target) ** 2))
    intensity = 0.0 if gamma <= 0.0 else _clip_intensity((phi / t) / gamma)
    sigma = intensity * target + (1.0 - intensity) * sample
    return 0.5 * (sigma + sigma.T), intensity


def _shrink_diagonal(
    demeaned: np.ndarray, sample: np.ndarray, t: int, n: int
) -> tuple[np.ndarray, float]:
    # F = diag(S) is non-random off the diagonal, so ρ reduces to the diagonal
    # of φ and only off-diagonal estimation error enters the intensity.
    phi = _entry_variance(demeaned, sample)
    off = ~np.eye(n, dtype=bool)
    gamma = float(np.sum(sample[off] ** 2))
    pi_off = float(np.sum(phi[off]))
    intensity = 0.0 if gamma <= 0.0 else _clip_intensity((pi_off / t) / gamma)
    target = np.diag(np.diag(sample))
    sigma = intensity * target + (1.0 - intensity) * sample
    return 0.5 * (sigma + sigma.T), intensity


def _shrink_constant_correlation(
    demeaned: np.ndarray, sample: np.ndarray, t: int, n: int
) -> tuple[np.ndarray, float]:
    # Ledoit & Wolf constant-correlation target. Variances are the sample variances;
    # every correlation is the average sample correlation. Intensity is κ / T with
    # κ = (π − ρ) / γ, matching the covariance code distributed with the paper.
    if n < 2:
        return sample.copy(), 0.0
    variances = np.diag(sample).copy()
    if np.any(variances <= 0.0):
        raise ValueError("constant-correlation shrinkage needs strictly positive variances")
    sqrt_var = np.sqrt(variances)
    outer = np.outer(sqrt_var, sqrt_var)
    corr = sample / outer
    np.fill_diagonal(corr, 1.0)
    r_bar = float((np.sum(corr) - n) / (n * (n - 1)))
    target = r_bar * outer
    np.fill_diagonal(target, variances)

    phi = _entry_variance(demeaned, sample)
    pi = float(np.sum(phi))
    # θ_ij estimates AsyCov(√T s_ii, √T s_ij) = mean(x_i³ x_j) − s_ii s_ij.
    cubes = demeaned**3
    theta = (cubes.T @ demeaned) / t - variances[:, None] * sample
    np.fill_diagonal(theta, 0.0)
    scale = sqrt_var[None, :] / sqrt_var[:, None]
    rho = float(np.sum(np.diag(phi)) + r_bar * np.sum(scale * theta))
    gamma = float(np.sum((sample - target) ** 2))
    if gamma <= 0.0:
        intensity = 0.0
    else:
        intensity = _clip_intensity(((pi - rho) / gamma) / t)
    sigma = intensity * target + (1.0 - intensity) * sample
    return 0.5 * (sigma + sigma.T), intensity
