"""Closed set of world-state transforms (spec §4.3, §6.3).

Each transform reads point-in-time observation frames and returns a ``Quantity``. It does not
query a database and it does not impute holes: a gap is the caller's skip, not a filled value.
Cross-dimension conversions that the unit registry refuses live here only when the transform's
name is that conversion (a percent-of-capacity level into a share; a difference of two percent
levels into basis points).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible, convert
from macrosys.risk.regime import absorption_ratio, two_state_variance_regime

OPS = frozenset(
    {
        "latest",
        "ratio",
        "days_of_cover",
        "spread",
        "pct_change",
        "zscore",
        "regime_absorption",
        "regime_variance",
    }
)

# Transforms whose result unit is fixed by the operation. ``latest`` and ``ratio`` take the recipe unit.
OUTPUT_UNITS: dict[str, str] = {
    "days_of_cover": "d",
    "spread": "bp",
    "pct_change": "pct",
    "zscore": "ratio",
    "regime_absorption": "share",
    "regime_variance": "share",
}

_CALL = re.compile(r"^(?P<op>[a-z_]+)(?:\((?P<args>.*)\))?$")

# Worst-of order for quality_flag. Higher is worse (spec §9.4, §9.6).
QUALITY_RANK: dict[str, int] = {
    "ok": 0,
    "reconstructed_pubtime": 1,
    "parser_changed": 2,
    "unofficial": 3,
    "estimated": 4,
    "imputed": 5,
}


@dataclass(frozen=True)
class ParsedTransform:
    """A transform call after the recipe string has been parsed."""

    op: str
    series: tuple[str, ...]
    numbers: tuple[float, ...]
    keywords: dict[str, float]


@dataclass(frozen=True)
class TransformResult:
    """One derived quantity plus the input vintages that produced it (spec §4.3, §4.7).

    ``period_end`` is the reference-period end of the rows that entered the value, so refresh
    can reject a result whose overlapping date is older than the recipe allows.
    """

    quantity: Quantity
    quality_flag: str
    vintages: tuple[tuple[str, str], ...]
    period_end: date
    imputed: bool = False


class TransformError(ValueError):
    """The transform cannot be evaluated on these rows. Refresh records it and writes nothing."""


def parse_transform(text: str, inputs: list[str]) -> ParsedTransform:
    """Parse a recipe transform string against ``inputs`` (spec §4.3).

    Series arguments must be members of ``inputs``. Positional numbers and ``key=value`` numbers
    are the only other arguments. ``latest`` takes no arguments and uses the single input.
    """
    match = _CALL.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"transform {text!r} is not a call")
    op = match.group("op")
    if op not in OPS:
        raise ValueError(f"transform {op!r} is not in the closed set")
    raw = match.group("args")
    parts = [] if raw is None or not raw.strip() else [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"transform {text!r} has an empty argument")
    series: list[str] = []
    numbers: list[float] = []
    keywords: dict[str, float] = {}
    known = set(inputs)
    for part in parts:
        if "=" in part:
            key, _, value = part.partition("=")
            keywords[key.strip()] = _number(value.strip(), part)
            continue
        if part in known:
            series.append(part)
            continue
        numbers.append(_number(part, part))
    if op == "latest":
        if parts:
            raise ValueError("latest takes no arguments")
        if len(inputs) != 1:
            raise ValueError("latest requires exactly one input")
        series = list(inputs)
    _check_shape(op, series, numbers, keywords, inputs)
    return ParsedTransform(op=op, series=tuple(series), numbers=tuple(numbers), keywords=keywords)


def worst_quality(flags: list[str], *, imputed: bool) -> str:
    """Worst input flag, and at least ``estimated`` when the transform imputes (spec §4.3, §9.4)."""
    if not flags:
        raise ValueError("quality propagation needs at least one flag")
    unknown = [flag for flag in flags if flag not in QUALITY_RANK]
    if unknown:
        raise ValueError(f"unknown quality_flag {unknown}")
    worst = max(flags, key=lambda flag: QUALITY_RANK[flag])
    if imputed and QUALITY_RANK[worst] < QUALITY_RANK["estimated"]:
        return "estimated"
    return worst


def apply_transform(parsed: ParsedTransform, frames: dict[str, pd.DataFrame], unit: str) -> TransformResult:
    """Apply ``parsed`` to point-in-time frames and return one quantity (spec §4.3).

    ``frames`` are already restricted to rows published by the caller's ``as_of``. Null values
    are absent, not zeros. The result provenance is the transform name; the caller adds the
    recipe id and vintage ids when it writes the row.
    """
    op = parsed.op
    if op == "latest":
        return _latest(parsed.series[0], frames, unit)
    if op == "ratio":
        return _ratio(parsed.series, frames, unit)
    if op == "days_of_cover":
        return _days_of_cover(parsed.series, parsed.numbers[0], frames)
    if op == "spread":
        return _spread(parsed.series, frames)
    if op == "pct_change":
        return _pct_change(parsed.series[0], int(parsed.numbers[0]), frames)
    if op == "zscore":
        return _zscore(parsed.series[0], int(parsed.numbers[0]), frames)
    if op == "regime_absorption":
        return _absorption(parsed.series, int(parsed.keywords["window"]), int(parsed.keywords["n_components"]), frames)
    if op == "regime_variance":
        return _variance(parsed.series[0], frames)
    raise TransformError(f"unknown transform {op}")


def _number(text: str, label: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"transform argument {label!r} is not a series id or a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"transform argument {label!r} is not finite")
    return value


def _check_shape(
    op: str,
    series: list[str],
    numbers: list[float],
    keywords: dict[str, float],
    inputs: list[str],
) -> None:
    unused = [series_id for series_id in inputs if series_id not in series]
    if unused:
        raise ValueError(f"{op} does not use inputs {unused}")
    if op == "ratio" and (len(series) != 2 or numbers or keywords):
        raise ValueError("ratio(a, b) takes two series")
    if op == "days_of_cover" and (len(series) != 2 or len(numbers) != 1 or keywords):
        raise ValueError("days_of_cover(stock, flow_per_period, period_days) takes two series and a number")
    if op == "days_of_cover" and numbers[0] <= 0:
        raise ValueError("period_days must be positive")
    if op == "spread" and (len(series) != 2 or numbers or keywords):
        raise ValueError("spread(a, b) takes two series")
    if op == "pct_change" and (len(series) != 1 or len(numbers) != 1 or keywords):
        raise ValueError("pct_change(a, periods) takes one series and a period count")
    if op == "pct_change" and (int(numbers[0]) != numbers[0] or numbers[0] < 1):
        raise ValueError("pct_change periods must be an integer >= 1")
    if op == "zscore" and (len(series) != 1 or len(numbers) != 1 or keywords):
        raise ValueError("zscore(a, window) takes one series and a window")
    if op == "zscore" and (int(numbers[0]) != numbers[0] or numbers[0] < 3):
        raise ValueError("zscore window must be an integer >= 3")
    if op == "regime_absorption":
        if len(series) < 2 or numbers or set(keywords) != {"window", "n_components"}:
            raise ValueError("regime_absorption(series..., window=, n_components=) is the only form")
        window = keywords["window"]
        components = keywords["n_components"]
        if int(window) != window or window < 2:
            raise ValueError("absorption window must be an integer >= 2")
        if int(components) != components or components < 1 or components > len(series):
            raise ValueError("n_components must be an integer between 1 and the number of series")
    if op == "regime_variance" and (len(series) != 1 or numbers or keywords):
        raise ValueError("regime_variance(a) takes one series")


def _clean(frame: pd.DataFrame, series_id: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise TransformError(f"missing input {series_id}")
    out = frame.dropna(subset=["value"]).copy()
    if out.empty:
        raise TransformError(f"missing input {series_id}")
    out["value"] = out["value"].astype(float)
    return out.sort_values(["ref_period_end", "ref_period_start"], kind="mergesort")


def _latest_row(frame: pd.DataFrame) -> pd.Series:
    return frame.iloc[-1]


def _flag(row: pd.Series) -> str:
    flag = row["quality_flag"]
    if flag is None or (isinstance(flag, float) and math.isnan(flag)):
        return "ok"
    return str(flag)


def _vintage(series_id: str, row: pd.Series) -> tuple[str, str]:
    return series_id, str(row["vintage_id"])


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()  # type: ignore[arg-type]


def _result(
    value: float,
    unit: str,
    provenance: str,
    flags: list[str],
    vintages: list[tuple[str, str]],
    period_end: object,
) -> TransformResult:
    if not math.isfinite(value):
        raise TransformError("transform result is not finite")
    return TransformResult(
        quantity=Quantity(value=value, unit=unit, provenance=provenance),
        quality_flag=worst_quality(flags, imputed=False),
        vintages=tuple(vintages),
        period_end=_as_date(period_end),
    )


def _as_unit(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return value
    if compatible(from_unit, to_unit):
        return convert(value, from_unit, to_unit)
    # [J] FRED publishes capacity utilization as a percent level (76.3 means 76.3 percent).
    # B03 reads a share. Dividing by 100 is that definition, not an imputed observation.
    if from_unit == "pct" and to_unit == "share":
        return value / 100.0
    raise TransformError(f"cannot express {from_unit} as {to_unit}")


def _latest(series_id: str, frames: dict[str, pd.DataFrame], unit: str) -> TransformResult:
    frame = _clean(frames[series_id], series_id)
    row = _latest_row(frame)
    value = _as_unit(float(row["value"]), str(row["unit"]), unit)
    return _result(value, unit, "latest", [_flag(row)], [_vintage(series_id, row)], row["ref_period_end"])


def _overlap(series: tuple[str, ...], frames: dict[str, pd.DataFrame]) -> tuple[pd.Series, ...]:
    cleaned = [_clean(frames[series_id], series_id) for series_id in series]
    common = set(cleaned[0]["ref_period_end"])
    for frame in cleaned[1:]:
        common &= set(frame["ref_period_end"])
    if not common:
        raise TransformError("inputs do not share a reference period")
    end = max(common)
    rows = []
    for frame in cleaned:
        match = frame.loc[frame["ref_period_end"] == end]
        rows.append(match.iloc[-1])
    return tuple(rows)


def _ratio(series: tuple[str, ...], frames: dict[str, pd.DataFrame], unit: str) -> TransformResult:
    left, right = _overlap(series, frames)
    if float(right["value"]) == 0.0:
        raise TransformError("ratio divisor is zero")
    if str(left["unit"]) != str(right["unit"]) and not compatible(str(left["unit"]), str(right["unit"])):
        raise TransformError(f"ratio units {left['unit']} and {right['unit']} differ")
    denominator = float(right["value"])
    if compatible(str(left["unit"]), str(right["unit"])):
        denominator = convert(denominator, str(right["unit"]), str(left["unit"]))
    return _result(
        float(left["value"]) / denominator,
        unit,
        "ratio",
        [_flag(left), _flag(right)],
        [_vintage(series[0], left), _vintage(series[1], right)],
        left["ref_period_end"],
    )


def _days_of_cover(series: tuple[str, ...], period_days: float, frames: dict[str, pd.DataFrame]) -> TransformResult:
    """Stock over flow, times the flow's period length in days (spec §4.3 inventories)."""
    stock, flow = _overlap(series, frames)
    if float(flow["value"]) == 0.0:
        raise TransformError("days of cover are undefined when the flow is zero")
    if not compatible(str(stock["unit"]), str(flow["unit"])):
        raise TransformError(f"days_of_cover units {stock['unit']} and {flow['unit']} differ")
    flow_value = convert(float(flow["value"]), str(flow["unit"]), str(stock["unit"]))
    days = (float(stock["value"]) / flow_value) * period_days
    return _result(
        days,
        "d",
        "days_of_cover",
        [_flag(stock), _flag(flow)],
        [_vintage(series[0], stock), _vintage(series[1], flow)],
        stock["ref_period_end"],
    )


