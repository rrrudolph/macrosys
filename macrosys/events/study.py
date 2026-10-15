"""Score predicted release-to-market signs and estimate response coefficients (spec §10.1 items 1, 5, 6, 10, 11; §10.2 item 1).

For each configured response and horizon the study joins surprise events to market outcomes and
reports: sign agreement on events whose standardized surprise clears the threshold, with a
binomial p-value against one half; the OLS slope of the response on the standardized surprise
(clipped at ``sigma_cap``) with HC1 standard errors; walk-forward out-of-sample sign accuracy using the slope's sign fitted
on purged training folds; and, for state-dependent signs, the same inside and outside the state.
The study scores the library's hypotheses; it does not choose them.
"""

from __future__ import annotations

import math
from datetime import datetime

import duckdb
import numpy as np
from scipy.stats import binomtest

from macrosys.core.quantity import Quantity
from macrosys.eval.scoring import purged_walk_forward_splits
from macrosys.events.config import EventsConfig, ResponseSpec, StateSign
from macrosys.events.outcomes import market_outcomes
from macrosys.events.schema import EventOutcome, RegimeSplit, ResponseStudy, SurpriseEvent
from macrosys.events.surprise import build_surprises, regime_state

_TRADING_DAYS_PER_WEEK = 5.0
_CALENDAR_DAYS_PER_WEEK = 7.0


class _Fit:
    def __init__(self, beta: float, se: float, r_squared: float) -> None:
        self.beta = beta
        self.se = se
        self.r_squared = r_squared

    @property
    def t_stat(self) -> float | None:
        return None if self.se == 0.0 or not math.isfinite(self.se) else self.beta / self.se


def ols_hc1(x: np.ndarray, y: np.ndarray) -> _Fit | None:
    """Slope of ``y`` on ``x`` with an intercept and HC1 (White, small-sample corrected) standard error."""
    n = len(x)
    if n < 3 or np.ptp(x) == 0.0:
        return None
    design = np.column_stack([np.ones(n), x])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    residuals = y - design @ coef
    bread = np.linalg.inv(design.T @ design)
    meat = design.T @ (design * (residuals**2)[:, None])
    cov = bread @ meat @ bread * (n / (n - 2))
    se = float(math.sqrt(max(cov[1, 1], 0.0)))
    total = float(np.sum((y - y.mean()) ** 2))
    r_squared = 0.0 if total == 0.0 else 1.0 - float(np.sum(residuals**2)) / total
    return _Fit(float(coef[1]), se, r_squared)


def sign_hits(z: np.ndarray, y: np.ndarray, predicted: np.ndarray, threshold: float) -> tuple[int, int]:
    """(hits, scored) over events with ``|z| >= threshold`` and a nonzero realized change."""
    mask = (np.abs(z) >= threshold) & (y != 0.0) & (predicted != 0)
    if not mask.any():
        return 0, 0
    expected = np.sign(z[mask]) * predicted[mask]
    hits = int(np.sum(expected == np.sign(y[mask])))
    return hits, int(mask.sum())


def _binomial_p(hits: int, n: int) -> float | None:
    if n == 0:
        return None
    return float(binomtest(hits, n, 0.5, alternative="two-sided").pvalue)


def _overlap_factor(events: list[SurpriseEvent], horizon_days: int) -> float:
    if len(events) < 2:
        return 1.0
    dates = sorted(event.event_date for event in events)
    spacing = float(np.median([(b - a).days for a, b in zip(dates[:-1], dates[1:], strict=True)]))
    if spacing <= 0.0:
        return 1.0
    calendar_horizon = horizon_days * _CALENDAR_DAYS_PER_WEEK / _TRADING_DAYS_PER_WEEK
    return max(1.0, calendar_horizon / spacing)


