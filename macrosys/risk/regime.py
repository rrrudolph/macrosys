"""Regime indicators for factor co-movement (spec §6.3–6.4).

Rolling and EWMA correlations, bootstrap intervals, the Kritzman absorption
ratio, a two-state Hamilton variance regime, and Engle's DCC(1,1) on the factor
set. A correlation change is actionable only when two intervals do not overlap.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from macrosys.risk._bootstrap import circular_block_indices


def rolling_correlation(
    x: np.ndarray | pd.Series,
    y: np.ndarray | pd.Series,
    windows: tuple[int, ...] = (63, 126, 252),
) -> pd.DataFrame:
    """Trailing Pearson correlations at the spec windows (spec §6.3).

    Columns are the window lengths in observations (63, 126, and 252 trading days
    by default). A window with any non-finite pair is missing rather than filled,
    and the correlation is not computed from a shorter pairwise subset. The result
    is dimensionless.
    """
    left, right, index = _as_pair(x, y)
    frame = {}
    s_left = pd.Series(left, index=index)
    s_right = pd.Series(right, index=index)
    for window in windows:
        if int(window) < 3:
            raise ValueError("rolling windows must be at least 3 observations")
        frame[int(window)] = s_left.rolling(int(window), min_periods=int(window)).corr(s_right)
    return pd.DataFrame(frame, index=index)


def ewma_correlation(
    x: np.ndarray | pd.Series,
    y: np.ndarray | pd.Series,
    halflife_days: float,
) -> pd.Series:
    """Recursive EWMA correlation path (spec §6.3).

    Both series are demeaned with their full-sample means, then variances and the
    covariance follow ``λ = 2**(−1/halflife_days)`` with the RiskMetrics update
    ``v_t = λ v_{t−1} + (1−λ) z_t²``. The first observation is undefined. Non-finite
    pairs raise: a block correlation is not defined across a hole. Dimensionless.
    """
    if halflife_days <= 0:
        raise ValueError("halflife_days must be positive")
    left, right, index = _as_pair(x, y)
    if not (np.isfinite(left).all() and np.isfinite(right).all()):
        raise ValueError("ewma_correlation does not accept non-finite pairs")
    left = left - left.mean()
    right = right - right.mean()
    lam = 0.5 ** (1.0 / float(halflife_days))
    t = left.shape[0]
    out = np.full(t, np.nan, dtype=np.float64)
    var_x = float(left[0] ** 2)
    var_y = float(right[0] ** 2)
    cov = float(left[0] * right[0])
    for i in range(1, t):
        var_x = lam * var_x + (1.0 - lam) * float(left[i] ** 2)
        var_y = lam * var_y + (1.0 - lam) * float(right[i] ** 2)
        cov = lam * cov + (1.0 - lam) * float(left[i] * right[i])
        if var_x > 0.0 and var_y > 0.0:
            out[i] = cov / np.sqrt(var_x * var_y)
    return pd.Series(out, index=index, name="ewma_correlation")


def correlation_interval(
    x: np.ndarray | pd.Series,
    y: np.ndarray | pd.Series,
    n_boot: int,
    block_len: int,
    seed: int,
    level: float = 0.95,
) -> tuple[float, float]:
    """Circular block-bootstrap percentile interval for a Pearson correlation (spec §6.4).

    Pairs ``(x_t, y_t)`` are resampled together. The default interval is 95% [J].
    Non-finite values raise so that blocks stay on contiguous time.
    """
    if n_boot < 1:
        raise ValueError("n_boot must be positive")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    left, right, _index = _as_pair(x, y)
    if not (np.isfinite(left).all() and np.isfinite(right).all()):
        raise ValueError("correlation_interval does not accept non-finite pairs")
    t = left.shape[0]
    if t < 3:
        raise ValueError("need at least 3 observations")
    draws = np.empty(n_boot, dtype=np.float64)
    rng = np.random.default_rng(seed)
    for b in range(n_boot):
        idx = circular_block_indices(t, block_len, rng)
        draws[b] = _pearson(left[idx], right[idx])
    if not np.isfinite(draws).all():
        raise ValueError("bootstrap correlation was undefined; a series may be constant")
    tail = (1.0 - level) / 2.0
    low, high = np.quantile(draws, [tail, 1.0 - tail])
    return float(low), float(high)


def correlation_changed(
    a_interval: tuple[float, float], b_interval: tuple[float, float]
) -> bool:
    """True only when the two intervals do not overlap (spec §6.4).

    Touching endpoints count as overlap, so the rule does not fire on a point
    contact. That is the conservative reading of "do not overlap": acting on
    correlation noise is how a book gets churned.
    """
    a_low, a_high = (float(a_interval[0]), float(a_interval[1]))
    b_low, b_high = (float(b_interval[0]), float(b_interval[1]))
    if a_low > a_high or b_low > b_high:
        raise ValueError("each interval must be ordered (low, high)")
    return bool(a_high < b_low or b_high < a_low)


def absorption_ratio(
    returns: np.ndarray | pd.DataFrame,
    n_components: int,
    window: int | None,
) -> float | pd.Series | np.ndarray:
    """Variance share of the first ``n_components`` eigenvectors (spec §6.3).

    Kritzman, Li, Page & Rigobon (2011): the absorption ratio is the fraction of
    total variance explained by a fixed number of principal components, computed
    here from the eigenvalues of the sample covariance (divisor ``T``). A rising
    ratio means diversification is evaporating. ``window is None`` returns one
    float for the whole sample after listwise deletion. An integer window returns
    the trailing ratio, with incomplete windows left missing rather than filled.
    """
    if isinstance(returns, pd.DataFrame):
        values = returns.to_numpy(dtype=np.float64)
        index = returns.index
    else:
        values = np.asarray(returns, dtype=np.float64)
        index = None
        if values.ndim != 2:
            raise ValueError("returns must be T×N")
    t, n = values.shape
    if n_components < 1 or n_components > n:
        raise ValueError("n_components must be between 1 and N")
    if window is None:
        keep = np.isfinite(values).all(axis=1)
        block = values[keep]
        if block.shape[0] < 2:
            raise ValueError("need at least 2 complete rows")
        return _absorption(block, n_components)
    if window < 2:
        raise ValueError("window must be at least 2")
    out = np.full(t, np.nan, dtype=np.float64)
    for end in range(window - 1, t):
        block = values[end - window + 1 : end + 1]
        if np.isfinite(block).all():
            out[end] = _absorption(block, n_components)
    if index is not None:
        return pd.Series(out, index=index, name="absorption_ratio")
    return out


@dataclass(frozen=True)
class VarianceRegimeProbabilities:
    """Filtered and smoothed probabilities of the high-variance state."""

    filtered: np.ndarray
    smoothed: np.ndarray


@dataclass(frozen=True)
class VarianceRegimeParams:
    """Fitted two-state variance model. Variances are in return-squared units."""

    mean: float
    variance_low: float
    variance_high: float
    p_stay_low: float
    p_stay_high: float
    loglik: float
    n_iter: int
    converged: bool


def two_state_variance_regime(
    returns_1d: np.ndarray | pd.Series,
    max_iter: int,
    tol: float,
) -> tuple[VarianceRegimeProbabilities, VarianceRegimeParams]:
    """Two-state Markov-switching variance, common mean (spec §6.3; Hamilton 1989).

    The equity-factor calm/stress flag. Conditional on the state, returns are
    Gaussian with one mean and two variances; the state is a two-point Markov
    chain. Parameters are fit by EM (Hamilton 1990). Each E-step runs the Hamilton
    filter and the Kim (1994) smoother. The returned probabilities are for the
    high-variance state, filtered (information up to t) and smoothed (full sample).
    ``tol`` is the maximum of the relative mean change, the log-variance change,
    and the transition-probability change. Non-finite observations raise, because
    dropping them would splice the chain.
    """
    y = np.asarray(returns_1d, dtype=np.float64).ravel()
    if y.ndim != 1:
        raise ValueError("returns_1d must be one series")
    if not np.isfinite(y).all():
        raise ValueError("two_state_variance_regime does not accept non-finite values")
    if y.shape[0] < 30:
        raise ValueError("need at least 30 observations")
    if max_iter < 1:
        raise ValueError("max_iter must be positive")
    if tol <= 0:
        raise ValueError("tol must be positive")

    theta = _regime_init(y)
    converged = False
    n_iter = 0
    while n_iter < max_iter:
        n_iter += 1
        _filtered, smoothed, joint, _ll = _hamilton_pass(y, theta)
        updated = _m_step(y, smoothed, joint, theta)
        if _theta_distance(theta, updated) < tol:
            theta = updated
            converged = True
            break
        theta = updated
    filtered, smoothed, _joint, loglik = _hamilton_pass(y, theta)
    probabilities = VarianceRegimeProbabilities(filtered=filtered, smoothed=smoothed)
    params = VarianceRegimeParams(
        mean=theta.mean,
        variance_low=theta.var_low,
        variance_high=theta.var_high,
        p_stay_low=theta.p_ll,
        p_stay_high=theta.p_hh,
        loglik=loglik,
        n_iter=n_iter,
        converged=converged,
    )
    return probabilities, params


@dataclass(frozen=True)
class DCCResult:
    """Engle (2002) DCC(1,1) path. Correlations are dimensionless; variances are return-squared."""

    correlation: np.ndarray
    conditional_variance: np.ndarray
    standardized_residuals: np.ndarray
    a: float
    b: float
    garch_omega: np.ndarray
    garch_alpha: np.ndarray
    garch_beta: np.ndarray


def dcc(returns: np.ndarray | pd.DataFrame, *, max_iter: int = 80) -> DCCResult:
    """DCC(1,1) by two-step QMLE for the factor set (spec §6.3; Engle 2002).

    Step one fits a variance-targeted Gaussian GARCH(1,1) to each demeaned column
    (``ω = σ² (1 − α − β)`` [J], which keeps the unconditional variance on the sample
    variance and leaves a two-parameter line search). Step two maximizes the DCC
    quasi-likelihood in ``(a, b)`` with ``Q_t = (1−a−b) S + a u_{t−1} u_{t−1}' + b Q_{t−1}``
    and ``R_t`` the correlation form of ``Q_t``. ``S`` is the sample covariance of
    the standardized residuals. Incomplete rows are dropped listwise. The returned
    ``correlation`` array has shape ``(T, N, N)``.
    """
    values = _dcc_matrix(returns)
    centered = values - values.mean(axis=0)
    t, n = centered.shape
    if n < 2:
        raise ValueError("DCC needs at least two series")
    variances = np.empty((t, n), dtype=np.float64)
    omega = np.empty(n, dtype=np.float64)
    alpha = np.empty(n, dtype=np.float64)
    beta = np.empty(n, dtype=np.float64)
    for j in range(n):
        sig2, w, a_j, b_j = _fit_garch11(centered[:, j], max_iter=max_iter)
        variances[:, j] = sig2
        omega[j] = w
        alpha[j] = a_j
        beta[j] = b_j
    standardized = centered / np.sqrt(variances)
    s = (standardized.T @ standardized) / t
    s = 0.5 * (s + s.T)
    a_hat, b_hat = _fit_dcc_ab(standardized, s, max_iter=max_iter)
    correlation = _dcc_path(standardized, a_hat, b_hat, s)
    return DCCResult(
        correlation=correlation,
        conditional_variance=variances,
        standardized_residuals=standardized,
        a=a_hat,
        b=b_hat,
        garch_omega=omega,
        garch_alpha=alpha,
        garch_beta=beta,
    )


# --- correlation primitives -----------------------------------------------------------------------


def _as_pair(
    x: np.ndarray | pd.Series, y: np.ndarray | pd.Series
) -> tuple[np.ndarray, np.ndarray, pd.Index]:
    if isinstance(x, pd.Series) and isinstance(y, pd.Series):
        joined = pd.concat([x.rename("_x"), y.rename("_y")], axis=1, join="inner")
        return (
            joined["_x"].to_numpy(dtype=np.float64),
            joined["_y"].to_numpy(dtype=np.float64),
            joined.index,
        )
    left = np.asarray(x, dtype=np.float64).ravel()
    right = np.asarray(y, dtype=np.float64).ravel()
    if left.shape != right.shape:
        raise ValueError("x and y must have the same length")
    return left, right, pd.RangeIndex(left.shape[0])


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    xc = x - x.mean()
    yc = y - y.mean()
    ss_x = float(np.dot(xc, xc))
    ss_y = float(np.dot(yc, yc))
    if ss_x <= 0.0 or ss_y <= 0.0:
        return float("nan")
    return float(np.dot(xc, yc) / np.sqrt(ss_x * ss_y))


def _absorption(block: np.ndarray, n_components: int) -> float:
    centered = block - block.mean(axis=0)
    cov = (centered.T @ centered) / centered.shape[0]
    evals = np.linalg.eigvalsh(0.5 * (cov + cov.T))
    evals = np.maximum(evals, 0.0)
    total = float(evals.sum())
    if total <= 0.0:
        return float("nan")
    return float(evals[-n_components:].sum() / total)


# --- Hamilton / Kim -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Theta:
    mean: float
    var_low: float
    var_high: float
    p_ll: float
    p_hh: float


def _regime_init(y: np.ndarray) -> _Theta:
    """Moment start: a 21-day rolling variance split, then empirical transitions."""
    mu = float(y.mean())
    sq = (y - mu) ** 2
    roll = 21
    csum = np.cumsum(sq)
    rolling = np.empty_like(sq)
    for t in range(y.shape[0]):
        start = max(0, t - roll + 1)
        prev = 0.0 if start == 0 else csum[start - 1]
        rolling[t] = (csum[t] - prev) / (t - start + 1)
    high = rolling >= np.quantile(rolling, 0.75)
    if high.all() or not np.any(high):
        high = sq >= np.quantile(sq, 0.75)
    var_high = float(np.mean(sq[high]))
    var_low = float(np.mean(sq[~high]))
    if var_high < var_low:
        var_low, var_high = var_high, var_low
        high = ~high
    floor = max(1e-12, 1e-8 * float(np.var(y)))
    var_low = max(var_low, floor)
    var_high = max(var_high, floor)
    from_low = int(np.sum(~high[:-1]))
    from_high = int(np.sum(high[:-1]))
    stay_low = int(np.sum(~high[:-1] & ~high[1:]))
    stay_high = int(np.sum(high[:-1] & high[1:]))
    p_ll = 0.9 if from_low == 0 else stay_low / from_low
    p_hh = 0.9 if from_high == 0 else stay_high / from_high
    return _order_theta(mu, var_low, var_high, float(np.clip(p_ll, 1e-3, 1 - 1e-3)), float(np.clip(p_hh, 1e-3, 1 - 1e-3)))


def _order_theta(mean: float, var_low: float, var_high: float, p_ll: float, p_hh: float) -> _Theta:
    if var_high < var_low:
        var_low, var_high = var_high, var_low
        p_ll, p_hh = p_hh, p_ll
    return _Theta(mean=mean, var_low=var_low, var_high=var_high, p_ll=p_ll, p_hh=p_hh)


def _transition(theta: _Theta) -> np.ndarray:
    return np.array(
        [[theta.p_ll, 1.0 - theta.p_ll], [1.0 - theta.p_hh, theta.p_hh]],
        dtype=np.float64,
    )


def _stationary(theta: _Theta) -> np.ndarray:
    # π_low = p_hl / (p_hl + p_lh)
    denom = (1.0 - theta.p_hh) + (1.0 - theta.p_ll)
    pi_low = (1.0 - theta.p_hh) / denom
    return np.array([pi_low, 1.0 - pi_low], dtype=np.float64)


def _gauss(obs: float, mean: float, var: float) -> float:
    return float(np.exp(-0.5 * (obs - mean) ** 2 / var) / np.sqrt(2.0 * np.pi * var))


def _hamilton_pass(
    y: np.ndarray, theta: _Theta
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return filtered P(high), smoothed P(high), joint transitions, log likelihood."""
    t = y.shape[0]
    trans = _transition(theta)
    predicted = np.zeros((t, 2), dtype=np.float64)
    filtered = np.zeros((t, 2), dtype=np.float64)
    loglik = 0.0
    pred = _stationary(theta)
    for i in range(t):
        predicted[i] = pred
        density = np.array(
            [_gauss(float(y[i]), theta.mean, theta.var_low), _gauss(float(y[i]), theta.mean, theta.var_high)]
        )
        joint = pred * density
        evidence = float(joint.sum())
        if evidence <= 0.0 or not np.isfinite(evidence):
            filtered[i] = pred
            loglik += -700.0
        else:
            filtered[i] = joint / evidence
            loglik += float(np.log(evidence))
        pred = filtered[i] @ trans

    smoothed = np.zeros((t, 2), dtype=np.float64)
    smoothed[-1] = filtered[-1]
    for i in range(t - 2, -1, -1):
        ratio = _safe_ratio(smoothed[i + 1], predicted[i + 1])
        smoothed[i] = filtered[i] * (trans @ ratio)
        total = float(smoothed[i].sum())
        if total > 0.0 and np.isfinite(total):
            smoothed[i] /= total
        else:
            smoothed[i] = filtered[i]

    joint_tt = np.zeros((t - 1, 2, 2), dtype=np.float64)
    for i in range(t - 1):
        ratio = _safe_ratio(smoothed[i + 1], predicted[i + 1])
        for src in range(2):
            for dst in range(2):
                joint_tt[i, src, dst] = filtered[i, src] * trans[src, dst] * ratio[dst]
        total = float(joint_tt[i].sum())
        if total > 0.0 and np.isfinite(total):
            joint_tt[i] /= total
    return filtered[:, 1].copy(), smoothed[:, 1].copy(), joint_tt, loglik


