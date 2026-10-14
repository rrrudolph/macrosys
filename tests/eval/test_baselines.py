"""Baseline weights and a toy backtest (spec §10.2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from macrosys.eval.baselines import backtest, equal_weight, inverse_vol, sixty_forty, tsmom_12m


def test_long_only_weights_sum_to_one_and_tsmom_is_zero_net() -> None:
    index = pd.bdate_range("2024-01-02", periods=30)
    rng = np.random.default_rng(0)
    returns = pd.DataFrame(
        {
            "eq": rng.normal(0.001, 0.01, len(index)),
            "bd": rng.normal(0.0002, 0.004, len(index)),
            "cu": rng.normal(0.0, 0.015, len(index)),
        },
        index=index,
    )
    book = sixty_forty(returns, "eq", "bd")
    assert book.sum(axis=1).iloc[-1] == pytest.approx(1.0)
    assert book["eq"].iloc[0] == pytest.approx(0.6)
    assert book["bd"].iloc[0] == pytest.approx(0.4)
    assert book["cu"].iloc[0] == pytest.approx(0.0)
    assert equal_weight(returns).sum(axis=1).iloc[0] == pytest.approx(1.0)

    inv = inverse_vol(returns, window=5)
    last = inv.iloc[-1]
    assert last.sum() == pytest.approx(1.0)
    assert (last >= -1e-12).all()

    months = pd.date_range("2020-01-31", periods=36, freq="ME")
    drift = np.random.default_rng(1)
    monthly = pd.DataFrame(
        {
            "up": 0.03 + drift.normal(0.0, 0.005, len(months)),
            "down": -0.03 + drift.normal(0.0, 0.005, len(months)),
        },
        index=months,
    )
    momentum = tsmom_12m(monthly, skip_last_month=True)
    row = momentum.iloc[-1]
    assert row.sum() == pytest.approx(0.0)
    assert row.abs().sum() == pytest.approx(1.0)
    assert row["up"] > 0
    assert row["down"] < 0


def test_backtest_turnover_and_drawdown_on_a_toy_series() -> None:
    index = pd.bdate_range("2024-01-02", periods=4)
    returns = pd.DataFrame(
        {"a": [0.0, -0.10, -0.10, 0.05], "b": [0.0, -0.10, -0.10, 0.05]},
        index=index,
    )
    free = backtest(equal_weight, returns, rebalance="B", cost_bp=0.0)
    paid = backtest(equal_weight, returns, rebalance="B", cost_bp=100.0)
    assert free.turnover.iloc[0] == pytest.approx(1.0)
    assert free.turnover.iloc[1] == pytest.approx(0.0)
    assert free.net_returns.iloc[0] == pytest.approx(0.0)
    assert free.net_returns.iloc[1] == pytest.approx(-0.10)
    assert free.drawdown.iloc[2] == pytest.approx(0.81 - 1.0)
    assert free.drawdown.min() < 0
    # 100 bp charged on the opening unit of turnover.
    assert paid.net_returns.iloc[0] == pytest.approx(-0.01)
