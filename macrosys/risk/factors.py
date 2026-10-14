"""Curated linear factor model (spec §6.2, §11.4–11.5).

Loadings are the hedge ratios of §11.5: ridge regression of asset returns on a
small set of observable factors, with an unpenalized intercept. Residual
correlations are kept so a cluster the factors miss (shared issuer, roll, country)
can be treated as one position for limits.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from macrosys.risk._bootstrap import circular_block_indices


class FactorModel:
    """Rolling or full-sample factor regression with ridge shrinkage (spec §6.2).

    ``ridge`` is the L2 penalty added to ``F'F`` (intercept not penalized). ``ridge=0``
    is OLS. ``window`` keeps the last that many complete rows after date alignment;
    ``None`` uses the whole overlap. Returns are decimal simple returns.
    """

    def __init__(self) -> None:
        self.loadings_: pd.DataFrame | None = None
        self.residual_variance_: pd.Series | None = None
        self.residual_correlation_: pd.DataFrame | None = None
        self.r_squared_: pd.Series | None = None
        self.alpha_: pd.Series | None = None
        self.ridge_: float | None = None
        self.window_: int | None = None
        self.n_obs_: int | None = None

    def fit(
        self,
        asset_returns: pd.DataFrame,
        factor_returns: pd.DataFrame,
        ridge: float,
        window: int | None,
    ) -> FactorModel:
        """Estimate loadings, residual variances, residual correlation, and R² (spec §6.2).

        Assets and factors are inner-joined on the index and rows with any missing
        value are dropped (listwise), matching the covariance estimators. Loadings
        are an N×K frame (assets × factors). Residual variance uses ``T − K − 1``
        degrees of freedom. R² is about the asset mean and can be negative under a
        strong ridge. The residual correlation is the Pearson matrix of residuals
        and is not projected to be positive semi-definite.
        """
        assets, factors = _align(asset_returns, factor_returns, window)
        loadings, resid_var, resid, r2, alpha = _fit_arrays(
            assets.to_numpy(dtype=float), factors.to_numpy(dtype=float), ridge
        )
        asset_names = list(assets.columns)
        factor_names = list(factors.columns)
        self.loadings_ = pd.DataFrame(loadings, index=asset_names, columns=factor_names)
        self.residual_variance_ = pd.Series(resid_var, index=asset_names, name="residual_variance")
        self.residual_correlation_ = _residual_correlation(resid, asset_names)
        self.r_squared_ = pd.Series(r2, index=asset_names, name="r_squared")
        self.alpha_ = pd.Series(alpha, index=asset_names, name="alpha")
        self.ridge_ = float(ridge)
        self.window_ = window
        self.n_obs_ = int(assets.shape[0])
        return self

    def exposures(self, positions: dict[str, float]) -> pd.Series:
        """Portfolio factor exposure ``Σ_i w_i β_i`` (spec §6.2, §11.4, §11.5).

        Assets in the model but absent from ``positions`` contribute zero. Assets in
        ``positions`` that were not in the regression raise. The result is in the
        same unit as the positions: dimensionless factor exposure when positions are
        portfolio weights, notional exposure when positions are currency amounts.
        """
        if self.loadings_ is None:
            raise RuntimeError("fit the model before asking for exposures")
        return exposures_from_loadings(positions, self.loadings_)

    def bootstrap_loadings(
        self,
        asset_returns: pd.DataFrame,
        factor_returns: pd.DataFrame,
        n_boot: int,
        block_len: int,
        seed: int,
        ridge: float | None = None,
        window: int | None = None,
        level: float = 0.95,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Circular block-bootstrap percentile intervals for each loading (spec §6.4).

        See ``bootstrap_loadings``. ``ridge`` and ``window`` default to the values
        stored by ``fit``.
        """
        use_ridge = self.ridge_ if ridge is None else ridge
        use_window = self.window_ if window is None else window
        if use_ridge is None:
            raise RuntimeError("pass ridge, or fit the model first")
        return bootstrap_loadings(
            asset_returns,
            factor_returns,
            use_ridge,
            use_window,
            n_boot,
            block_len,
            seed,
            level=level,
        )