def _safe_ratio(smoothed_row: np.ndarray, predicted_row: np.ndarray) -> np.ndarray:
    ratio = np.zeros(2, dtype=np.float64)
    ok = predicted_row > 1e-12
    ratio[ok] = smoothed_row[ok] / predicted_row[ok]
    return ratio


def _m_step(y: np.ndarray, smoothed_high: np.ndarray, joint: np.ndarray, theta: _Theta) -> _Theta:
    # EM M-step: weight the common mean by the E-step variances, then update
    # variances around that mean. Hamilton (1990).
    g_high = smoothed_high
    g_low = 1.0 - smoothed_high
    precision = g_low / theta.var_low + g_high / theta.var_high
    mean = float(np.sum(precision * y) / np.sum(precision))
    e2 = (y - mean) ** 2
    floor = max(1e-12, 1e-8 * float(np.var(y)))
    var_low = max(float(np.sum(g_low * e2) / max(float(np.sum(g_low)), 1e-12)), floor)
    var_high = max(float(np.sum(g_high * e2) / max(float(np.sum(g_high)), 1e-12)), floor)
    from_low = float(np.sum(joint[:, 0, :]))
    from_high = float(np.sum(joint[:, 1, :]))
    p_ll = float(np.sum(joint[:, 0, 0]) / from_low) if from_low > 0 else 0.9
    p_hh = float(np.sum(joint[:, 1, 1]) / from_high) if from_high > 0 else 0.9
    p_ll = float(np.clip(p_ll, 1e-3, 1.0 - 1e-3))
    p_hh = float(np.clip(p_hh, 1e-3, 1.0 - 1e-3))
    return _order_theta(mean, var_low, var_high, p_ll, p_hh)


