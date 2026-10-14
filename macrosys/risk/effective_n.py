"""Autocorrelation-adjusted sample size (spec §6.4).

Every reported correlation is paired with an effective sample size so a short
dependent window is not treated as if its observations were independent.
"""

from __future__ import annotations

import numpy as np


def effective_sample_size(x: np.ndarray) -> float:
    """Effective n of a series, ``n / (1 + 2 Σ_k w_k ρ_k)`` (spec §6.4).

    Autocorrelations use the usual ``1/n`` divisor. The sum is truncated at the
    Newey–West (1994) plug-in lag ``floor(4 (n/100)**(2/9))`` and weighted by the
    Bartlett kernel ``w_k = 1 − k/(m+1)``, which is the Newey–West kernel. A
    non-positive denominator (the truncated sum is too negative to be a sample
    size) returns ``n`` rather than a negative or infinite value. The result is a
    count of observations, not a return.
    """
    series = np.asarray(x, dtype=np.float64).ravel()
    series = series[np.isfinite(series)]
    n = int(series.shape[0])
    if n < 3:
        raise ValueError("effective sample size needs at least 3 finite observations")
    centered = series - series.mean()
    gamma0 = float(np.dot(centered, centered) / n)
    if gamma0 <= 0.0:
        return float(n)
    lag = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lag = max(1, min(lag, n - 1))
    weighted = 0.0
    for k in range(1, lag + 1):
        gamma_k = float(np.dot(centered[k:], centered[:-k]) / n)
        rho = gamma_k / gamma0
        weight = 1.0 - k / (lag + 1.0)
        weighted += weight * rho
    denom = 1.0 + 2.0 * weighted
    if denom <= 1e-8:
        return float(n)
    return float(n / denom)


def overlap_adjusted_n(n: float, horizon: int) -> float:
    """Effective size of overlapping ``horizon``-period returns (spec §6.4).

    If one-period shocks are uncorrelated, overlapping h-period sums have
    ``Corr(r_t, r_{t−k}) = (h − k) / h`` for ``k < h``. Then
    ``1 + 2 Σ_{k=1}^{h−1} ρ_k = h``, so the effective count is ``n / horizon``.
    ``horizon`` is in the same observation units as ``n`` (for example 21 when
    ``n`` counts overlapping 21-day returns built from daily data).
    """
    if horizon < 1:
        raise ValueError("horizon must be a positive integer")
    if not np.isfinite(n) or n <= 0:
        raise ValueError("n must be a positive finite count")
    return float(n) / float(horizon)
