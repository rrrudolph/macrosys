"""Factor model, bootstrap intervals, residual clusters (spec §6.2, §6.4)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from macrosys.risk import FactorModel, bootstrap_loadings, residual_clusters


def test_factor_model_recovers_known_loadings():
    rng = np.random.default_rng(42)
    t = 4000
    factors = rng.normal(size=(t, 2))
    true = np.array([[1.0, 0.0], [0.5, -0.3], [0.0, 1.2]])
    assets = factors @ true.T + rng.normal(scale=0.2, size=(t, 3))
    idx = pd.bdate_range("2010-01-01", periods=t)
    asset_df = pd.DataFrame(assets, index=idx, columns=["A", "B", "C"])
    factor_df = pd.DataFrame(factors, index=idx, columns=["eq", "usd"])
    model = FactorModel().fit(asset_df, factor_df, ridge=0.0, window=None)
    assert model.loadings_.shape == (3, 2)
    assert np.allclose(model.loadings_.to_numpy(), true, atol=0.05)
    assert model.r_squared_.min() > 0.8
    exposure = model.exposures({"A": 2.0, "B": -1.0})
    expected = model.loadings_.loc["A"] * 2.0 + model.loadings_.loc["B"] * -1.0
    assert np.allclose(exposure.to_numpy(), expected.to_numpy())


def test_residual_clusters_are_components_above_the_threshold():
    corr = pd.DataFrame(
        [
            [1.0, 0.9, 0.1, 0.0],
            [0.9, 1.0, 0.2, 0.0],
            [0.1, 0.2, 1.0, 0.8],
            [0.0, 0.0, 0.8, 1.0],
        ],
        index=["A", "B", "C", "D"],
        columns=["A", "B", "C", "D"],
    )
    clusters = residual_clusters(corr, threshold=0.5)
    assert clusters == [{"A", "B"}, {"C", "D"}]
    assert residual_clusters(corr, threshold=0.95) == []


def test_bootstrap_intervals_cover_the_true_loading_at_a_loose_rate():
    # 95% intervals over many seeds. Block bootstrap coverage is noisy in a
    # short Monte Carlo, so the bar is well below the nominal rate.
    n_seeds = 24
    hits = 0
    true_beta = 1.5
    for seed in range(n_seeds):
        rng = np.random.default_rng(1000 + seed)
        t = 400
        factor = rng.normal(size=(t, 1))
        asset = true_beta * factor[:, 0] + rng.normal(scale=1.0, size=t)
        idx = pd.RangeIndex(t)
        asset_df = pd.DataFrame({"A": asset}, index=idx)
        factor_df = pd.DataFrame({"f": factor[:, 0]}, index=idx)
        low, high = bootstrap_loadings(
            asset_df, factor_df, ridge=0.0, window=None, n_boot=80, block_len=5, seed=seed
        )
        if float(low.loc["A", "f"]) <= true_beta <= float(high.loc["A", "f"]):
            hits += 1
    coverage = hits / n_seeds
    assert coverage >= 0.70, coverage