def _walk_forward(z: np.ndarray, y: np.ndarray, threshold: float, splits: int, label_horizon: int) -> tuple[float | None, int, float | None]:
    n = len(z)
    folds = min(splits, n // 6)
    if folds < 2:
        return None, 0, None
    hits = 0
    scored = 0
    betas: list[float] = []
    for train, test in purged_walk_forward_splits(n, folds, embargo=1, label_horizon=label_horizon):
        if len(train) < 3:
            continue
        fit = ols_hc1(z[train], y[train])
        if fit is None:
            continue
        betas.append(fit.beta)
        predicted = np.full(len(test), int(np.sign(fit.beta)) or 1)
        h, s = sign_hits(z[test], y[test], predicted, threshold)
        hits += h
        scored += s
    accuracy = None if scored == 0 else hits / scored
    beta_range = None if len(betas) < 2 else float(max(betas) - min(betas))
    return accuracy, scored, beta_range


def _predicted_signs(spec: ResponseSpec, states: list[bool | None]) -> np.ndarray:
    if isinstance(spec.sign, StateSign):
        return np.array(
            [0 if state is None else (spec.sign.when_true if state else spec.sign.when_false) for state in states],
            dtype=int,
        )
    return np.full(len(states), int(spec.sign), dtype=int)


def study_pair(
    spec: ResponseSpec,
    horizon_days: int,
    events: list[SurpriseEvent],
    outcomes: list[EventOutcome],
    states: dict[tuple[str, object], bool | None],
    config: EventsConfig,
    change_unit: str,
) -> ResponseStudy:
    """One release-to-market pair at one horizon (see module docstring)."""
    by_period = {o.ref_period_start: o for o in outcomes if o.horizon_days == horizon_days}
    paired: list[tuple[SurpriseEvent, EventOutcome]] = [
        (event, by_period[event.ref_period_start])
        for event in events
        if event.surprise_sigma is not None and event.ref_period_start in by_period and by_period[event.ref_period_start].change.value is not None
    ]
    regime_name = spec.regime_name
    state_list: list[bool | None] = [
        states.get((regime_name, event.ref_period_start)) if regime_name is not None else True for event, _ in paired
    ]
    z = np.clip(np.array([event.surprise_sigma for event, _ in paired], dtype=float), -config.sigma_cap, config.sigma_cap)
    y = np.array([outcome.change.value for _, outcome in paired], dtype=float)
    predicted = _predicted_signs(spec, state_list)
    n = len(paired)
    overlap = _overlap_factor([event for event, _ in paired], horizon_days)
    hits, scored = sign_hits(z, y, predicted, config.sign_threshold_sigma)
    fit = ols_hc1(z, y) if n >= config.min_events_for_regression else None
    beta = None
    if fit is not None:
        half_width = 1.96 * fit.se
        beta = Quantity(
            value=fit.beta,
            unit=change_unit,
            low=fit.beta - half_width,
            high=fit.beta + half_width,
            provenance=(
                f"event_study:{spec.release}->{spec.market}@{horizon_days}d per 1sd surprise; "
                f"n={n}; {paired[0][0].event_date.isoformat()}..{paired[-1][0].event_date.isoformat()}; "
                f"forecaster={config.forecaster.name}; HC1"
            ),
        )
    oos_accuracy, oos_n, beta_range = _walk_forward(
        z, y, config.sign_threshold_sigma, config.walk_forward_splits, label_horizon=1 if overlap > 1.0 else 0
    ) if n >= config.min_events_for_regression else (None, 0, None)
    regimes: list[RegimeSplit] = []
    if isinstance(spec.sign, StateSign):
        for state in (True, False):
            mask = np.array([s is state for s in state_list], dtype=bool)
            sub_sign = spec.sign.when_true if state else spec.sign.when_false
            sub_z, sub_y = z[mask], y[mask]
            sub_hits, sub_scored = sign_hits(sub_z, sub_y, np.full(int(mask.sum()), sub_sign), config.sign_threshold_sigma)
            sub_fit = ols_hc1(sub_z, sub_y) if int(mask.sum()) >= config.min_events_for_regression else None
            regimes.append(
                RegimeSplit(
                    regime=spec.sign.regime,
                    state=state,
                    predicted_sign=sub_sign,
                    n=int(mask.sum()),
                    n_signed=sub_scored,
                    sign_accuracy=None if sub_scored == 0 else sub_hits / sub_scored,
                    beta=None
                    if sub_fit is None
                    else Quantity(value=sub_fit.beta, unit=change_unit, provenance=f"event_study:{spec.release}->{spec.market}@{horizon_days}d {spec.sign.regime}={state}"),
                    t_stat=None if sub_fit is None else sub_fit.t_stat,
                )
            )
    return ResponseStudy(
        release_id=spec.release,
        market_id=spec.market,
        horizon_days=horizon_days,
        predicted_sign=None if isinstance(spec.sign, StateSign) else spec.sign,
        mechanisms=list(spec.mechanisms),
        reference=spec.reference,
        n=n,
        n_effective=float(n) / overlap if n > 0 else 0.0,
        n_signed=scored,
        sign_accuracy=None if scored == 0 else hits / scored,
        p_value=_binomial_p(hits, scored),
        beta=beta,
        t_stat=None if fit is None else fit.t_stat,
        r_squared=None if fit is None else fit.r_squared,
        oos_sign_accuracy=oos_accuracy,
        oos_n=oos_n,
        beta_fold_range=beta_range,
        regimes=regimes,
        first_event=paired[0][0].event_date if paired else None,
        last_event=paired[-1][0].event_date if paired else None,
    )


def run_study(conn: duckdb.DuckDBPyConnection, config: EventsConfig, as_of: datetime | None = None) -> list[ResponseStudy]:
    """Build events, measure outcomes, and score every configured response at every horizon.

    ``as_of`` bounds every read (events, outcomes, regime states) by ``published_at``; ``None``
    uses everything stored. Pairs with no joined events still return a row with ``n = 0`` so the
    report shows what the store cannot yet test.
    """
    events_by_release: dict[str, list[SurpriseEvent]] = {}
    outcomes_by_pair: dict[tuple[str, str], list[EventOutcome]] = {}
    states: dict[tuple[str, object], bool | None] = {}
    results: list[ResponseStudy] = []
    for spec in config.responses:
        if spec.release not in events_by_release:
            events_by_release[spec.release] = build_surprises(conn, config.release(spec.release), config, as_of)
        events = events_by_release[spec.release]
        key = (spec.release, spec.market)
        if key not in outcomes_by_pair:
            outcomes_by_pair[key] = market_outcomes(conn, events, config.market(spec.market), config.horizons_days, as_of)
        if spec.regime_name is not None:
            regime = config.regime(spec.regime_name)
            for event in events:
                state_key = (regime.name, event.ref_period_start)
                if state_key not in states:
                    states[state_key] = regime_state(conn, regime, event.published_at)
        market = config.market(spec.market)
        sample_unit = _change_unit(outcomes_by_pair[key], market.response)
        for horizon in config.horizons_days:
            results.append(study_pair(spec, horizon, events, outcomes_by_pair[key], states, config, sample_unit))
    return results


def _change_unit(outcomes: list[EventOutcome], response: str) -> str:
    if outcomes:
        return outcomes[0].change.unit
    return "pct" if response == "pct_change" else "bp"
