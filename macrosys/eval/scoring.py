"""Forecast scores, deflated Sharpe, and purged walk-forward splits (spec §10.1, §10.2).

Proper scores follow Gneiting & Raftery (2007): the Brier score here is the mean squared error
(lower is better) and the logarithmic score is the mean log probability of the outcome (higher is
better). The probabilistic and deflated Sharpe ratios follow Bailey & López de Prado (2014). Purging
and the embargo follow López de Prado (2018): a training label that reaches into the test window is
dropped, and a strip of observations after the test window is dropped so serial correlation cannot leak.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
import pandas as pd
from scipy.stats import norm, spearmanr

# Euler–Mascheroni constant γ in Bailey & López de Prado (2014). Float64 value; SciPy 1.18 no longer
# exports it from scipy.constants.
_EULER_MASCHERONI = 0.5772156649015329

ArrayLike = np.ndarray | list[float] | pd.Series


def brier(probs: ArrayLike, outcomes: ArrayLike) -> float:
    """Mean squared difference between probabilities and 0/1 outcomes (Brier; Gneiting & Raftery 2007).

    Lower is better. ``probs`` and ``outcomes`` are matched elementwise; this is the binary score.
    Multi-category rows are summed inside ``ForecastLedger.score`` before they reach this helper.
    """
    predicted, realized = _pair(probs, outcomes)
    return float(np.mean((predicted - realized) ** 2))


def log_score(probs: ArrayLike, outcomes: ArrayLike) -> float:
    """Mean logarithmic score, positively oriented (Gneiting & Raftery 2007).

    For a binary outcome this is ``mean(y log p + (1 - y) log(1 - p))``. Probabilities are clipped
    away from 0 and 1 so a single certain miss does not return negative infinity.
    """
    predicted, realized = _pair(probs, outcomes)
    clipped = np.clip(predicted, 1e-15, 1.0 - 1e-15)
    terms = realized * np.log(clipped) + (1.0 - realized) * np.log(1.0 - clipped)
    return float(np.mean(terms))


def reliability_table(probs: ArrayLike, outcomes: ArrayLike, bins: int = 10) -> pd.DataFrame:
    """Bin predicted probabilities and compare them with the realized frequency (spec §10.1 item 5).

    Columns are ``bin``, ``low``, ``high``, ``count``, ``mean_predicted``, and ``mean_outcome``.
    Empty bins have NaN means. The last edge is inclusive of 1.
    """
    if bins < 1:
        raise ValueError("bins must be positive")
    predicted, realized = _pair(probs, outcomes)
    edges = np.linspace(0.0, 1.0, bins + 1)
    indexes = np.digitize(predicted, edges[1:-1], right=False)
    rows: list[dict[str, float | int]] = []
    for bin_index in range(bins):
        mask = indexes == bin_index
        count = int(mask.sum())
        rows.append(
            {
                "bin": bin_index,
                "low": float(edges[bin_index]),
                "high": float(edges[bin_index + 1]),
                "count": count,
                "mean_predicted": float(predicted[mask].mean()) if count else float("nan"),
                "mean_outcome": float(realized[mask].mean()) if count else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def sign_accuracy(predicted: ArrayLike, realized: ArrayLike) -> float:
    """Fraction of pairs whose signs agree, including zeros (spec §10.1 held-out case scoring)."""
    left, right = _pair(predicted, realized)
    return float(np.mean(np.sign(left) == np.sign(right)))


def rank_correlation(predicted: ArrayLike, realized: ArrayLike) -> float:
    """Spearman rank correlation of magnitudes (spec §10.1; SciPy's ``spearmanr``)."""
    left, right = _pair(predicted, realized)
    if len(left) < 2:
        raise ValueError("rank correlation needs at least two observations")
    coefficient = spearmanr(left, right).statistic
    return float(coefficient)


def interval_coverage(realized: ArrayLike, low: ArrayLike, high: ArrayLike) -> float:
    """Fraction of outcomes that fall inside the declared interval (spec §10.2 ledger scoring)."""
    outcomes = _as_float(realized)
    lower = _as_float(low)
    upper = _as_float(high)
    if not (len(outcomes) == len(lower) == len(upper)):
        raise ValueError("realized, low, and high must have the same length")
    if len(outcomes) == 0:
        raise ValueError("interval_coverage needs at least one observation")
    if np.any(lower > upper):
        raise ValueError("interval low cannot exceed high")
    inside = (outcomes >= lower) & (outcomes <= upper)
    return float(np.mean(inside))


def probabilistic_sharpe_ratio(sr: float, n: int, skew: float, kurt: float, sr_benchmark: float) -> float:
    """Probability that the true Sharpe exceeds ``sr_benchmark`` (Bailey & López de Prado 2014).

    ``kurt`` is the non-excess fourth moment (a Gaussian sample has kurtosis 3). ``n`` is the number
    of return observations. The statistic is

    ``(sr - sr*) * sqrt(n - 1) / sqrt(1 - skew * sr + (kurt - 1) / 4 * sr^2)``

    and the return value is the standard-normal CDF of that statistic.
    """
    if n <= 1:
        raise ValueError("probabilistic Sharpe ratio needs n > 1")
    inside = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if inside <= 0.0:
        raise ValueError("Sharpe variance estimate is not positive; check skew and kurtosis")
    statistic = (sr - sr_benchmark) * math.sqrt(n - 1) / math.sqrt(inside)
    return float(norm.cdf(statistic))


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """Expected maximum Sharpe under the null, given ``n_trials`` (Bailey & López de Prado 2014).

    For ``N > 1`` and positive variance the paper's Euler–Mascheroni approximation is

    ``sqrt(V) * ((1 - γ) Φ^{-1}(1 - 1/N) + γ Φ^{-1}(1 - 1/(N e)))``

    with ``γ`` the Euler–Mascheroni constant. A single trial has no selection bias, and a zero
    cross-trial variance has no dispersion, so both cases return 0. That is what makes the deflated
    Sharpe equal the probabilistic Sharpe benchmarked at 0 when ``n_trials`` is 1 and ``var_sr`` is 0.
    """
    if n_trials < 1:
        raise ValueError("n_trials must be at least 1")
    if var_sr < 0:
        raise ValueError("var_sr cannot be negative")
    if n_trials == 1 or var_sr == 0.0:
        return 0.0
    trials = float(n_trials)
    z_one = float(norm.ppf(1.0 - 1.0 / trials))
    z_two = float(norm.ppf(1.0 - 1.0 / (trials * math.e)))
    return math.sqrt(var_sr) * ((1.0 - _EULER_MASCHERONI) * z_one + _EULER_MASCHERONI * z_two)


def deflated_sharpe_ratio(
    sr_observed: float,
    n_trials: int,
    var_sr_trials: float,
    n: int,
    skew: float,
    kurt: float,
) -> float:
    """Probabilistic Sharpe ratio benchmarked at the expected maximum Sharpe (Bailey & López de Prado 2014).

    The benchmark is ``expected_max_sharpe(n_trials, var_sr_trials)``. With one trial and zero
    cross-trial variance that benchmark is 0, so this equals ``probabilistic_sharpe_ratio(..., 0)``.
    """
    benchmark = expected_max_sharpe(n_trials, var_sr_trials)
    return probabilistic_sharpe_ratio(sr_observed, n, skew, kurt, benchmark)


def purged_walk_forward_splits(
    n_obs: int,
    n_splits: int,
    embargo: int,
    label_horizon: int,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Contiguous test folds with purging and an embargo (López de Prado 2018; spec §10.2).

    The sample ``0 .. n_obs-1`` is partitioned into ``n_splits`` contiguous test blocks, in time order.
    For each block the training set is every other index except two strips: the ``label_horizon``
    observations immediately before the test (their forward labels overlap the test) and the
    ``embargo`` observations immediately after the test. Training may include later folds; the embargo
    is what keeps that later data from sitting on the test boundary. Indexes are ``int64`` arrays.
    """
    if n_obs < 1:
        raise ValueError("n_obs must be positive")
    if n_splits < 1 or n_splits > n_obs:
        raise ValueError("n_splits must be between 1 and n_obs")
    if embargo < 0 or label_horizon < 0:
        raise ValueError("embargo and label_horizon cannot be negative")
    sizes = _fold_sizes(n_obs, n_splits)
    edges = [0]
    for size in sizes:
        edges.append(edges[-1] + size)
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for start, end in pairwise(edges):
        test = np.arange(start, end, dtype=np.int64)
        purge_lo = start - label_horizon
        embargo_hi = end + embargo
        train = np.array(
            [index for index in range(n_obs) if index < purge_lo or index >= embargo_hi],
            dtype=np.int64,
        )
        splits.append((train, test))
    return splits


def _fold_sizes(n_obs: int, n_splits: int) -> list[int]:
    base, remainder = divmod(n_obs, n_splits)
    return [base + (1 if index < remainder else 0) for index in range(n_splits)]


def _as_float(values: ArrayLike) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValueError("expected a one-dimensional sequence")
    return array


def _pair(left: ArrayLike, right: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
    first, second = _as_float(left), _as_float(right)
    if len(first) != len(second):
        raise ValueError("inputs must have the same length")
    if len(first) == 0:
        raise ValueError("score needs at least one observation")
    return first, second