def _spread(series: tuple[str, ...], frames: dict[str, pd.DataFrame]) -> TransformResult:
    """Difference of two rate levels, expressed in basis points (spec §4.3 funding).

    Two ``pct`` levels differ by percentage points; one percentage point is 100 bp. Two ``pp``
    or ``bp`` levels convert through the unit registry. This is the spread transform, not a
    general percent-to-basis-point cast of a single series.
    """
    left, right = _overlap(series, frames)
    left_unit, right_unit = str(left["unit"]), str(right["unit"])
    if left_unit != right_unit and not compatible(left_unit, right_unit):
        raise TransformError(f"spread units {left_unit} and {right_unit} differ")
    right_value = float(right["value"])
    if compatible(left_unit, right_unit) and left_unit != right_unit:
        right_value = convert(right_value, right_unit, left_unit)
    diff = float(left["value"]) - right_value
    if left_unit == "pct":
        bp = diff * 100.0
    elif left_unit == "bp":
        bp = diff
    elif compatible(left_unit, "bp"):
        bp = convert(diff, left_unit, "bp")
    else:
        raise TransformError(f"spread does not know how to express {left_unit} in bp")
    return _result(
        bp,
        "bp",
        "spread",
        [_flag(left), _flag(right)],
        [_vintage(series[0], left), _vintage(series[1], right)],
        left["ref_period_end"],
    )


