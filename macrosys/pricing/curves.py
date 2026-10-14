"""Treasury and GSW zero curves (spec §3.3 C03, C04; §8.5).

Par bootstrap is semi-annual. GSW zeros are already continuously compounded and are
preferred when the snapshot has them. Year fractions are constant-maturity tenors
(months/12 or whole years), not actual/actual bond dates.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, model_validator
from scipy.optimize import least_squares

from macrosys.core.quantity import Quantity
from macrosys.pricing._map import nominal_block, series_map
from macrosys.pricing._series import latest
from macrosys.pricing.errors import CurveError
from macrosys.pricing.types import PathWithRange
from macrosys.store.observations import PointInTime

Compounding = Literal["semi_annual", "continuous", "annual"]


class RatePoint(BaseModel):
    """One curve node: a tenor in years and a yield in percent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenor_y: float
    rate: Quantity

    @model_validator(mode="after")
    def _pct(self) -> RatePoint:
        if self.tenor_y <= 0.0:
            raise ValueError("tenor must be positive")
        if self.rate.unit != "pct":
            raise ValueError(f"yield unit must be pct, got {self.rate.unit}")
        return self


def _ordered(points: tuple[RatePoint, ...]) -> tuple[RatePoint, ...]:
    if not points:
        raise ValueError("curve has no points")
    tenors = [p.tenor_y for p in points]
    if any(b - a <= 1e-12 for a, b in zip(tenors, tenors[1:], strict=False)):
        raise ValueError("tenors must be strictly increasing")
    return points


class ParCurve(BaseModel):
    """Constant-maturity par yields, tenor in years to annual percent (spec §3.3 C03)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    points: tuple[RatePoint, ...]

    @model_validator(mode="after")
    def _check(self) -> ParCurve:
        _ordered(self.points)
        return self

    @classmethod
    def from_pairs(cls, pairs: list[tuple[float, float]], *, provenance: str = "par") -> ParCurve:
        """Build a curve from ``(tenor years, par yield percent)`` pairs."""
        ordered = sorted(pairs, key=lambda item: item[0])
        return cls(
            points=tuple(
                RatePoint(tenor_y=tenor, rate=Quantity(value=yield_pct, unit="pct", provenance=provenance))
                for tenor, yield_pct in ordered
            )
        )

    def yield_at(self, tenor_y: float) -> Quantity:
        """Par yield at ``tenor_y``, linear in the year fraction between quoted tenors.

        No extrapolation. A tenor outside the quoted span raises ``CurveError``.
        """
        return _interp(self.points, tenor_y, provenance="par_interp")


class ZeroCurve(BaseModel):
    """Zero-coupon yields in percent, with the compounding convention that defines them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    points: tuple[RatePoint, ...]
    compounding: Compounding
    source: str

    @model_validator(mode="after")
    def _check(self) -> ZeroCurve:
        _ordered(self.points)
        return self

    @classmethod
    def from_pairs(
        cls,
        pairs: list[tuple[float, float]],
        *,
        compounding: Compounding,
        source: str,
        provenance: str | None = None,
    ) -> ZeroCurve:
        """Build a zero curve from ``(tenor years, zero yield percent)`` pairs."""
        ordered = sorted(pairs, key=lambda item: item[0])
        return cls(
            points=tuple(
                RatePoint(
                    tenor_y=tenor,
                    rate=Quantity(value=yield_pct, unit="pct", provenance=provenance or source),
                )
                for tenor, yield_pct in ordered
            ),
            compounding=compounding,
            source=source,
        )

    @property
    def tenors_y(self) -> tuple[float, ...]:
        """Quoted tenors in years."""
        return tuple(p.tenor_y for p in self.points)

    def zero_at(self, tenor_y: float) -> Quantity:
        """Zero rate at ``tenor_y``. Linear in the quoted zero, no extrapolation.

        Interpolation is in the curve's own compounding, not in discount factors. [J]
        """
        return _interp(self.points, tenor_y, provenance=f"{self.source}:interp")


