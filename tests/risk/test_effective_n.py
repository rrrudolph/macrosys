"""Effective sample size (spec §6.4)."""

from __future__ import annotations

import numpy as np
import pytest

from macrosys.risk import effective_sample_size, overlap_adjusted_n


def test_effective_n_shrinks_under_positive_autocorrelation():
    rng = np.random.default_rng(0)
    n = 2000
    shocks = rng.normal(size=n)
    series = np.zeros(n)
    for t in range(1, n):
        series[t] = 0.8 * series[t - 1] + shocks[t]
    neff = effective_sample_size(series)
    assert neff < n
    assert neff > 1.0


def test_overlap_adjusted_n_divides_by_the_horizon():
    assert overlap_adjusted_n(252, 21) == pytest.approx(12.0)
    assert overlap_adjusted_n(100, 1) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        overlap_adjusted_n(10, 0)