def _pct_change(series_id: str, periods: int, frames: dict[str, pd.DataFrame]) -> TransformResult:
    frame = _clean(frames[series_id], series_id)
    if len(frame) <= periods:
        raise TransformError("insufficient_history")
    current = frame.iloc[-1]
    previous = frame.iloc[-1 - periods]
    if float(previous["value"]) == 0.0:
        raise TransformError("pct_change base is zero")
    if str(current["unit"]) != str(previous["unit"]):
        raise TransformError("pct_change vintages do not share a unit")
    change = (float(current["value"]) / float(previous["value"]) - 1.0) * 100.0
    return _result(
        change,
        "pct",
        "pct_change",
        [_flag(current), _flag(previous)],
        [_vintage(series_id, current), _vintage(series_id, previous)],
        current["ref_period_end"],
    )


def _zscore(series_id: str, window: int, frames: dict[str, pd.DataFrame]) -> TransformResult:
    frame = _clean(frames[series_id], series_id)
    if len(frame) < window:
        raise TransformError("insufficient_history")
    block = frame.iloc[-window:]
    values = block["value"].to_numpy(dtype=float)
    center = float(values.mean())
    # [J] Sample standard deviation: a trailing window is a sample, not the population.
    scale = float(pd.Series(values).std(ddof=1))
    if scale == 0.0 or not math.isfinite(scale):
        raise TransformError("zscore window has zero variance")
    current = block.iloc[-1]
    return _result(
        (float(current["value"]) - center) / scale,
        "ratio",
        "zscore",
        [_flag(row) for _, row in block.iterrows()],
        [_vintage(series_id, current)],
        current["ref_period_end"],
    )


