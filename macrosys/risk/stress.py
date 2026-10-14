"""Stress co-movement and scenario P&L (spec §6.5, §11.4).

Conditional correlations are reported raw and after the Forbes–Rigobon
heteroskedasticity adjustment. Scenario P&L prices each position's factor
exposure; a factor-shock scenario with a null move is refused rather than filled
with an invented magnitude.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ScenarioKind = Literal["historical", "factor_shock"]
Tail = Literal["lower_tail", "upper_tail"]


class ScenarioPhase(BaseModel):
    """One leg of a factor-shock path. Moves are percent; null means uncalibrated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    factor_moves_pct: dict[str, float | None]
    note: str | None = None


class Scenario(BaseModel):
    """A historical window or a mechanism-driven factor shock (spec §6.5).

    ``factor_moves_pct`` is a simple factor return in percent (10 means +10%).
    Credit factors are credit-portfolio returns, so a widening is a negative move
    once someone calibrates it — this model does not convert spreads. Historical
    scenarios carry an inclusive calendar window and no moves. A factor shock
    carries either one move map or an ordered list of phases (the funding squeeze
    is "duration down then up", which is two phases, not one net number).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: ScenarioKind
    start: date | None = None
    end: date | None = None
    factor_moves_pct: dict[str, float | None] | None = None
    phases: list[ScenarioPhase] | None = None
    note: str | None = None
    provenance: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _as_date(cls, value: object) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise ValueError(f"cannot read a date from {value!r}")

    @model_validator(mode="after")
    def _kind_fields(self) -> Scenario:
        if self.kind == "historical":
            if self.start is None or self.end is None:
                raise ValueError("historical scenario needs start and end")
            if self.start > self.end:
                raise ValueError("historical scenario starts after it ends")
            if self.factor_moves_pct is not None or self.phases is not None:
                raise ValueError("historical scenario does not take factor moves")
        else:
            if self.start is not None or self.end is not None:
                raise ValueError("factor_shock scenario does not take a window")
            has_moves = self.factor_moves_pct is not None
            has_phases = self.phases is not None
            if has_moves == has_phases:
                raise ValueError("factor_shock needs factor_moves_pct or phases, not both")
            if self.phases is not None and len(self.phases) == 0:
                raise ValueError("factor_shock phases must not be empty")
        return self


def load_scenarios(path: str | Path) -> list[Scenario]:
    """Read the scenario library from YAML (spec §6.5).

    The file is a mapping with a ``scenarios`` list, or a bare list. Dates are
    calendar dates. Null factor moves are preserved; ``scenario_pnl`` raises on
    them instead of substituting a magnitude.
    """
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if isinstance(raw, dict) and "scenarios" in raw:
        items = raw["scenarios"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("scenario file must be a list or a mapping with 'scenarios'")
    if not isinstance(items, list):
        raise ValueError("'scenarios' must be a list")
    return [Scenario.model_validate(item) for item in items]


def conditional_correlation(
    x: np.ndarray | pd.Series,
    y: np.ndarray | pd.Series,
    condition: Tail,
    quantile: float,
) -> float:
    """Exceedance correlation in one joint tail (spec §6.5; Ang & Chen 2002).

    ``quantile`` is the marginal threshold in the empirical distribution of each
    series. ``lower_tail`` keeps dates where both series are at or below their
    ``quantile`` quantile; ``upper_tail`` keeps dates where both are at or above it.
    The reported number is the Pearson correlation of that subset (Longin & Solnik
    2001 use the same exceedance definition). It is not Forbes–Rigobon adjusted;
    that correction is a separate call. Non-finite pairs are dropped before the
    thresholds are computed.
    """
    if condition not in ("lower_tail", "upper_tail"):
        raise ValueError("condition must be 'lower_tail' or 'upper_tail'")
    if not 0.0 < quantile < 1.0:
        raise ValueError("quantile must be in (0, 1)")
    left = np.asarray(x, dtype=np.float64).ravel()
    right = np.asarray(y, dtype=np.float64).ravel()
    if left.shape != right.shape:
        raise ValueError("x and y must have the same length")
    ok = np.isfinite(left) & np.isfinite(right)
    left, right = left[ok], right[ok]
    if left.shape[0] < 3:
        raise ValueError("need at least 3 finite pairs")
    thr_x = float(np.quantile(left, quantile))
    thr_y = float(np.quantile(right, quantile))
    if condition == "lower_tail":
        keep = (left <= thr_x) & (right <= thr_y)
    else:
        keep = (left >= thr_x) & (right >= thr_y)
    if int(keep.sum()) < 3:
        raise ValueError("not enough joint exceedances to compute a correlation")
    return _pearson(left[keep], right[keep])


def _pearson(x: np.ndarray, y: np.ndarray) -> float:
    xc = x - x.mean()
    yc = y - y.mean()
    ss_x = float(np.dot(xc, xc))
    ss_y = float(np.dot(yc, yc))
    if ss_x <= 0.0 or ss_y <= 0.0:
        return float("nan")
    return float(np.dot(xc, yc) / np.sqrt(ss_x * ss_y))


def forbes_rigobon_adjusted(rho_crisis: float, var_ratio: float) -> float:
    """Heteroskedasticity-adjusted correlation (spec §6.5; Forbes & Rigobon 2002).

    ``ρ* = ρ / sqrt(1 + δ (1 − ρ²))`` with ``δ = var_ratio − 1`` and
    ``var_ratio = σ²_crisis / σ²_calm`` of the source series (the series whose
    variance moved). ``ρ`` is the correlation measured in the high-variance window.
    When only that variance changes and the regression slope is stable, ``ρ*``
    recovers the calm-window correlation. The value is not clipped to [−1, 1].
    """
    rho = float(rho_crisis)
    ratio = float(var_ratio)
    if not np.isfinite(rho) or rho < -1.0 or rho > 1.0:
        raise ValueError("rho_crisis must be a correlation in [-1, 1]")
    if not np.isfinite(ratio) or ratio <= 0.0:
        raise ValueError("var_ratio must be a positive finite variance ratio")
    delta = ratio - 1.0
    inside = 1.0 + delta * (1.0 - rho**2)
    if inside <= 0.0:
        raise ValueError("Forbes–Rigobon adjustment is undefined for this variance ratio")
    return float(rho / np.sqrt(inside))


class StressCorrelationReport(BaseModel):
    """Raw and Forbes–Rigobon pairwise correlations on a crisis mask (spec §6.5)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    raw: pd.DataFrame
    adjusted: pd.DataFrame
    variance_ratio: pd.Series

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"StressCorrelationReport(assets={list(self.raw.columns)})"


