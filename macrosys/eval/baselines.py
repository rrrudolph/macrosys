"""Dumb baselines and a costed backtest (spec §10.2 items 2 and 3).

The reference books are 60/40, equal weight, inverse volatility, and 12-month time-series momentum
(Moskowitz, Ooi & Pedersen 2012). Each function turns a T×N simple-return panel into a weight panel
on the same index. ``backtest`` applies those weights on a rebalance calendar, charges ``cost_bp``
on traded notional, and reports net returns, turnover, and drawdown. No data is fetched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

WeightsFn = Callable[[pd.DataFrame], pd.DataFrame]


@dataclass(frozen=True)
class BacktestResult:
    """Net-of-cost simple returns, one-way turnover, and the drawdown of compounded wealth."""

    net_returns: pd.Series
    turnover: pd.Series
    drawdown: pd.Series


def sixty_forty(returns: pd.DataFrame, equity_col: str, bond_col: str) -> pd.DataFrame:
    """Constant 60% equity and 40% bonds; every other column is zero (spec §10.2).

    Weights sum to 1 on every row. ``equity_col`` and ``bond_col`` must be columns of ``returns``.
    """
    _require_panel(returns)
    missing = [name for name in (equity_col, bond_col) if name not in returns.columns]
    if missing:
        raise KeyError(f"sixty_forty columns not in the return panel: {missing}")
    if equity_col == bond_col:
        raise ValueError("equity and bond columns must be different")
    weights = pd.DataFrame(0.0, index=returns.index, columns=returns.columns)
    weights[equity_col] = 0.6
    weights[bond_col] = 0.4
    return weights


def equal_weight(returns: pd.DataFrame) -> pd.DataFrame:
    """Each column gets ``1 / N`` on every row (spec §10.2). Weights sum to 1."""
    _require_panel(returns)
    n_assets = returns.shape[1]
    return pd.DataFrame(1.0 / n_assets, index=returns.index, columns=returns.columns)


def inverse_vol(returns: pd.DataFrame, window: int) -> pd.DataFrame:
    """Weights proportional to the inverse of trailing realized volatility, renormalized to sum to 1.

    Volatility is the rolling sample standard deviation of simple returns over ``window`` bars
    (spec §10.2 inverse-vol baseline). Rows before the window is full are zero, which the backtest
    treats as flat. A column with zero volatility in a row is left out of that row's normalization.
    """
    _require_panel(returns)
    if window < 2:
        raise ValueError("inverse_vol window must be at least 2")
    vol = returns.rolling(window).std()
    inverse = 1.0 / vol.where(vol > 0.0)
    totals = inverse.sum(axis=1, min_count=1)
    weights = inverse.div(totals, axis=0)
    return weights.fillna(0.0)


def tsmom_12m(returns: pd.DataFrame, skip_last_month: bool = True) -> pd.DataFrame:
    """Sign of the trailing 12-month return, scaled by inverse volatility (Moskowitz, Ooi & Pedersen 2012).

    The lookback is 12 months on a monthly index, 52 weeks on a weekly index, and 252 sessions on a
    daily index. ``skip_last_month=True`` ends that window one month (or 21 sessions, or 4 weeks)
    before the row, which is the usual skip of the most recent month. Volatility is the trailing
    lookback standard deviation known before the row.

    Within the long side and within the short side, weights are proportional to ``sign / vol``.
    When both sides are present each side is scaled to gross 0.5, so the row nets to 0 and grosses
    to 1. That dollar-neutral scaling is a [J] on top of the paper, which targets volatility per
    instrument and does not force a zero net; the evaluation harness asks for a 0-net long/short
    book. A row with only one sign is fully invested in that direction (weights sum to +1 or -1).
    Warmup rows are zero.
    """
    _require_panel(returns)
    if (returns.to_numpy(dtype=float) <= -1.0).any():
        raise ValueError("tsmom_12m needs simple returns strictly above -100%")
    lookback, skip = _horizon(returns.index, skip_last_month)
    log_returns = np.log1p(returns)
    signal = np.expm1(log_returns.rolling(lookback).sum().shift(skip))
    vol = returns.rolling(lookback).std().shift(1)
    raw = np.sign(signal) / vol.where(vol > 0.0)
    return _dollar_neutral(raw)


def backtest(
    weights_fn: WeightsFn,
    returns: pd.DataFrame,
    rebalance: str = "W",
    cost_bp: float = 0.0,
) -> BacktestResult:
    """Apply ``weights_fn(returns)`` on a rebalance rule and charge ``cost_bp`` per unit traded.

    Weights are sampled with ``resample(rebalance).last()`` and held between those dates. The weight
    held through the return at t is the weight from t-1, so a decision does not earn the same bar's
    return. Turnover at t is the sum of absolute weight changes from t-1, counting a start from flat
    cash. Cost is ``turnover * cost_bp / 10000`` subtracted from that bar's gross return (one-way,
    in basis points of notional traded). Drawdown is compounded net wealth divided by its running peak,
    minus one.
    """
    _require_panel(returns)
    if cost_bp < 0:
        raise ValueError("cost_bp cannot be negative")
    if not isinstance(returns.index, pd.DatetimeIndex):
        raise TypeError("backtest requires a DatetimeIndex so the rebalance rule can be applied")
    target = weights_fn(returns).reindex(index=returns.index, columns=returns.columns).fillna(0.0)
    held = target.resample(rebalance).last().reindex(returns.index, method="ffill").fillna(0.0)
    previous = held.shift(1).fillna(0.0)
    turnover = (held - previous).abs().sum(axis=1)
    gross = (previous * returns.fillna(0.0)).sum(axis=1)
    net = gross - turnover * (cost_bp / 10_000.0)
    wealth = (1.0 + net).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    net.name = "net_returns"
    turnover.name = "turnover"
    drawdown.name = "drawdown"
    return BacktestResult(net_returns=net, turnover=turnover, drawdown=drawdown)


def _require_panel(returns: pd.DataFrame) -> None:
    if not isinstance(returns, pd.DataFrame) or returns.shape[1] < 1:
        raise ValueError("returns must be a T×N DataFrame with at least one column")


def _horizon(index: pd.Index, skip_last_month: bool) -> tuple[int, int]:
    freq = pd.infer_freq(index)
    lookback, skip = 252, 21
    if freq:
        head = freq.upper()
        if head.startswith(("M", "BM")):
            lookback, skip = 12, 1
        elif head.startswith("W"):
            lookback, skip = 52, 4
        elif head.startswith("Q"):
            lookback, skip = 4, 1
    if not skip_last_month:
        skip = 0
    return lookback, skip


def _dollar_neutral(raw: pd.DataFrame) -> pd.DataFrame:
    weights = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)
    values = raw.to_numpy(dtype=float)
    out = np.zeros_like(values)
    for row_index, row in enumerate(values):
        if not np.isfinite(row).all():
            continue
        long_mask = row > 0
        short_mask = row < 0
        if long_mask.any() and short_mask.any():
            out[row_index, long_mask] = row[long_mask] / row[long_mask].sum() * 0.5
            out[row_index, short_mask] = row[short_mask] / np.abs(row[short_mask]).sum() * 0.5
        elif long_mask.any():
            out[row_index, long_mask] = row[long_mask] / row[long_mask].sum()
        elif short_mask.any():
            out[row_index, short_mask] = row[short_mask] / np.abs(row[short_mask]).sum()
    weights.iloc[:, :] = out
    return weights