def _panel(series: tuple[str, ...], frames: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, list[str]]:
    cleaned = []
    for series_id in series:
        frame = _clean(frames[series_id], series_id)
        slim = frame.loc[:, ["ref_period_end", "value", "quality_flag", "vintage_id"]].copy()
        slim = slim.drop_duplicates("ref_period_end", keep="last").set_index("ref_period_end")
        slim.columns = pd.MultiIndex.from_product([[series_id], slim.columns])
        cleaned.append(slim)
    panel = cleaned[0].join(cleaned[1:], how="inner").sort_index()
    if panel.empty:
        raise TransformError("inputs do not share a reference period")
    flags = []
    last = panel.iloc[-1]
    for series_id in series:
        flag = last[(series_id, "quality_flag")]
        flags.append("ok" if flag is None or (isinstance(flag, float) and math.isnan(flag)) else str(flag))
    return panel, flags


def _absorption(series: tuple[str, ...], window: int, n_components: int, frames: dict[str, pd.DataFrame]) -> TransformResult:
    """Last trailing absorption ratio (spec §6.3; Kritzman, Li, Page & Rigobon 2011)."""
    panel, flags = _panel(series, frames)
    if len(panel) < window:
        raise TransformError("insufficient_history")
    values = panel.loc[:, [(series_id, "value") for series_id in series]].to_numpy(dtype=float)
    ratios = absorption_ratio(values, n_components, window)
    if isinstance(ratios, pd.Series):
        latest = ratios.iloc[-1]
    else:
        latest = ratios[-1]
    if latest is None or not math.isfinite(float(latest)):
        raise TransformError("insufficient_history")
    vintages = []
    last = panel.iloc[-1]
    for series_id in series:
        vintages.append((series_id, str(last[(series_id, "vintage_id")])))
    return _result(float(latest), "share", "regime_absorption", flags, vintages, panel.index[-1])


def _variance(series_id: str, frames: dict[str, pd.DataFrame]) -> TransformResult:
    """Smoothed probability of the high-variance state (spec §6.3; Hamilton 1989).

    The EM iteration cap and tolerance are numerical controls, the same kind as a loop's
    ``max_iter``, not estimated economic magnitudes. A sample that does not converge is not
    written.
    """
    frame = _clean(frames[series_id], series_id)
    values = frame["value"].to_numpy(dtype=float)
    if len(values) < 30:
        raise TransformError("insufficient_history")
    try:
        probabilities, params = two_state_variance_regime(values, max_iter=80, tol=1e-4)
    except ValueError as exc:
        raise TransformError(str(exc)) from exc
    if not params.converged:
        raise TransformError("regime_variance_not_converged")
    probability = float(probabilities.smoothed[-1])
    row = frame.iloc[-1]
    return _result(probability, "share", "regime_variance", [_flag(row)], [_vintage(series_id, row)], row["ref_period_end"])