def stress_correlation_report(
    returns: np.ndarray | pd.DataFrame,
    crisis_mask: np.ndarray,
) -> StressCorrelationReport:
    """Pairwise crisis correlations, raw and variance-adjusted (spec §6.5).

    The raw matrix is the Pearson correlation of rows where ``crisis_mask`` is true,
    after dropping incomplete rows. The adjusted matrix applies Forbes & Rigobon
    (2002) to each pair. The formula needs one source variance; [J] the source is
    the asset in the pair with the larger crisis-to-calm variance ratio, and the
    same adjusted value is written in both off-diagonal entries. Nothing is clipped
    and the matrix is not replaced by a PSD projection. ``variance_ratio`` is the
    per-asset crisis/calm variance, each variance taken around its own regime mean.
    """
    values, names = _labelled_returns(returns)
    mask = np.asarray(crisis_mask, dtype=bool)
    if mask.shape != (values.shape[0],):
        raise ValueError("crisis_mask must have one boolean per row")
    finite = np.isfinite(values).all(axis=1)
    crisis = values[finite & mask]
    calm = values[finite & ~mask]
    if crisis.shape[0] < 3 or calm.shape[0] < 2:
        raise ValueError("need at least 3 complete crisis rows and 2 calm rows")
    raw = np.corrcoef(crisis, rowvar=False)
    raw = np.atleast_2d(raw)
    var_crisis = crisis.var(axis=0)
    var_calm = calm.var(axis=0)
    if np.any(var_calm <= 0.0):
        raise ValueError("a calm-window variance is zero; the variance ratio is undefined")
    ratio = var_crisis / var_calm
    n = raw.shape[0]
    adjusted = np.eye(n, dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            source = float(max(ratio[i], ratio[j]))
            value = forbes_rigobon_adjusted(float(raw[i, j]), source)
            adjusted[i, j] = value
            adjusted[j, i] = value
    return StressCorrelationReport(
        raw=pd.DataFrame(raw, index=names, columns=names),
        adjusted=pd.DataFrame(adjusted, index=names, columns=names),
        variance_ratio=pd.Series(ratio, index=names, name="variance_ratio"),
    )


def scenario_pnl(
    positions: dict[str, float],
    loadings: pd.DataFrame,
    factor_returns: pd.DataFrame,
    scenarios: Scenario | list[Scenario],
) -> pd.DataFrame:
    """Price each position's factor exposure under each scenario (spec §6.5, §11.4).

    Historical scenarios compound realized simple factor returns over the inclusive
    calendar window, ``Π(1+r) − 1``, on rows that are complete across the loading
    factors. Factor-shock scenarios convert declared percent moves to decimal
    returns and, when the scenario has phases, sum the phase returns (linear
    approximation of "down then up"). Position P&L is weight times loading times
    that factor return; ``portfolio`` is the sum. If positions are weights, P&L is
    a portfolio-return contribution (dimensionless). A null move raises ``ValueError``
    — magnitudes live in the scenario file and are not invented here.
    """
    if isinstance(scenarios, Scenario):
        scenario_list = [scenarios]
    else:
        scenario_list = list(scenarios)
    if "portfolio" in positions:
        raise ValueError("position name 'portfolio' is reserved for the total")
    names = list(positions.keys())
    missing = [name for name in names if name not in loadings.index]
    if missing:
        raise ValueError(f"positions not in loadings: {sorted(missing)}")
    weights = np.array([float(positions[name]) for name in names], dtype=np.float64)
    if not np.isfinite(weights).all():
        raise ValueError("positions must be finite")
    beta = loadings.loc[names].to_numpy(dtype=np.float64)
    rows = []
    index = []
    for scenario in scenario_list:
        factor_move = _scenario_factor_return(scenario, loadings, factor_returns)
        contrib = beta @ factor_move
        pnl = weights * contrib
        row = {name: float(pnl[i]) for i, name in enumerate(names)}
        row["portfolio"] = float(pnl.sum())
        rows.append(row)
        index.append(scenario.name)
    out = pd.DataFrame(rows, index=pd.Index(index, name="scenario"))
    return out.loc[:, [*names, "portfolio"]]


def _labelled_returns(returns: np.ndarray | pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    if isinstance(returns, pd.DataFrame):
        return returns.to_numpy(dtype=np.float64), [str(c) for c in returns.columns]
    values = np.asarray(returns, dtype=np.float64)
    if values.ndim != 2:
        raise ValueError("returns must be T×N")
    return values, [str(i) for i in range(values.shape[1])]


def _scenario_factor_return(
    scenario: Scenario, loadings: pd.DataFrame, factor_returns: pd.DataFrame
) -> np.ndarray:
    columns = [str(c) for c in loadings.columns]
    if scenario.kind == "historical":
        assert scenario.start is not None and scenario.end is not None
        return _historical_factor_return(factor_returns, scenario.start, scenario.end, columns, scenario.name)
    return _shock_factor_return(scenario, columns)


def _historical_factor_return(
    factor_returns: pd.DataFrame,
    start: date,
    end: date,
    columns: list[str],
    name: str,
) -> np.ndarray:
    missing = [col for col in columns if col not in factor_returns.columns]
    if missing:
        raise ValueError(f"scenario {name!r} is missing factor columns {missing}")
    idx = pd.DatetimeIndex(pd.to_datetime(factor_returns.index))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    days = idx.normalize()
    mask = (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))
    window = factor_returns.loc[mask, columns].to_numpy(dtype=np.float64)
    if window.size == 0:
        raise ValueError(f"scenario {name!r} has no factor returns in {start}..{end}")
    keep = np.isfinite(window).all(axis=1)
    window = window[keep]
    if window.shape[0] == 0:
        raise ValueError(f"scenario {name!r} has no complete factor-return rows in the window")
    return np.prod(1.0 + window, axis=0) - 1.0


def _shock_factor_return(scenario: Scenario, columns: list[str]) -> np.ndarray:
    phases: list[dict[str, float | None]]
    if scenario.phases is not None:
        phases = [phase.factor_moves_pct for phase in scenario.phases]
    elif scenario.factor_moves_pct is not None:
        phases = [scenario.factor_moves_pct]
    else:
        raise ValueError(f"scenario {scenario.name!r} has no factor moves")
    if any(value is None for moves in phases for value in moves.values()):
        raise ValueError(
            f"scenario {scenario.name!r} has null factor moves "
            "(TODO_calibrate); refusing to price an uncalibrated shock"
        )
    index = {col: i for i, col in enumerate(columns)}
    unknown = sorted({name for moves in phases for name in moves if name not in index})
    if unknown:
        raise ValueError(f"scenario {scenario.name!r} names factors not in loadings: {unknown}")
    total = np.zeros(len(columns), dtype=np.float64)
    for moves in phases:
        for name, value in moves.items():
            assert value is not None
            total[index[name]] += float(value) / 100.0
    return total