def _theta_distance(old: _Theta, new: _Theta) -> float:
    mean_scale = 1.0 + abs(old.mean)
    return float(
        max(
            abs(new.mean - old.mean) / mean_scale,
            abs(np.log(new.var_low) - np.log(old.var_low)),
            abs(np.log(new.var_high) - np.log(old.var_high)),
            abs(new.p_ll - old.p_ll),
            abs(new.p_hh - old.p_hh),
        )
    )


# --- DCC ------------------------------------------------------------------------------------------


def _dcc_matrix(returns: np.ndarray | pd.DataFrame) -> np.ndarray:
    values = returns.to_numpy(dtype=np.float64) if isinstance(returns, pd.DataFrame) else np.asarray(returns, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("returns must be T×N")
    keep = np.isfinite(values).all(axis=1)
    values = values[keep]
    if values.shape[0] < 30:
        raise ValueError("DCC needs at least 30 complete rows")
    return values


def _garch_path(r: np.ndarray, alpha: float, beta: float, unconditional: float) -> np.ndarray | None:
    if alpha < 0.0 or beta < 0.0 or alpha + beta >= 0.999:
        return None
    omega = unconditional * (1.0 - alpha - beta)
    if omega <= 0.0:
        return None
    sig2 = np.empty(r.shape[0], dtype=np.float64)
    sig2[0] = unconditional
    for i in range(1, r.shape[0]):
        sig2[i] = omega + alpha * r[i - 1] ** 2 + beta * sig2[i - 1]
        if not np.isfinite(sig2[i]) or sig2[i] <= 1e-18:
            return None
    return sig2


def _garch_nll(r: np.ndarray, alpha: float, beta: float, unconditional: float) -> float:
    sig2 = _garch_path(r, alpha, beta, unconditional)
    if sig2 is None:
        return 1e12
    return float(0.5 * np.sum(np.log(sig2) + r**2 / sig2))


def _fit_garch11(r: np.ndarray, max_iter: int) -> tuple[np.ndarray, float, float, float]:
    unconditional = float(np.dot(r, r) / r.shape[0])
    if unconditional <= 0.0:
        raise ValueError("a DCC series has zero variance")
    starts = ((0.05, 0.90), (0.10, 0.80), (0.02, 0.95), (0.15, 0.70))
    best_ab = np.array([0.05, 0.90])
    best_val = _garch_nll(r, best_ab[0], best_ab[1], unconditional)
    for a0, b0 in starts:
        res = minimize(
            lambda ab, _r=r, _u=unconditional: _garch_nll(_r, float(ab[0]), float(ab[1]), _u),
            np.array([a0, b0], dtype=np.float64),
            method="L-BFGS-B",
            bounds=[(0.0, 0.4), (0.0, 0.98)],
            options={"maxiter": max_iter, "ftol": 1e-10},
        )
        val = float(res.fun)
        if val < best_val and _garch_path(r, float(res.x[0]), float(res.x[1]), unconditional) is not None:
            best_val = val
            best_ab = np.asarray(res.x, dtype=np.float64)
    alpha = float(best_ab[0])
    beta = float(best_ab[1])
    if alpha + beta >= 0.999:
        alpha, beta = 0.05, 0.90
    sig2 = _garch_path(r, alpha, beta, unconditional)
    if sig2 is None:
        raise RuntimeError("GARCH(1,1) QMLE failed")
    omega = unconditional * (1.0 - alpha - beta)
    return sig2, float(omega), alpha, beta


def _dcc_path(u: np.ndarray, a: float, b: float, q_bar: np.ndarray) -> np.ndarray:
    t, n = u.shape
    out = np.empty((t, n, n), dtype=np.float64)
    q = q_bar.copy()
    for i in range(t):
        diag = np.maximum(np.diag(q), 1e-18)
        inv = 1.0 / np.sqrt(diag)
        corr = q * np.outer(inv, inv)
        corr = 0.5 * (corr + corr.T)
        np.fill_diagonal(corr, 1.0)
        out[i] = corr
        outer = np.outer(u[i], u[i])
        q = (1.0 - a - b) * q_bar + a * outer + b * q
        q = 0.5 * (q + q.T)
    return out


def _dcc_nll(u: np.ndarray, a: float, b: float, q_bar: np.ndarray) -> float:
    if a < 0.0 or b < 0.0 or a + b >= 0.999:
        return 1e12
    t, _n = u.shape
    q = q_bar.copy()
    total = 0.0
    for i in range(t):
        diag = np.maximum(np.diag(q), 1e-18)
        inv = 1.0 / np.sqrt(diag)
        corr = q * np.outer(inv, inv)
        corr = 0.5 * (corr + corr.T)
        np.fill_diagonal(corr, 1.0)
        sign, logdet = np.linalg.slogdet(corr)
        if sign <= 0.0 or not np.isfinite(logdet):
            return 1e12
        try:
            solved = np.linalg.solve(corr, u[i])
        except np.linalg.LinAlgError:
            return 1e12
        total += 0.5 * (logdet + float(np.dot(u[i], solved)))
        outer = np.outer(u[i], u[i])
        q = (1.0 - a - b) * q_bar + a * outer + b * q
        q = 0.5 * (q + q.T)
    return float(total)


def _fit_dcc_ab(u: np.ndarray, q_bar: np.ndarray, max_iter: int) -> tuple[float, float]:
    grid = []
    for a0 in (0.01, 0.02, 0.05, 0.10):
        for b0 in (0.80, 0.90, 0.95, 0.97):
            if a0 + b0 < 0.998:
                grid.append((a0, b0))
    best = min(grid, key=lambda ab: _dcc_nll(u, ab[0], ab[1], q_bar))
    res = minimize(
        lambda ab, _u=u, _q=q_bar: _dcc_nll(_u, float(ab[0]), float(ab[1]), _q),
        np.array(best, dtype=np.float64),
        method="SLSQP",
        bounds=[(0.0, 0.5), (0.0, 0.998)],
        constraints={"type": "ineq", "fun": lambda ab: 0.998 - float(ab[0]) - float(ab[1])},
        options={"maxiter": max_iter, "ftol": 1e-8},
    )
    a_hat, b_hat = (float(res.x[0]), float(res.x[1])) if np.isfinite(res.fun) else best
    if a_hat < 0.0 or b_hat < 0.0 or a_hat + b_hat >= 0.999:
        a_hat, b_hat = best
    if _dcc_nll(u, a_hat, b_hat, q_bar) >= 1e12:
        raise RuntimeError("DCC(1,1) QMLE failed")
    return a_hat, b_hat