class NelsonSiegel(BaseModel):
    """Nelson–Siegel parameters fitted to a zero curve. Betas are in the curve's percent units."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    beta0: Quantity
    beta1: Quantity
    beta2: Quantity
    tau: Quantity
    rmse: Quantity


def _interp(points: tuple[RatePoint, ...], tenor_y: float, *, provenance: str) -> Quantity:
    first, last = points[0].tenor_y, points[-1].tenor_y
    if tenor_y < first - 1e-12 or tenor_y > last + 1e-12:
        raise CurveError(f"tenor {tenor_y}y is outside [{first}, {last}]y; no extrapolation")
    for point in points:
        if abs(point.tenor_y - tenor_y) <= 1e-12:
            return point.rate
    for left, right in zip(points, points[1:], strict=False):
        if left.tenor_y <= tenor_y <= right.tenor_y:
            span = right.tenor_y - left.tenor_y
            weight = 0.0 if span == 0.0 else (tenor_y - left.tenor_y) / span
            return _lerp_rate(left.rate, right.rate, weight, provenance)
    raise CurveError(f"tenor {tenor_y}y was not bracketed")


def _lerp_rate(left: Quantity, right: Quantity, weight: float, provenance: str) -> Quantity:
    def blend(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return (1.0 - weight) * a + weight * b

    value = blend(left.value, right.value)
    low = blend(left.low, right.low)
    high = blend(left.high, right.high)
    if value is None and (low is None or high is None):
        value = blend(left.mid, right.mid)
    return Quantity(value=value, unit="pct", low=low, high=high, provenance=provenance)


def _point(q: Quantity) -> float:
    if q.value is not None:
        return float(q.value)
    return float(q.mid)


def tenor_years(label: str) -> float:
    """Parse ``6M`` or ``10Y`` into a year fraction (months/12, or whole years)."""
    text = label.strip().upper()
    if len(text) < 2 or text[-1] not in {"M", "Y"}:
        raise ValueError(f"tenor label {label!r} is not like 6M or 10Y")
    try:
        number = float(text[:-1])
    except ValueError as exc:
        raise ValueError(f"tenor label {label!r} is not like 6M or 10Y") from exc
    if number <= 0.0:
        raise ValueError(f"tenor label {label!r} is not positive")
    return number / 12.0 if text[-1] == "M" else number


def bootstrap_zero(par: ParCurve) -> ZeroCurve:
    """Bootstrap semi-annually compounded zeros from par yields (spec §3.3 C03, C04).

    Par yields are annual percent on the US Treasury constant-maturity convention,
    semi-annually compounded. Between quoted tenors the par yield is linear in the
    year fraction; nothing is extrapolated, so the shortest quote must sit at or
    before 0.5 years. A knot at or before 0.5 years is a zero on that same
    bond-equivalent convention, ``DF(t) = 1 / (1 + y/200)^(2t)``. Longer knots are
    par bonds with semi-annual coupons of ``y/2`` per unit face, on every half-year
    from 0.5 to the longest quote. The stored zero is the semi-annually compounded
    spot rate in percent. A flat par curve is an exact fixed point: every zero
    equals the par yield. Day counts are the constant-maturity tenors, not
    actual/actual coupon dates. [M] for the semi-annual par convention; [J] for
    linear par interpolation and the year-fraction grid.
    """
    t_min = par.points[0].tenor_y
    t_max = par.points[-1].tenor_y
    if t_min > 0.5 + 1e-12:
        raise CurveError(
            f"shortest par tenor is {t_min}y; semi-annual bootstrap needs a quote at or before 0.5y"
        )
    knots: list[float] = []

    def _add(tenor: float) -> None:
        if any(abs(tenor - got) <= 1e-10 for got in knots):
            return
        knots.append(tenor)

    for point in par.points:
        if point.tenor_y <= 0.5 + 1e-12:
            _add(point.tenor_y)
    n_max = int(math.floor(t_max * 2.0 + 1e-9))
    for n in range(1, n_max + 1):
        tenor = n / 2.0
        if tenor <= t_max + 1e-12:
            _add(tenor)
    knots.sort()
    dfs: list[tuple[float, float]] = []
    zeros: list[RatePoint] = []
    for tenor in knots:
        y_pct = _point(par.yield_at(tenor))
        y = y_pct / 100.0
        if tenor <= 0.5 + 1e-12:
            df = (1.0 + y / 2.0) ** (-2.0 * tenor)
        else:
            n = int(round(tenor * 2.0))
            if abs(n / 2.0 - tenor) > 1e-8:
                raise CurveError(f"coupon knot {tenor}y is not a whole number of half-years")
            coupon = y / 2.0
            accrued = 0.0
            for k in range(1, n):
                accrued += coupon * _df_at(dfs, k / 2.0)
            df = (1.0 - accrued) / (1.0 + coupon)
        if df <= 0.0:
            raise CurveError(f"non-positive discount factor at {tenor}y")
        dfs.append((tenor, df))
        zero_pct = 200.0 * (df ** (-1.0 / (2.0 * tenor)) - 1.0)
        zeros.append(RatePoint(tenor_y=tenor, rate=Quantity(value=zero_pct, unit="pct", provenance="par_bootstrap")))
    return ZeroCurve(points=tuple(zeros), compounding="semi_annual", source="par_bootstrap")


def _df_at(dfs: list[tuple[float, float]], tenor: float) -> float:
    for got, df in dfs:
        if abs(got - tenor) <= 1e-8:
            return df
    raise CurveError(f"discount factor at {tenor}y is missing; the short end was not bootstrapped")


def forward_rate(zero: ZeroCurve, t1: float, t2: float) -> Quantity:
    """Forward rate from ``t1`` to ``t2`` in the curve's compounding, percent (spec §3.3 C03).

    Semi-annual: ``(1+z/200)^(2t)`` compounds. Continuous: ``f = (z2·t2 − z1·t1)/(t2−t1)``.
    Annual: ``(1+z/100)^t`` compounds. The forward from 0 to ``t2`` is the zero at ``t2``.
    Rates are not converted across conventions. No extrapolation past the curve.
    """
    if t2 <= t1:
        raise CurveError(f"forward end {t2} is not after start {t1}")
    if t1 < 0.0:
        raise CurveError("forward start is negative")
    z2 = _point(zero.zero_at(t2))
    if t1 == 0.0:
        rate = z2
    else:
        z1 = _point(zero.zero_at(t1))
        rate = _forward(z1, t1, z2, t2, zero.compounding)
    return Quantity(value=rate, unit="pct", provenance=f"forward:{zero.source}")


def _forward(z1: float, t1: float, z2: float, t2: float, compounding: Compounding) -> float:
    span = t2 - t1
    if compounding == "continuous":
        return (z2 * t2 - z1 * t1) / span
    if compounding == "annual":
        growth = (1.0 + z2 / 100.0) ** t2 / (1.0 + z1 / 100.0) ** t1
        return 100.0 * (growth ** (1.0 / span) - 1.0)
    if compounding == "semi_annual":
        growth = (1.0 + z2 / 200.0) ** (2.0 * t2) / (1.0 + z1 / 200.0) ** (2.0 * t1)
        return 200.0 * (growth ** (1.0 / (2.0 * span)) - 1.0)
    raise CurveError(f"unknown compounding {compounding}")


def to_semi_annual_pct(rate_pct: float, compounding: Compounding) -> float:
    """Convert one zero rate in percent to a semi-annually compounded percent."""
    if compounding == "semi_annual":
        return rate_pct
    if compounding == "continuous":
        return 200.0 * (math.exp(rate_pct / 200.0) - 1.0)
    if compounding == "annual":
        return 200.0 * (math.sqrt(1.0 + rate_pct / 100.0) - 1.0)
    raise CurveError(f"unknown compounding {compounding}")


def nelson_siegel_rate(tenor_y: float, beta0: float, beta1: float, beta2: float, tau: float) -> float:
    """Nelson–Siegel zero at one tenor. Loadings use the usual ``(1−e^{−x})/x`` factor."""
    if tau <= 0.0:
        raise CurveError("Nelson–Siegel tau must be positive")
    if tenor_y <= 0.0:
        raise CurveError("Nelson–Siegel tenor must be positive")
    x = tenor_y / tau
    if x < 1e-6:
        factor = 1.0 - x / 2.0 + x * x / 6.0
        decay = 1.0 - x + x * x / 2.0
    else:
        decay = math.exp(-x)
        factor = (1.0 - decay) / x
    return beta0 + beta1 * factor + beta2 * (factor - decay)


def fit_nelson_siegel(zero: ZeroCurve) -> NelsonSiegel:
    """Fit Nelson–Siegel to the curve's own zero rates with scipy least squares (spec §3.3 C03).

    Tau is gridded and the three betas are linear at each tau; least squares then
    polishes all four parameters. The fit is on the quoted compounding, not a
    converted yield. Fewer than four tenors refuse. This is a smoother, not a
    premium estimate.
    """
    if len(zero.points) < 4:
        raise CurveError("Nelson–Siegel needs at least four tenors")
    tenors = np.array([p.tenor_y for p in zero.points], dtype=float)
    yields = np.array([_point(p.rate) for p in zero.points], dtype=float)
    start = _ns_start(tenors, yields)

    def residual(theta: np.ndarray) -> np.ndarray:
        b0, b1, b2, tau = (float(x) for x in theta)
        fitted = np.array([nelson_siegel_rate(float(t), b0, b1, b2, tau) for t in tenors])
        return fitted - yields

    polished = least_squares(
        residual,
        np.array(start, dtype=float),
        bounds=([-20.0, -20.0, -20.0, 0.05], [20.0, 20.0, 20.0, 30.0]),
        ftol=1e-12,
        xtol=1e-12,
    )
    if not polished.success:
        raise CurveError(f"Nelson–Siegel fit failed: {polished.message}")
    b0, b1, b2, tau = (float(x) for x in polished.x)
    fitted = np.array([nelson_siegel_rate(float(t), b0, b1, b2, tau) for t in tenors])
    rmse = float(np.sqrt(np.mean((fitted - yields) ** 2)))
    prov = f"nelson_siegel:{zero.source}"
    return NelsonSiegel(
        beta0=Quantity(value=b0, unit="pct", provenance=prov),
        beta1=Quantity(value=b1, unit="pct", provenance=prov),
        beta2=Quantity(value=b2, unit="pct", provenance=prov),
        tau=Quantity(value=tau, unit="y", provenance=prov),
        rmse=Quantity(value=rmse, unit="pct", provenance=prov),
    )


def _ns_loadings(tenors: np.ndarray, tau: float) -> np.ndarray:
    rows = []
    for tenor in tenors:
        x = float(tenor) / tau
        decay = math.exp(-x)
        factor = (1.0 - decay) / x
        rows.append((1.0, factor, factor - decay))
    return np.array(rows, dtype=float)


def _ns_start(tenors: np.ndarray, yields: np.ndarray) -> tuple[float, float, float, float]:
    best: tuple[float, float, float, float] | None = None
    best_sse = math.inf
    for tau in np.linspace(0.25, 10.0, 40):
        design = _ns_loadings(tenors, float(tau))
        betas, *_ = np.linalg.lstsq(design, yields, rcond=None)
        sse = float(np.sum((design @ betas - yields) ** 2))
        if sse < best_sse:
            best_sse = sse
            best = (float(betas[0]), float(betas[1]), float(betas[2]), float(tau))
    if best is None:
        raise CurveError("Nelson–Siegel grid produced no start")
    return best


def load_par_curve(snapshot: PointInTime, *, ccy: str = "USD", as_of: date | None = None) -> ParCurve:
    """Read ``TREASURY:PAR:<tenor>`` (or the mapped par pattern) into a par curve (spec §9.2, §3.3).

    The snapshot is already point-in-time. ``as_of`` additionally drops reference
    periods that start after that date. Missing tenors are skipped. An empty curve raises.
    """
    block = _nominal(ccy)
    pattern = block.get("par_pattern")
    labels = block.get("par_tenors") or []
    if not pattern or not labels:
        raise CurveError(f"{ccy} has no par-yield series in the pricing map")
    pairs: list[tuple[float, Quantity]] = []
    for label in labels:
        series_id = str(pattern).format(tenor=label)
        got = latest(snapshot, series_id, as_of)
        if got is None:
            continue
        pairs.append((tenor_years(str(label)), got))
    if not pairs:
        raise CurveError(f"no {pattern} par yields in the snapshot for {ccy}")
    pairs.sort(key=lambda item: item[0])
    return ParCurve(points=tuple(RatePoint(tenor_y=tenor, rate=rate) for tenor, rate in pairs))


def load_zero_curve(snapshot: PointInTime, *, ccy: str = "USD", as_of: date | None = None) -> ZeroCurve:
    """Zero curve for ``ccy``, preferring published zeros over a par bootstrap (spec §3.3 C03).

    USD uses ``FEDGSW:SVENYnn`` when any tenor is present. Those yields stay
    continuously compounded, as in the GSW file. If none are present, Treasury par
    yields are bootstrapped. EUR uses the ECB Svensson spot series in the map,
    stored in the compounding the map declares (annual). A currency with neither
    raises ``CurveError`` naming the missing pattern.
    """
    block = _nominal(ccy)
    gsw = _load_gsw(snapshot, block, as_of)
    if gsw is not None:
        return gsw
    spots = _load_spots(snapshot, block, as_of)
    if spots is not None:
        return spots
    if block.get("par_pattern"):
        return bootstrap_zero(load_par_curve(snapshot, ccy=ccy, as_of=as_of))
    raise CurveError(f"no nominal zero or par series mapped for {ccy}")


def _nominal(ccy: str) -> dict:
    block = nominal_block(ccy)
    if block is None:
        known = ", ".join(sorted(series_map().get("nominal_zero") or {}))
        raise CurveError(f"no nominal curve mapping for {ccy}; mapped: {known}")
    return block


def _load_gsw(snapshot: PointInTime, block: dict, as_of: date | None) -> ZeroCurve | None:
    pattern = block.get("gsw_pattern")
    if not pattern:
        return None
    points: list[RatePoint] = []
    for year in range(1, 31):
        series_id = str(pattern).format(nn=year)
        got = latest(snapshot, series_id, as_of)
        if got is None:
            continue
        points.append(RatePoint(tenor_y=float(year), rate=got))
    if not points:
        return None
    compounding = block.get("gsw_compounding") or "continuous"
    return ZeroCurve(points=tuple(points), compounding=compounding, source="FEDGSW")


def _load_spots(snapshot: PointInTime, block: dict, as_of: date | None) -> ZeroCurve | None:
    pattern = block.get("spot_pattern")
    labels = block.get("spot_labels") or []
    if not pattern or not labels:
        return None
    points: list[RatePoint] = []
    for label in labels:
        series_id = str(pattern).format(label=label)
        got = latest(snapshot, series_id, as_of)
        if got is None:
            continue
        points.append(RatePoint(tenor_y=tenor_years(str(label)), rate=got))
    if not points:
        return None
    compounding = block.get("compounding") or "annual"
    return ZeroCurve(points=tuple(points), compounding=compounding, source=str(pattern).split("{", 1)[0] + "spot")


def curve_path(zero: ZeroCurve) -> PathWithRange:
    """Each zero as ``(tenor in quarters, rate)`` for callers that want a path."""
    return [(point.tenor_y * 4.0, point.rate) for point in zero.points]
