"""Scores, deflated Sharpe, and purged splits (Bailey & López de Prado 2014; López de Prado 2018)."""

from __future__ import annotations

import numpy as np
import pytest

from macrosys.eval.scoring import (
    brier,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    interval_coverage,
    log_score,
    probabilistic_sharpe_ratio,
    purged_walk_forward_splits,
    rank_correlation,
    reliability_table,
    sign_accuracy,
)


def test_brier_log_sign_rank_coverage_and_reliability() -> None:
    assert brier([1.0, 0.0], [1.0, 0.0]) == pytest.approx(0.0)
    assert brier([0.5], [1.0]) == pytest.approx(0.25)
    perfect = log_score([1.0], [1.0])
    coin = log_score([0.5], [1.0])
    assert perfect > coin
    assert sign_accuracy([1.0, -1.0], [2.0, 3.0]) == pytest.approx(0.5)
    assert rank_correlation([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    assert interval_coverage([0.0, 5.0], [0.0, 0.0], [1.0, 4.0]) == pytest.approx(0.5)
    table = reliability_table([0.1, 0.9], [0.0, 1.0], bins=2)
    assert list(table.columns) == ["bin", "low", "high", "count", "mean_predicted", "mean_outcome"]
    assert int(table["count"].sum()) == 2


def test_deflated_sharpe_equals_psr_for_one_trial_and_grows_with_trials() -> None:
    sr, n, skew, kurt = 0.8, 80, -0.2, 3.5
    deflated = deflated_sharpe_ratio(sr, n_trials=1, var_sr_trials=0.0, n=n, skew=skew, kurt=kurt)
    probabilistic = probabilistic_sharpe_ratio(sr, n, skew, kurt, sr_benchmark=0.0)
    assert deflated == pytest.approx(probabilistic)
    assert expected_max_sharpe(1, 0.2) == 0.0
    assert expected_max_sharpe(5, 0.0) == 0.0
    assert expected_max_sharpe(30, 0.04) > expected_max_sharpe(3, 0.04)


def test_purged_splits_have_no_leakage() -> None:
    n_obs, n_splits, embargo, horizon = 100, 5, 4, 6
    splits = purged_walk_forward_splits(n_obs, n_splits, embargo, horizon)
    assert len(splits) == n_splits
    covered: list[int] = []
    for train, test in splits:
        assert len(np.intersect1d(train, test)) == 0
        start, end = int(test.min()), int(test.max())
        assert np.array_equal(test, np.arange(start, end + 1))
        for index in train:
            if index < start:
                assert start - index > horizon
            if index > end:
                assert index - end > embargo
        covered.extend(int(index) for index in test)
    assert covered == list(range(n_obs))
