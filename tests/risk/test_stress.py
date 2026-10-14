"""Stress correlations and scenario P&L (spec §6.5, §11.4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from macrosys.risk import (
    Scenario,
    conditional_correlation,
    forbes_rigobon_adjusted,
    load_scenarios,
    scenario_pnl,
    stress_correlation_report,
)

ROOT = Path(__file__).resolve().parents[2]


def test_forbes_rigobon_undoes_a_pure_variance_increase():
    rho = 0.4
    # Algebraic identity of the published formula at δ = 3, β held fixed.
    rho_crisis = rho * np.sqrt(4.0) / np.sqrt(rho**2 * 4.0 + (1.0 - rho**2))
    assert forbes_rigobon_adjusted(rho_crisis, 4.0) == pytest.approx(rho)

    rng = np.random.default_rng(1)
    n_calm, n_crisis = 8000, 8000
    delta = 3.0
    x_calm = rng.normal(size=n_calm)
    y_calm = rho * x_calm + rng.normal(scale=np.sqrt(1.0 - rho**2), size=n_calm)
    x_crisis = rng.normal(scale=np.sqrt(1.0 + delta), size=n_crisis)
    y_crisis = rho * x_crisis + rng.normal(scale=np.sqrt(1.0 - rho**2), size=n_crisis)
    raw = np.corrcoef(x_crisis, y_crisis)[0, 1]
    assert raw > rho + 0.05
    var_ratio = np.var(x_crisis) / np.var(x_calm)
    adjusted = forbes_rigobon_adjusted(raw, var_ratio)
    assert adjusted == pytest.approx(rho, abs=0.03)

    returns = np.column_stack(
        [np.concatenate([x_calm, x_crisis]), np.concatenate([y_calm, y_crisis])]
    )
    mask = np.concatenate([np.zeros(n_calm, dtype=bool), np.ones(n_crisis, dtype=bool)])
    report = stress_correlation_report(returns, mask)
    assert report.raw.iloc[0, 1] > rho + 0.05
    assert report.adjusted.iloc[0, 1] == pytest.approx(rho, abs=0.04)


def test_scenario_library_and_null_moves_raise():
    scenarios = load_scenarios(ROOT / "config" / "scenarios.yaml")
    by_name = {s.name: s for s in scenarios}
    assert by_name["hist_2008q4"].start == date(2008, 10, 1)
    assert by_name["hist_2008q4"].end == date(2008, 12, 31)
    assert by_name["hist_2011_08"].start == date(2011, 8, 1)
    assert by_name["hist_2013_taper"].start == date(2013, 5, 1)
    assert by_name["hist_2013_taper"].end == date(2013, 6, 30)
    assert by_name["hist_2015_08"].end == date(2015, 8, 31)
    assert by_name["hist_2018q4"].start == date(2018, 10, 1)
    assert by_name["hist_2020_03"].start == date(2020, 3, 1)
    assert by_name["hist_2020_03"].end == date(2020, 3, 31)
    assert by_name["hist_2022"].start == date(2022, 1, 1)
    assert by_name["hist_2022"].end == date(2022, 12, 31)
    assert by_name["funding_squeeze"].phases[0].factor_moves_pct["usd"] is None
    assert by_name["funding_squeeze"].phases[1].factor_moves_pct["us_duration"] is None
    assert by_name["inflation_shock"].factor_moves_pct["inflation_breakeven"] is None
    assert by_name["inflation_shock"].provenance == "TODO_calibrate"

    loadings = pd.DataFrame({"usd": [1.0], "us_duration": [0.5]}, index=["A"])
    factors = pd.DataFrame({"usd": [0.0], "us_duration": [0.0]}, index=pd.to_datetime(["2020-01-02"]))
    with pytest.raises(ValueError, match="null"):
        scenario_pnl({"A": 1.0}, loadings, factors, by_name["funding_squeeze"])
    with pytest.raises(ValueError, match="null"):
        scenario_pnl({"A": 1.0}, loadings, factors, by_name["inflation_shock"])


def test_scenario_pnl_arithmetic_on_a_factor_shock_and_a_window():
    positions = {"A": 1.0, "B": -1.0}
    loadings = pd.DataFrame({"eq": [1.0, 0.0], "usd": [0.0, 1.0]}, index=["A", "B"])
    shock = Scenario(name="toy", kind="factor_shock", factor_moves_pct={"eq": 10.0, "usd": -5.0})
    empty = pd.DataFrame(columns=["eq", "usd"])
    pnl = scenario_pnl(positions, loadings, empty, shock)
    # A: 1 * (1.0 * 0.10) = 0.10; B: -1 * (1.0 * -0.05) = 0.05; book = 0.15
    assert pnl.loc["toy", "A"] == pytest.approx(0.10)
    assert pnl.loc["toy", "B"] == pytest.approx(0.05)
    assert pnl.loc["toy", "portfolio"] == pytest.approx(0.15)

    idx = pd.to_datetime(["2020-03-02", "2020-03-03", "2020-03-04"])
    factor_returns = pd.DataFrame({"eq": [0.01, 0.02, -0.01], "usd": [0.0, 0.0, 0.0]}, index=idx)
    window = Scenario(name="win", kind="historical", start=date(2020, 3, 2), end=date(2020, 3, 4))
    hist = scenario_pnl(positions, loadings, factor_returns, window)
    eq = float(np.prod(1.0 + factor_returns["eq"].to_numpy()) - 1.0)
    assert hist.loc["win", "A"] == pytest.approx(eq)
    assert hist.loc["win", "B"] == pytest.approx(0.0)
    assert hist.loc["win", "portfolio"] == pytest.approx(eq)

    partial = Scenario(name="two_days", kind="historical", start=date(2020, 3, 2), end=date(2020, 3, 3))
    partial_pnl = scenario_pnl({"A": 1.0}, loadings.loc[["A"]], factor_returns, partial)
    eq_two = float(np.prod([1.01, 1.02]) - 1.0)
    assert partial_pnl.loc["two_days", "A"] == pytest.approx(eq_two)


def test_lower_tail_correlation_uses_only_the_joint_exceedances():
    # Medians split the sample. The joint lower tail is the points on y = x.
    left = np.array([0.0, 1.0, 2.0, 3.0, -1.0, -2.0])
    right = np.array([0.0, 5.0, 5.0, 5.0, -1.0, -2.0])
    rho = conditional_correlation(left, right, "lower_tail", 0.5)
    assert rho == pytest.approx(1.0)