def bootstrap_loadings(
    asset_returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    ridge: float,
    window: int | None,
    n_boot: int,
    block_len: int,
    seed: int,
    level: float = 0.95,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Percentile intervals for factor loadings by circular block bootstrap (spec §6.4).

    Time is resampled in circular blocks (Politis & Romano 1992) so asset and factor
    rows stay paired; each replicate refits the ridge regression. Intervals are the
    ``level`` percentile interval (default 95%) and are returned as ``(low, high)``
    frames aligned with the point loadings. The default level is a reporting choice
    [J]; spec §6.4 requires an interval and the non-overlap rule, not a particular
    tail probability.
    """
    if n_boot < 1:
        raise ValueError("n_boot must be positive")
    if not 0.0 < level < 1.0:
        raise ValueError("level must be in (0, 1)")
    assets, factors = _align(asset_returns, factor_returns, window)
    y = assets.to_numpy(dtype=float)
    f = factors.to_numpy(dtype=float)
    t = y.shape[0]
    draws = np.empty((n_boot, y.shape[1], f.shape[1]), dtype=np.float64)
    rng = np.random.default_rng(seed)
    for b in range(n_boot):
        idx = circular_block_indices(t, block_len, rng)
        loadings, _, _, _, _ = _fit_arrays(y[idx], f[idx], ridge)
        draws[b] = loadings
    tail = (1.0 - level) / 2.0
    low = np.quantile(draws, tail, axis=0)
    high = np.quantile(draws, 1.0 - tail, axis=0)
    names_a = list(assets.columns)
    names_f = list(factors.columns)
    return (
        pd.DataFrame(low, index=names_a, columns=names_f),
        pd.DataFrame(high, index=names_a, columns=names_f),
    )


def residual_clusters(resid_corr: pd.DataFrame, threshold: float) -> list[set[str]]:
    """Connected components of residual correlations with ``|ρ| > threshold`` (spec §6.2, §6.6).

    An edge joins two assets when the absolute residual correlation is strictly above
    ``threshold``. Components of size one are omitted: a cluster is shared structure,
    and §6.6 says to treat the cluster as one position for limits. The matrix is not
    modified. Labels come from the index, which must match the columns.
    """
    if threshold < 0:
        raise ValueError("threshold must be non-negative")
    names = [str(name) for name in resid_corr.index]
    if [str(name) for name in resid_corr.columns] != names:
        raise ValueError("residual correlation must be square with matching labels")
    corr = resid_corr.to_numpy(dtype=float)
    n = len(names)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            rho = corr[i, j]
            if np.isfinite(rho) and abs(float(rho)) > threshold:
                union(i, j)
    groups: dict[int, set[str]] = {}
    for i, name in enumerate(names):
        groups.setdefault(find(i), set()).add(name)
    clusters = [group for group in groups.values() if len(group) >= 2]
    clusters.sort(key=lambda group: min(group))
    return clusters


def exposures_from_loadings(positions: dict[str, float], loadings: pd.DataFrame) -> pd.Series:
    """``Σ_i w_i β_i`` from a loadings frame (spec §11.4–11.5). See ``FactorModel.exposures``."""
    missing = [name for name in positions if name not in loadings.index]
    if missing:
        raise ValueError(f"positions not in the factor model: {sorted(missing)}")
    weights = np.array([float(positions.get(asset, 0.0)) for asset in loadings.index], dtype=np.float64)
    if not np.isfinite(weights).all():
        raise ValueError("positions must be finite")
    exposure = loadings.to_numpy(dtype=float).T @ weights
    return pd.Series(exposure, index=list(loadings.columns), name="factor_exposure")


def _align(
    asset_returns: pd.DataFrame, factor_returns: pd.DataFrame, window: int | None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not isinstance(asset_returns, pd.DataFrame) or not isinstance(factor_returns, pd.DataFrame):
        raise TypeError("asset_returns and factor_returns must be DataFrames")
    if asset_returns.columns.has_duplicates or factor_returns.columns.has_duplicates:
        raise ValueError("return columns must be unique")
    overlap = set(asset_returns.columns) & set(factor_returns.columns)
    if overlap:
        raise ValueError(f"asset and factor names overlap: {sorted(overlap)}")
    if window is not None and window < 1:
        raise ValueError("window must be positive or None")
    idx = asset_returns.index.intersection(factor_returns.index)
    assets = asset_returns.loc[idx]
    factors = factor_returns.loc[idx]
    keep = assets.notna().all(axis=1) & factors.notna().all(axis=1)
    assets = assets.loc[keep]
    factors = factors.loc[keep]
    if window is not None:
        if window > len(assets):
            raise ValueError(f"window {window} exceeds {len(assets)} complete rows")
        assets = assets.iloc[-window:]
        factors = factors.iloc[-window:]
    k = factors.shape[1]
    if assets.shape[0] <= k + 1:
        raise ValueError("need more complete rows than factors plus the intercept")
    if k < 1 or assets.shape[1] < 1:
        raise ValueError("need at least one asset and one factor")
    return assets, factors


def _fit_arrays(
    asset: np.ndarray, factor: np.ndarray, ridge: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if ridge < 0:
        raise ValueError("ridge must be non-negative")
    y_mean = asset.mean(axis=0)
    f_mean = factor.mean(axis=0)
    y = asset - y_mean
    f = factor - f_mean
    k = f.shape[1]
    xtx = f.T @ f
    if ridge > 0:
        xtx = xtx + ridge * np.eye(k)
    try:
        beta = np.linalg.solve(xtx, f.T @ y)  # (K, N)
    except np.linalg.LinAlgError as exc:
        raise ValueError("factor design is singular; increase ridge") from exc
    loadings = beta.T
    resid = y - f @ beta
    dof = asset.shape[0] - k - 1
    if dof <= 0:
        raise ValueError("need more rows than factors plus the intercept")
    ss_res = np.sum(resid**2, axis=0)
    ss_tot = np.sum(y**2, axis=0)
    resid_var = ss_res / dof
    r2 = np.full(asset.shape[1], np.nan, dtype=np.float64)
    positive = ss_tot > 0
    r2[positive] = 1.0 - ss_res[positive] / ss_tot[positive]
    alpha = y_mean - f_mean @ beta
    return loadings, resid_var, resid, r2, alpha


def _residual_correlation(resid: np.ndarray, names: list[str]) -> pd.DataFrame:
    scale = resid.std(axis=0, ddof=0)
    corr = np.full((resid.shape[1], resid.shape[1]), np.nan, dtype=np.float64)
    np.fill_diagonal(corr, 1.0)
    ok = scale > 0
    if np.any(ok):
        z = resid[:, ok] / scale[ok]
        block = (z.T @ z) / resid.shape[0]
        corr[np.ix_(ok, ok)] = block
        np.fill_diagonal(corr, 1.0)
    return pd.DataFrame(corr, index=names, columns=names)
