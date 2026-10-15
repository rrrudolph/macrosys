"""Estimate mechanism coefficients from the store, or record why they stay empty (spec §8.4, §3.1).

A job is a local projection: the change in one stored series on the change in another, at a
stated horizon (Jordà 2005). The HC1 interval has to exclude zero, and the slope has to keep
its sign in every re-estimation window, before the coefficient may replace ``TODO_estimate``.
A half-life is that autocorrelation mapped through ``ln(0.5) / ln(rho)``. A shortfall is a
mean whose interval lies inside ``(0, 1)``. A ceiling is the upper tail of a utilization
series. A failed job stays empty. The ``unavailable`` list is the coefficients this store
cannot identify; those reasons are copied into the parameter file and the cards are not edited.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import duckdb
import numpy as np
import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field

from macrosys.events.study import ols_hc1
from macrosys.store.observations import ObservationStore

Frequency = Literal["M", "Q"]
Op = Literal["pct_change", "difference", "level"]
MapName = Literal["identity", "negate", "one_minus"]
Kind = Literal["slope", "half_life", "shortfall", "ceiling"]
Status = Literal["installed", "rejected", "unavailable"]


class EstimateJob(BaseModel):
    """One regression that matches a mechanism formula (spec §8.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism: str
    parameter: str
    unit: str
    y: str
    x: str
    frequency: Frequency
    y_op: Op
    x_op: Op
    horizon: int = Field(ge=0)
    beta_sign: Literal[-1, 1]
    map: MapName
    note: str
    x_minus: str | None = None
    y_minus: str | None = None
    y_scale: float = 1.0
    x_over: str | None = None
    x_times: str | None = None
    x_times_scale: float = 1.0
    x_scale: float = 1.0
    x_lookback: int = Field(default=0, ge=0)
    y_add: str | None = None
    y_add_minus: str | None = None
    y_add_scale: float = 0.0
    control: str | None = None
    control_lookback: int = Field(default=0, ge=0)
    instrument: str | None = None
    state_series: str | None = None
    state_below: float | None = None
    state_above: float | None = None
    kind: Kind = "slope"
    foreign_price: str | None = None
    home_price: str | None = None
    lag: int = Field(default=0, ge=0)
    ceiling_floor: float = 0.70
    step: bool = False
    record_only: bool = False


class Unavailable(BaseModel):
    """A coefficient this store cannot identify (spec §3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism: str
    parameter: str
    reason: str


class EstimateConfig(BaseModel):
    """Jobs, the sample rules, and the coefficients that are not run (spec §8.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_n: int = Field(ge=10)
    minimum_window: int = Field(ge=5)
    windows: int = Field(ge=2)
    jobs: list[EstimateJob]
    unavailable: list[Unavailable] = Field(default_factory=list)


class Coefficient(BaseModel):
    """The structural parameter and the regression it came from (spec §8.4)."""

    model_config = ConfigDict(frozen=True)

    mechanism: str
    parameter: str
    status: Status
    reason: str
    unit: str | None = None
    low: float | None = None
    high: float | None = None
    beta: float | None = None
    n: int | None = None
    source: str | None = None


class EstimateReport(BaseModel):
    """Every job and every unavailable coefficient (spec §8.4, §3.1)."""

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    results: tuple[Coefficient, ...]


def default_config_path() -> Path:
    """Repository ``config/local_estimates.yaml``."""
    return Path(__file__).resolve().parents[2] / "config" / "local_estimates.yaml"


def default_output_path() -> Path:
    """Repository ``parameters/local_elasticities.yaml``."""
    return Path(__file__).resolve().parents[2] / "parameters" / "local_elasticities.yaml"


def load_config(path: str | Path) -> EstimateConfig:
    """Load the local-projection jobs (spec §8.4)."""
    loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must be a mapping")
    return EstimateConfig.model_validate(loaded)


def run_estimates(
    conn: duckdb.DuckDBPyConnection,
    config: EstimateConfig,
    as_of: datetime,
) -> EstimateReport:
    """Fit each job on observations published by ``as_of`` and record the rest as unavailable (spec §8.4, §9.2).

    ``as_of`` must be timezone-aware. Each series is the latest vintage of each reference period
    that had been published by that instant. A job whose interval contains zero, whose sign
    disagrees with the formula, or whose sign flips across windows is ``rejected`` and is not
    a magnitude.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    view = ObservationStore(conn).snapshot(as_of)
    results = [_fit_job(view, job, config, as_of) for job in config.jobs]
    results.extend(_unavailable(item) for item in config.unavailable)
    return EstimateReport(as_of=as_of, results=tuple(results))


def write_report(path: str | Path, report: EstimateReport) -> None:
    """Write the estimate report as YAML (spec §8.4)."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": report.as_of.isoformat(),
        "method": (
            "Local projection: HC1 slope of the transformed outcome on the transformed input, "
            "with an intercept. A coefficient is installed only when the 95% interval excludes "
            "zero and the slope's sign agrees in every re-estimation window. A half-life maps "
            "that autocorrelation through ln(0.5)/ln(rho). A shortfall is a mean inside (0, 1). "
            "A ceiling is the upper tail of a utilization series (spec 8.4)."
        ),
        "entries": [item.model_dump() for item in report.results],
    }
    file_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def install_supported(report: EstimateReport, mechanisms_dir: str | Path) -> list[str]:
    """Write installed intervals onto the mechanism cards. Rejected coefficients are left alone (spec §3.1).

    Returns the mechanism ids that changed. A card whose parameter is not ``TODO_estimate`` is
    not rewritten.
    """
    changed: list[str] = []
    root = Path(mechanisms_dir)
    for item in report.results:
        if item.status != "installed" or item.low is None or item.high is None or item.source is None:
            continue
        path = _card_path(root, item.mechanism)
        original = path.read_text(encoding="utf-8")
        updated = _replace_todo(original, item.parameter, item.low, item.high, item.source)
        if updated == original:
            continue
        path.write_text(updated, encoding="utf-8")
        changed.append(item.mechanism)
    return changed


def judge_slope(
    x: np.ndarray,
    y: np.ndarray,
    *,
    beta_sign: Literal[-1, 1],
    map_name: MapName,
    minimum_n: int,
    minimum_window: int,
    windows: int,
    controls: np.ndarray | None = None,
    instrument: np.ndarray | None = None,
) -> tuple[Status, str, float | None, float | None, float | None, int]:
    """Decide whether a slope may become a library range (spec §8.4, §14).

    Returns status, reason, low, high, beta, and n. ``low`` and ``high`` are the structural
    parameter after ``map_name``, and they are set only when the status is ``installed``.
    ``controls`` are additional regressors. ``instrument`` replaces the OLS slope with a
    just-identified instrumental-variable slope.
    """
    n = int(len(x))
    if n < minimum_n:
        return "rejected", f"n={n} is below {minimum_n}", None, None, None, n
    fit = _fit_slope(x, y, controls, instrument)
    if fit is None:
        return "rejected", "the slope is not identified", None, None, None, n
    beta, se = fit
    if not math.isfinite(beta) or se <= 0.0:
        return "rejected", "the slope is not identified", None, None, None, n
    sign = _sign(beta)
    if sign != beta_sign:
        return "rejected", f"slope sign {sign} is not the formula sign {beta_sign}", None, None, beta, n
    if beta - 1.96 * se <= 0.0 <= beta + 1.96 * se:
        return "rejected", "the HC1 interval contains zero", None, None, beta, n
    if not _stable(x, y, windows, minimum_window, sign, controls, instrument):
        return "rejected", "the slope sign is not the same in every window", None, None, beta, n
    low, high = _mapped_interval(beta, se, map_name)
    return "installed", "interval excludes zero and the sign is stable", low, high, beta, n


def judge_mean(
    values: np.ndarray,
    *,
    minimum_n: int,
    minimum_window: int,
    windows: int,
) -> tuple[Status, str, float | None, float | None, float | None, int]:
    """A mean whose 95% interval lies inside ``(0, 1)`` and stays positive in every window (spec §8.4)."""
    n = int(len(values))
    if n < minimum_n:
        return "rejected", f"n={n} is below {minimum_n}", None, None, None, n
    mean = float(np.mean(values))
    se = float(np.std(values, ddof=1) / math.sqrt(n))
    if not math.isfinite(mean) or se <= 0.0:
        return "rejected", "the mean is not identified", None, None, None, n
    low = mean - 1.96 * se
    high = mean + 1.96 * se
    if low <= 0.0 or high >= 1.0:
        return "rejected", "the mean is not a share whose interval lies inside (0, 1)", None, None, mean, n
    edges = np.linspace(0, n, windows + 1, dtype=int)
    for start, stop in zip(edges[:-1], edges[1:], strict=True):
        if stop - start < minimum_window or float(np.mean(values[start:stop])) <= 0.0:
            return "rejected", "the mean is not positive in every window", None, None, mean, n
    return "installed", "the mean is a share and it stays positive in every window", low, high, mean, n


def judge_ceiling(
    values: np.ndarray,
    *,
    floor: float,
    minimum_window: int,
    windows: int,
) -> tuple[Status, str, float | None, float | None, float | None, int]:
    """Upper tail of a utilization share: 95th percentile to the sample maximum (spec §8.4).

    Installed only when that tail sits strictly above ``floor`` and at or below 1, and every
    window's own 95th percentile stays above ``floor``.
    """
    n = int(len(values))
    if n < minimum_window * windows:
        return "rejected", f"n={n} is below {minimum_window * windows}", None, None, None, n
    low = float(np.quantile(values, 0.95))
    high = float(np.max(values))
    if not (floor < low <= high <= 1.0):
        return "rejected", "the upper tail is not a share above the kink and at most 1", None, None, high, n
    edges = np.linspace(0, n, windows + 1, dtype=int)
    for start, stop in zip(edges[:-1], edges[1:], strict=True):
        window = values[start:stop]
        if len(window) < minimum_window or float(np.quantile(window, 0.95)) <= floor:
            return "rejected", "a window's upper tail is at or below the kink", None, None, high, n
    return "installed", "historical upper tail of utilization stays above the kink", low, high, high, n


def bounds_half_life(low_rho: float, high_rho: float, periods_per_year: int) -> tuple[float, float] | None:
    """Map an autocorrelation interval inside ``(0, 1)`` into a half-life in years (spec §8.4).

    A higher autocorrelation is a longer half-life. ``None`` when the interval is not
    strictly inside ``(0, 1)``.
    """
    if periods_per_year < 1 or not (0.0 < low_rho < high_rho < 1.0):
        return None
    short = math.log(0.5) / math.log(low_rho) / periods_per_year
    long = math.log(0.5) / math.log(high_rho) / periods_per_year
    if not (math.isfinite(short) and math.isfinite(long) and 0.0 < short < long):
        return None
    return short, long


class _Prepared:
    """Aligned arrays for one job (spec §8.4)."""

    def __init__(
        self,
        y: np.ndarray,
        start: str,
        end: str,
        x: np.ndarray | None = None,
        controls: np.ndarray | None = None,
        instrument: np.ndarray | None = None,
    ) -> None:
        self.y = y
        self.x = x
        self.controls = controls
        self.instrument = instrument
        self.start = start
        self.end = end


def _fit_job(view, job: EstimateJob, config: EstimateConfig, as_of: datetime) -> Coefficient:
    del as_of
    try:
        prepared = _prepare(view, job)
    except KeyError as exc:
        return Coefficient(
            mechanism=job.mechanism,
            parameter=job.parameter,
            status="rejected",
            reason=str(exc),
            unit=job.unit,
        )
    status, reason, low, high, beta, n = _judge(prepared, job, config)
    if status == "installed" and job.kind == "half_life" and low is not None and high is not None:
        periods = 12 if job.frequency == "M" else 4
        bounds = bounds_half_life(low, high, periods)
        if bounds is None:
            status, reason, low, high = (
                "rejected",
                "the autocorrelation interval is not inside (0, 1), so the half-life is not finite",
                None,
                None,
            )
        else:
            low, high = bounds
    if status == "installed" and job.unit == "share" and job.kind == "slope":
        if low is None or high is None or low < 0.0 or high > 1.0:
            status, reason, low, high = (
                "rejected",
                "the mapped interval is outside the share range [0, 1]",
                None,
                None,
            )
    source = None
    if status == "installed" and job.record_only:
        status = "rejected"
        reason = "recorded only; not written onto the card. " + job.note
        low, high = None, None
    if status == "installed":
        source = (
            f"local_projection:{job.y} on {job.x}"
            + (f" minus {job.x_minus}" if job.x_minus else "")
            + f" {job.frequency} {job.kind} horizon {job.horizon}; HC1; n={n}; "
            f"{prepared.start}..{prepared.end}; {job.note}"
        )
    return Coefficient(
        mechanism=job.mechanism,
        parameter=job.parameter,
        status=status,
        reason=reason,
        unit=job.unit,
        low=None if low is None else round(low, 4),
        high=None if high is None else round(high, 4),
        beta=None if beta is None else round(beta, 4),
        n=n,
        source=source,
    )


def _judge(prepared: _Prepared, job: EstimateJob, config: EstimateConfig):
    if job.kind == "ceiling":
        return judge_ceiling(
            prepared.y,
            floor=job.ceiling_floor,
            minimum_window=config.minimum_window,
            windows=config.windows,
        )
    if job.kind == "shortfall":
        return judge_mean(
            prepared.y,
            minimum_n=config.minimum_n,
            minimum_window=config.minimum_window,
            windows=config.windows,
        )
    if prepared.x is None:
        raise ValueError(f"{job.mechanism}.{job.parameter} has no regressor")
    return judge_slope(
        prepared.x,
        prepared.y,
        beta_sign=job.beta_sign,
        map_name=job.map,
        minimum_n=config.minimum_n,
        minimum_window=config.minimum_window,
        windows=config.windows,
        controls=prepared.controls,
        instrument=prepared.instrument,
    )


def _prepare(view, job: EstimateJob) -> _Prepared:
    if job.kind == "half_life":
        return _half_life_sample(view, job)
    if job.kind == "ceiling":
        series = _series(view, job.y, job.frequency) * job.y_scale
        return _from_series(series.dropna(), job)
    if job.kind == "shortfall":
        return _shortfall_sample(view, job)
    y = _level(view, job.y, job.frequency, job.y_minus, step=job.step)
    x = _level(view, job.x, job.frequency, job.x_minus, step=job.step)
    if job.x_over is not None:
        x = x.div(_series(view, job.x_over, job.frequency)).replace([np.inf, -np.inf], np.nan).dropna()
    y_change = _transform(y, job.y_op, job.horizon, 0) * job.y_scale
    x_change = _transform(x, job.x_op, 0, job.x_lookback) * job.x_scale
    if job.x_times is not None:
        times = _series(view, job.x_times, job.frequency) * job.x_times_scale
        x_change = x_change.mul(times)
    if job.y_add is not None:
        added = _level(view, job.y_add, job.frequency, job.y_add_minus, step=job.step) * job.y_add_scale
        y_change = y_change.add(added)
    parts = [y_change.rename("y"), x_change.rename("x")]
    controls = None
    instrument = None
    if job.control is not None:
        control = _transform(
            _series(view, job.control, job.frequency), "pct_change", 0, job.control_lookback
        )
        parts.append(control.rename("control"))
    if job.instrument is not None:
        parts.append(_transform(_series(view, job.instrument, job.frequency), "pct_change", 0, 0).rename("z"))
    if job.state_series is not None:
        parts.append(_series(view, job.state_series, job.frequency).rename("state"))
    joined = pd.concat(parts, axis=1).dropna()
    before = len(joined)
    if job.state_below is not None and "state" in joined:
        joined = joined[joined["state"] < job.state_below]
    if job.state_above is not None and "state" in joined:
        joined = joined[joined["state"] > job.state_above]
    if joined.empty or "x" not in joined or joined["x"].empty:
        if before and (job.state_below is not None or job.state_above is not None):
            raise KeyError(f"{job.mechanism}.{job.parameter}: the state filter left no rows")
        raise KeyError(f"{job.mechanism}.{job.parameter}: {job.y} and {job.x} do not overlap")
    if "control" in joined:
        controls = joined["control"].to_numpy(dtype=float)
    if "z" in joined:
        instrument = joined["z"].to_numpy(dtype=float)
    return _Prepared(
        y=joined["y"].to_numpy(dtype=float),
        x=joined["x"].to_numpy(dtype=float),
        controls=controls,
        instrument=instrument,
        start=str(joined.index.min())[:10],
        end=str(joined.index.max())[:10],
    )


def _half_life_sample(view, job: EstimateJob) -> _Prepared:
    if job.foreign_price is None or job.home_price is None:
        raise KeyError(f"{job.mechanism}.{job.parameter}: half_life needs foreign_price and home_price")
    nominal = _series(view, job.y, job.frequency)
    foreign = _series(view, job.foreign_price, job.frequency)
    home = _series(view, job.home_price, job.frequency)
    real = np.log(nominal) + np.log(foreign) - np.log(home)
    real = real.replace([np.inf, -np.inf], np.nan).dropna()
    joined = pd.concat([real.rename("y"), real.shift(1).rename("x")], axis=1).dropna()
    if joined.empty:
        raise KeyError(f"{job.mechanism}.{job.parameter}: the real rate has no overlap")
    return _Prepared(
        y=joined["y"].to_numpy(dtype=float),
        x=joined["x"].to_numpy(dtype=float),
        start=str(joined.index.min())[:10],
        end=str(joined.index.max())[:10],
    )


def _shortfall_sample(view, job: EstimateJob) -> _Prepared:
    if job.lag < 1:
        raise KeyError(f"{job.mechanism}.{job.parameter}: shortfall needs a positive lag")
    completed = _series(view, job.y, job.frequency)
    permitted = _series(view, job.x, job.frequency).shift(job.lag)
    ratio = completed.div(permitted).replace([np.inf, -np.inf], np.nan)
    shortfall = (1.0 - ratio).dropna()
    if shortfall.empty:
        raise KeyError(f"{job.mechanism}.{job.parameter}: {job.y} and {job.x} do not overlap")
    return _from_series(shortfall, job)


def _from_series(series: pd.Series, job: EstimateJob) -> _Prepared:
    if series.empty:
        raise KeyError(f"{job.mechanism}.{job.parameter}: {job.y} has no observations")
    return _Prepared(
        y=series.to_numpy(dtype=float),
        start=str(series.index.min())[:10],
        end=str(series.index.max())[:10],
    )


def _level(view, series_id: str, frequency: Frequency, minus: str | None, *, step: bool = False) -> pd.Series:
    values = _series(view, series_id, frequency, step=step)
    if minus is None:
        return values
    return values.sub(_series(view, minus, frequency, step=step)).dropna()


def _series(view, series_id: str, frequency: Frequency, *, step: bool = False) -> pd.Series:
    frame = view.get(series_id)
    if frame.empty:
        raise KeyError(f"{series_id} has no observation published by as_of")
    stamp = pd.to_datetime(frame["ref_period_end"])
    values = pd.Series(frame["value"].to_numpy(dtype=float), index=stamp).sort_index()
    values = values[~values.index.duplicated(keep="last")]
    rule = "ME" if frequency == "M" else "QE"
    sampled = values.resample(rule).last()
    if step:
        sampled = sampled.ffill()
    return sampled.dropna()


def _transform(series: pd.Series, op: Op, horizon: int, lookback: int) -> pd.Series:
    if lookback:
        if op == "difference":
            changed = series - series.shift(lookback)
        elif op == "level":
            changed = series
        else:
            changed = (series / series.shift(lookback) - 1.0) * 100.0
    elif op == "level":
        changed = series
    elif op == "difference":
        changed = (series.shift(-horizon) - series) if horizon else series.diff()
    else:
        changed = (series.shift(-horizon) / series - 1.0) * 100.0 if horizon else series.pct_change() * 100.0
    return changed.replace([np.inf, -np.inf], np.nan)


def _fit_slope(
    x: np.ndarray,
    y: np.ndarray,
    controls: np.ndarray | None,
    instrument: np.ndarray | None,
) -> tuple[float, float] | None:
    if instrument is not None:
        return _iv_hc1(y, x, instrument)
    if controls is not None:
        return _hc1_column(y, np.column_stack([x, controls]))
    fit = ols_hc1(x, y)
    if fit is None:
        return None
    return fit.beta, fit.se


def _hc1_column(y: np.ndarray, columns: np.ndarray) -> tuple[float, float] | None:
    """HC1 standard error of the first slope, with an intercept (spec §8.4)."""
    n = len(y)
    design = np.column_stack([np.ones(n), columns])
    width = design.shape[1]
    if n < width + 2 or np.linalg.matrix_rank(design) < width:
        return None
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    residuals = y - design @ coef
    bread = np.linalg.inv(design.T @ design)
    meat = design.T @ (design * (residuals**2)[:, None])
    cov = bread @ meat @ bread * (n / (n - width))
    se = float(math.sqrt(max(cov[1, 1], 0.0)))
    return float(coef[1]), se


def _iv_hc1(y: np.ndarray, x: np.ndarray, instrument: np.ndarray) -> tuple[float, float] | None:
    """Just-identified IV slope and a robust standard error (spec §8.4)."""
    n = len(y)
    if n < 4 or np.ptp(instrument) == 0.0:
        return None
    y_c = y - y.mean()
    x_c = x - x.mean()
    z_c = instrument - instrument.mean()
    zx = float(np.dot(z_c, x_c))
    if abs(zx) < 1e-12:
        return None
    beta = float(np.dot(z_c, y_c) / zx)
    residual = y_c - beta * x_c
    meat = float(np.sum((z_c * residual) ** 2))
    se = math.sqrt(meat / (zx**2) * (n / (n - 2)))
    if not math.isfinite(se) or se <= 0.0:
        return None
    return beta, se


def _stable(
    x: np.ndarray,
    y: np.ndarray,
    windows: int,
    minimum_window: int,
    sign: int,
    controls: np.ndarray | None,
    instrument: np.ndarray | None,
) -> bool:
    edges = np.linspace(0, len(x), windows + 1, dtype=int)
    for start, stop in zip(edges[:-1], edges[1:], strict=True):
        if stop - start < minimum_window:
            return False
        window_controls = None if controls is None else controls[start:stop]
        window_instrument = None if instrument is None else instrument[start:stop]
        fit = _fit_slope(x[start:stop], y[start:stop], window_controls, window_instrument)
        if fit is None or _sign(fit[0]) != sign:
            return False
    return True


def _mapped_interval(beta: float, se: float, map_name: MapName) -> tuple[float, float]:
    low = beta - 1.96 * se
    high = beta + 1.96 * se
    if map_name == "negate":
        return -high, -low
    if map_name == "one_minus":
        return 1.0 - high, 1.0 - low
    return low, high


def _sign(value: float) -> Literal[-1, 1]:
    if value == 0.0 or not math.isfinite(value):
        return 1
    return -1 if value < 0.0 else 1


def _unavailable(item: Unavailable) -> Coefficient:
    return Coefficient(
        mechanism=item.mechanism,
        parameter=item.parameter,
        status="unavailable",
        reason=item.reason,
    )


def _card_path(root: Path, mechanism_id: str) -> Path:
    matches = list(root.glob(f"{mechanism_id}.yaml"))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected one card for {mechanism_id} in {root}")
    return matches[0]


def _replace_todo(text: str, parameter: str, low: float, high: float, source: str) -> str:
    inline = f"{{name: {parameter},"
    inline_at = text.find(inline)
    if inline_at >= 0:
        line_start = text.rfind("\n", 0, inline_at) + 1
        line_end = text.find("\n", inline_at)
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        if "source: TODO_estimate" not in line:
            return text
        indent = line[: len(line) - len(line.lstrip())]
        unit = _inline_unit(line)
        block = (
            f"{indent}- name: {parameter}\n"
            f"{indent}  unit: {unit}\n"
            f"{indent}  low: {low}\n"
            f"{indent}  high: {high}\n"
            f'{indent}  source: "{source}"'
        )
        return text[:line_start] + block + text[line_end:]
    needle = f"name: {parameter}\n"
    start = text.find(needle)
    if start < 0:
        return text
    end = _parameter_block_end(text, start)
    source_at = text.find("source: TODO_estimate", start, end)
    if source_at < 0:
        return text
    line_end = text.find("\n", source_at)
    if line_end < 0 or line_end > end:
        line_end = end
    replacement = f'low: {low}\n  high: {high}\n  source: "{source}"'
    return text[:source_at] + replacement + text[line_end:]


def _parameter_block_end(text: str, start: int) -> int:
    """Index just after the parameter that begins at ``start`` (spec §3.1)."""
    ends = [
        text.find(marker, start + 1)
        for marker in ("\n- name:", "\nhorizon:", "\nstate_dependence:", "\nassumptions:")
    ]
    found = [index for index in ends if index >= 0]
    return min(found) if found else len(text)


def _inline_unit(line: str) -> str:
    marker = "unit:"
    at = line.find(marker)
    if at < 0:
        raise ValueError(f"inline parameter has no unit: {line}")
    rest = line[at + len(marker) :].strip()
    return rest.split(",")[0].strip()


def _now() -> datetime:
    return datetime.now(UTC)
