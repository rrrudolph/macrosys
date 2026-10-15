"""Point-in-time walk-forward of the weekly book (spec §10.2, §10.1 item 10, §11.3, §11.4).

Each weekly ``as_of`` rebuilds the resting book from closes on or before that day, takes the replay
fires (and optional hand-filed shocks) whose date is that day, and calls ``decide_week``. The
research sign bar is passed as met: it is a paper-promotion gate (spec §10.3), not an evaluation
input. The exposure-vector check is passed as ``passed`` for the same reason — the paper clock
leaves it pending so it does not approve a position, and that stub is not an input here. Theses
whose decision is ``trade`` go to the approver and then through ``size_position``; the target book
is ``PortfolioConstructor`` with the resting book as the baseline. Nothing in this module writes
the store, the forecast ledger, or the week log.

No coefficient is fitted on the scored window, so there is no training label to purge and no
embargo strip to drop (López de Prado 2018). The resting book and the inverse-vol and 12-month
momentum baselines are trailing statistics, and ``backtest`` holds the previous bar's weight, so
a statistic known at t does not earn the return at t. A model that was estimated on overlapping
forward returns would have to be split with ``purged_walk_forward_splits`` before it could be
scored here. Factor attribution (spec §10.2 item 4) is not computed: the §6.2 factor panel is
not an input this harness reads, and stand-in series are not invented.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import duckdb
import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from scipy.stats import kurtosis, skew

from macrosys.core.quantity import Quantity
from macrosys.decide.baseline import TRADING_DAYS, BaselineUnavailableError, resting_book
from macrosys.decide.mandate import Mandate
from macrosys.decide.portfolio import OrderTicket, PortfolioConstructor
from macrosys.decide.shock import Shock
from macrosys.decide.sizing import group_correlated, size_position
from macrosys.decide.thesis import Thesis, ThesisStatus
from macrosys.eval.baselines import (
    BacktestResult,
    WeightsFn,
    backtest,
    equal_weight,
    inverse_vol,
    sixty_forty,
    tsmom_12m,
)
from macrosys.eval.experiment_log import ExperimentLog
from macrosys.eval.promotion import ResearchGateResult
from macrosys.eval.scoring import deflated_sharpe_ratio
from macrosys.forward.week import WeekDecision, decide_week, load_open_shocks
from macrosys.mechanisms.library import Library, load_library
from macrosys.pipeline.jobs import PipelineConfig
from macrosys.risk.effective_n import effective_sample_size
from macrosys.skeleton.run import as_of_datetime
from macrosys.store.prices import PriceStore

# Paper promotion is not an evaluation input (spec §10.2 versus §10.3).
EVAL_RESEARCH = ResearchGateResult(evaluable=True, met=True, reasons=())
# [J] Same shared-factor cutoff the sizing tests use. The mandate does not set one.
_SHARED_FACTOR = 0.5
_ANCHORS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# Gap units treated as a return when the Kelly ceiling is applied. An unknown unit ceilings at 0.
_TO_SHARE = {"share": 1.0, "ratio": 1.0, "pct": 0.01, "pp": 0.01, "bp": 0.0001}
_RETURN_NOTE = (
    "ann_return is the compounded daily net return, annualized with 252 sessions, as a share of NAV. "
    "vol is the sample standard deviation of daily net returns times sqrt(252), as a share. "
    "sharpe is mean(daily net) / std(daily net) * sqrt(252) with no risk-free rate, a ratio. "
    "max_drawdown is the minimum of compounded-wealth drawdown, a share. "
    "turnover is one-way traded notional over the window divided by years (n/252), a share of NAV per year. "
    "effective_n is the autocorrelation-adjusted count of those daily net returns (spec §10.1 item 10), not a return."
)
_PURGE_NOTE = (
    "No coefficient is estimated inside this walk-forward, so there is no training window to purge "
    "and no embargo to apply (Lopez de Prado 2018; spec §10.2 item 1). Trailing volatility and "
    "12-month momentum use only bars already in the row, and the backtest holds the previous bar's "
    "weight. A fitted model on overlapping forward returns would need purged_walk_forward_splits first."
)
_FACTOR_NOTE = (
    "Factor attribution (spec §10.2 item 4) is not computed. This harness does not read a §6.2 factor "
    "panel, and it does not invent momentum, value, or carry series to regress on."
)
_LIMITS_NOTE = (
    "Position size and gross exposure are enforced inside PortfolioConstructor. The mandate's factor, "
    "country, and currency limits need exposure vectors this harness does not have, so those three "
    "checks are passed through and do not bind."
)
_DEFLATED_NOTE = (
    "deflated_sharpe on each book uses n_trials equal to the number of books with a finite Sharpe and "
    "the sample variance of those Sharpes (Bailey and Lopez de Prado 2014). The experiment log appends "
    "the same variants in order and grows its own trial count, so the system line, written last, is the "
    "one deflated for the full set. The n passed to the deflator is the number of daily net returns; "
    "effective_n is reported beside it."
)
_NAV = 1_000_000.0


class Approval(BaseModel):
    """One thesis the gates already passed, as the approver sees it (spec §11.3, §11.4).

    ``weight`` is the unsigned fraction of NAV to hold, and 0 means the thesis is not approved.
    Direction stays on ``thesis.expression.direction``. ``scenario_loss`` is the loss fraction of
    NAV at weight 1 (a positive number); a negative figure is read as a P&L and flipped.
    ``vol_annual`` is an annualized fraction of NAV. This is the shape ``macrosys.decide.approve``
    is expected to match; the walker duck-types any object with these attributes.
    """

    model_config = ConfigDict(frozen=True)

    thesis_id: str
    exposure_match: bool
    cost_estimate_bp: float
    vol_annual: float
    scenario_loss: float
    regime_flag: str
    post_trade_ok: bool
    weight: float
    reasons: list[str]


class Approver(Protocol):
    """Something that can accept or refuse a thesis the four gates already passed."""

    def approve(
        self,
        thesis: Thesis,
        conn: duckdb.DuckDBPyConnection,
        as_of: datetime,
        mandate: Mandate,
        current: Mapping[str, float],
    ) -> Approval:
        """Return the approval. ``current`` is the book held coming into the week, ticker to share of NAV."""


class RefuseApprover:
    """Default approver: nothing is approved, so a passing gate still holds the resting book.

    Wired until ``macrosys.decide.approve`` is the caller-supplied approver. ``weight`` is 0 and
    ``post_trade_ok`` is false. The other fields echo the thesis so a log can show what was refused.
    """

    def approve(
        self,
        thesis: Thesis,
        conn: duckdb.DuckDBPyConnection,
        as_of: datetime,
        mandate: Mandate,
        current: Mapping[str, float],
    ) -> Approval:
        """Refuse ``thesis``. The book is unchanged by this approval (spec §11.3)."""
        del conn, as_of, mandate, current
        loss = _worst_loss(thesis.sizing_inputs.scenario_pnl)
        return Approval(
            thesis_id=thesis.thesis_id,
            exposure_match=False,
            cost_estimate_bp=float(thesis.expression.cost_estimate_bp),
            vol_annual=float(thesis.sizing_inputs.vol_annual),
            scenario_loss=loss,
            regime_flag="stable",
            post_trade_ok=False,
            weight=0.0,
            reasons=("no approver is wired; the default approves nothing",),
        )


class BookScore(BaseModel):
    """Costed result for one book on the shared weekly calendar (spec §10.2).

    Returns, volatility, drawdown, and turnover are ``share`` quantities (0.05 is 5% of NAV).
    Sharpe ratios are ``ratio``. ``effective_n`` is a count. See the report's ``return_convention``.
    """

    model_config = ConfigDict(frozen=True)

    ann_return: Quantity
    vol: Quantity
    sharpe: Quantity | None
    max_drawdown: Quantity
    turnover: Quantity
    effective_n: float | None
    n_obs: int
    deflated_sharpe: Quantity | None = None


class WeekRecord(BaseModel):
    """One decision date (spec §10.2). Weights themselves are the companion panel, not this row."""

    model_config = ConfigDict(frozen=True)

    as_of: date
    n_shocks: int
    n_theses: int
    n_trades: int
    n_sized: int
    failing_gates: dict[str, int]
    blocked: bool = False
    note: str = ""


class WalkForwardReport(BaseModel):
    """Summary and per-week records written to ``walkforward.json`` (spec §10.2)."""

    model_config = ConfigDict(frozen=True)

    start: date
    end: date
    step_days: int
    cost_bp: float
    rebalance: str
    n_weeks: int
    n_blocked: int
    n_shocks: int
    n_theses: int
    n_trades: int
    n_sized: int
    failing_gates: dict[str, int]
    weeks: list[WeekRecord]
    books: dict[str, BookScore]
    return_convention: str = _RETURN_NOTE
    purge_embargo: str = _PURGE_NOTE
    factor_attribution: str = _FACTOR_NOTE
    limits: str = _LIMITS_NOTE
    research_gate: str = (
        "Passed ResearchGateResult(evaluable=True, met=True, reasons=()). The research sign bar "
        "is a paper-promotion gate (spec §10.3), not an evaluation input."
    )
    exposure_check: str = (
        "decide_week is called with exposure_check='passed'. The prospective week leaves the "
        "exposure vector check pending so the paper clock does not approve a position."
    )
    deflated_sharpe_note: str = _DEFLATED_NOTE
    experiment_ids: list[str] = Field(default_factory=list)
    experiment_log: str | None = None


class WalkForwardResult(BaseModel):
    """The report, the weekly weight panels, and the costed books they were scored on."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    report: WalkForwardReport
    weights: pd.DataFrame
    resting: pd.DataFrame
    backtests: dict[str, BacktestResult]


def run(
    conn: duckdb.DuckDBPyConnection,
    mandate: Mandate,
    pipeline_config: PipelineConfig,
    replay_path: str | Path,
    start: date,
    end: date,
    step_days: int = 7,
    approver: Approver | None = None,
    *,
    shocks_path: str | Path | None = None,
    cost_bp: float = 5.0,
    out_dir: str | Path | None = None,
    nav: float = _NAV,
) -> WalkForwardResult:
    """Walk ``start`` through ``end`` and score the book against the dumb baselines (spec §10.2).

    ``step_days`` must be 7 so the decision grid is the weekly calendar ``backtest`` resamples
    (``W-<weekday of start>``). On each date the resting book uses closes with ``trade_date`` at
    or before that date, and the replay contributes only fires whose ``as_of`` is that same date;
    a fire between grid points is not pulled forward or backward. Hand-filed shocks, when
    ``shocks_path`` is set, are included on the date of their ``information_cutoff`` only.
    ``approver`` defaults to ``RefuseApprover``, which approves nothing. The research sign bar
    passed into ``decide_week`` is ``EVAL_RESEARCH`` (evaluable, met, no reasons): that bar is a
    paper-promotion gate, not an evaluation input. ``exposure_check`` is ``passed`` for the same
    reason. ``nav`` is the notional the constructor rounds to cents; the default is large enough
    that a basis point of weight survives. When ``out_dir`` is set, writes ``walkforward.json``,
    ``weights.csv``, and appends one experiment-log line per scored book.
    """
    if step_days != 7:
        raise ValueError("step_days must be 7 so the book and the baselines share a weekly calendar")
    if start > end:
        raise ValueError("start cannot be after end")
    if cost_bp < 0:
        raise ValueError("cost_bp cannot be negative")
    if nav <= 0:
        raise ValueError("nav must be positive")
    chosen = approver if approver is not None else RefuseApprover()
    weeks = _week_dates(start, end, step_days)
    replay = _load_replay(Path(replay_path))
    filed = _by_cutoff(load_open_shocks(shocks_path) if shocks_path is not None else [])
    library = load_library(_library_dir())
    panels = _decide_panels(
        conn,
        mandate,
        pipeline_config,
        weeks,
        replay,
        filed,
        chosen,
        library,
        nav,
    )
    rebalance = f"W-{_ANCHORS[start.weekday()]}"
    returns = _return_panel(conn, pipeline_config, panels.tickers, start, end)
    backtests = _score_books(
        panels.weights,
        panels.resting,
        returns,
        rebalance,
        cost_bp,
        mandate,
        pd.Timestamp(start),
    )
    scores = _book_scores(backtests)
    experiment_ids: list[str] = []
    log_path: str | None = None
    if out_dir is not None:
        destination = Path(out_dir)
        destination.mkdir(parents=True, exist_ok=True)
        log_file = destination / "experiments.jsonl"
        experiment_ids = _append_experiments(
            log_file,
            scores,
            backtests,
            start,
            end,
            cost_bp,
            pipeline_config,
            system_tag=type(chosen).__name__,
        )
        log_path = str(log_file)
    report = WalkForwardReport(
        start=start,
        end=end,
        step_days=step_days,
        cost_bp=cost_bp,
        rebalance=rebalance,
        n_weeks=len(panels.weeks),
        n_blocked=sum(1 for row in panels.weeks if row.blocked),
        n_shocks=sum(row.n_shocks for row in panels.weeks),
        n_theses=sum(row.n_theses for row in panels.weeks),
        n_trades=sum(row.n_trades for row in panels.weeks),
        n_sized=sum(row.n_sized for row in panels.weeks),
        failing_gates=_sum_gates(row.failing_gates for row in panels.weeks),
        weeks=panels.weeks,
        books=scores,
        experiment_ids=experiment_ids,
        experiment_log=log_path,
    )
    result = WalkForwardResult(
        report=report,
        weights=panels.weights,
        resting=panels.resting,
        backtests=backtests,
    )
    if out_dir is not None:
        _write(Path(out_dir), result)
    return result


class _Panels(BaseModel):
    """Weekly books and the tickers they name. Frames are attached after validation."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    weeks: list[WeekRecord]
    weights: pd.DataFrame
    resting: pd.DataFrame
    tickers: list[str]


def _decide_panels(
    conn: duckdb.DuckDBPyConnection,
    mandate: Mandate,
    config: PipelineConfig,
    weeks: Sequence[date],
    replay: Mapping[date, list[Shock]],
    filed: Mapping[date, list[Shock]],
    approver: Approver,
    library: Library,
    nav: float,
) -> _Panels:
    system_rows: list[dict[str, float]] = []
    resting_rows: list[dict[str, float]] = []
    records: list[WeekRecord] = []
    index: list[pd.Timestamp] = []
    previous: dict[str, float] = {}
    for day in weeks:
        as_of = as_of_datetime(day)
        index.append(pd.Timestamp(day))
        try:
            book = resting_book(conn, as_of, mandate, config.price_instrument_id)
        except BaselineUnavailableError as exc:
            records.append(
                WeekRecord(
                    as_of=day,
                    n_shocks=0,
                    n_theses=0,
                    n_trades=0,
                    n_sized=0,
                    failing_gates={},
                    blocked=True,
                    note=str(exc),
                )
            )
            system_rows.append(dict(previous))
            resting_rows.append(dict(previous))
            continue
        shocks, triggered = _merge_day(filed.get(day, []), replay.get(day, []))
        decision = decide_week(
            conn,
            as_of,
            shocks,
            mandate=mandate,
            research=EVAL_RESEARCH,
            book=book,
            triggered_ids=triggered,
            exposure_check="passed",
            library=library,
        )
        sized = _size_trades(decision, approver, conn, as_of, mandate, previous)
        baseline = None if book is None else dict(book.weights)
        target = _target_book(day, sized, previous, mandate, nav, baseline)
        rest = dict(baseline) if baseline is not None else {}
        records.append(
            WeekRecord(
                as_of=day,
                n_shocks=decision.n_shocks,
                n_theses=len(decision.theses),
                n_trades=sum(1 for note in decision.notes if note.action == "trade"),
                n_sized=len(sized),
                failing_gates=_gate_hist(decision),
            )
        )
        system_rows.append(target)
        resting_rows.append(rest)
        previous = target
    tickers = _ticker_list(mandate, config, system_rows, resting_rows)
    weights = _frame(system_rows, index, tickers)
    resting = _frame(resting_rows, index, tickers)
    return _Panels(weeks=records, weights=weights, resting=resting, tickers=tickers)


def _size_trades(
    decision: WeekDecision,
    approver: Approver,
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    mandate: Mandate,
    current: Mapping[str, float],
) -> dict[str, tuple[Thesis, float]]:
    """Approver weight, capped by ``size_position``, for theses whose decision is trade.

    A thesis the approver refuses (weight 0, or ``post_trade_ok`` false) is not sized. Correlated
    theses share one sizing budget (spec §11.4): the sum of their weights is held at the tightest
    single-thesis cap in the group. The weight handed to the constructor is the minimum of the
    approver's unsigned weight and that cap.
    """
    approved: list[Thesis] = []
    asks: dict[str, float] = {}
    caps: dict[str, float] = {}
    by_id = {thesis.thesis_id: thesis for thesis in decision.theses}
    for note in decision.notes:
        if note.action != "trade" or note.thesis_id is None:
            continue
        thesis = by_id.get(note.thesis_id)
        if thesis is None:
            continue
        approval = approver.approve(thesis, conn, as_of, mandate, current)
        ask = float(approval.weight)
        if ask < 0:
            raise ValueError("approver weight is an unsigned fraction of NAV")
        if ask == 0.0 or not bool(approval.post_trade_ok):
            continue
        try:
            live = thesis.transition(ThesisStatus.approved)
        except ValueError:
            continue
        cap = _size_cap(live, approval, mandate)
        approved.append(live)
        asks[live.thesis_id] = ask
        caps[live.thesis_id] = cap
    if not approved:
        return {}
    weights: dict[str, tuple[Thesis, float]] = {}
    for group in group_correlated(approved, _SHARED_FACTOR):
        raw = {thesis.thesis_id: min(asks[thesis.thesis_id], caps[thesis.thesis_id]) for thesis in group}
        if len(group) > 1:
            budget = min(caps[thesis.thesis_id] for thesis in group)
            total = sum(raw.values())
            if total > budget > 0.0:
                scale = budget / total
                raw = {key: value * scale for key, value in raw.items()}
            elif budget <= 0.0:
                raw = {key: 0.0 for key in raw}
        for thesis in group:
            weight = raw[thesis.thesis_id]
            if weight > 0.0:
                weights[thesis.thesis_id] = (thesis, weight)
    return weights


def _size_cap(thesis: Thesis, approval: Approval, mandate: Mandate) -> float:
    """The spec §11.4 weight, using the approver's vol and scenario loss when they are positive."""
    edges = _gap_edges(thesis.gap)
    if edges is None:
        return 0.0
    vol = float(approval.vol_annual)
    if vol <= 0.0:
        vol = float(thesis.sizing_inputs.vol_annual)
    if vol <= 0.0:
        return 0.0
    target = mandate.risk.vol_target_annual_pct
    # [J] A mandate with no volatility target sizes parity at the asset's own vol, so the
    # unconstrained weight is the conviction multiplier rather than an invented target.
    target_vol = vol if target is None else target / 100.0
    loss = float(approval.scenario_loss)
    if loss < 0.0:
        loss = abs(loss)
    if loss == 0.0:
        loss = _worst_loss(thesis.sizing_inputs.scenario_pnl)
    budget_pct = mandate.risk.scenario_loss_budget_pct
    remaining = math.inf if budget_pct is None else budget_pct / 100.0
    position_pct = mandate.risk.position_limit_pct
    position_cap = math.inf if position_pct is None else position_pct / 100.0
    factor_cap = _factor_cap(mandate, thesis)
    sized = size_position(
        vol_annual=vol,
        target_vol=target_vol,
        gap_low=edges[0],
        gap_high=edges[1],
        evidence_quality=thesis.evidence_quality,
        marginal_scenario_loss=loss,
        remaining_budget=remaining,
        position_cap=position_cap,
        factor_cap=factor_cap,
        gap_lower_edge_annual=_annual_edge(thesis.gap),
        kelly_fraction=mandate.sizing.fractional_kelly_ceiling,
        conviction_bounds=mandate.sizing.conviction_multiplier_bounds,
    )
    return float(sized.weight)


def _target_book(
    day: date,
    sized: Mapping[str, tuple[Thesis, float]],
    current: Mapping[str, float],
    mandate: Mandate,
    nav: float,
    baseline: Mapping[str, float] | None,
) -> dict[str, float]:
    """Tickets from ``PortfolioConstructor``, applied to ``current`` at ``nav`` (spec §11.3, §11.7)."""
    theses = [item[0] for item in sized.values()]
    sizes = {thesis_id: weight for thesis_id, (_thesis, weight) in sized.items()}
    constructor = PortfolioConstructor(
        f"wf-{day.isoformat()}",
        min_notional=0.0,
        factor_ok=_exposure_unwired,
        country_ok=_exposure_unwired,
        currency_ok=_exposure_unwired,
    )
    tickets = constructor.propose(
        theses,
        sizes,
        current,
        mandate,
        nav,
        baseline=None if baseline is None else dict(baseline),
    )
    return _apply_tickets(current, tickets, baseline, nav)


def _apply_tickets(
    current: Mapping[str, float],
    tickets: Sequence[OrderTicket],
    baseline: Mapping[str, float] | None,
    nav: float,
) -> dict[str, float]:
    out = {name: float(weight) for name, weight in current.items()}
    for ticket in tickets:
        sign = 1.0 if ticket.side == "buy" else -1.0
        out[ticket.instrument_id] = out.get(ticket.instrument_id, 0.0) + sign * ticket.quantity / nav
    if baseline is not None:
        for name in baseline:
            out.setdefault(name, 0.0)
    return {name: weight for name, weight in out.items() if weight != 0.0 or (baseline is not None and name in baseline)}


def _exposure_unwired(weights: Mapping[str, float]) -> bool:
    """Factor, country, and currency limits have no exposure vector in this harness (spec §11.3)."""
    del weights
    return True


def _return_panel(
    conn: duckdb.DuckDBPyConnection,
    config: PipelineConfig,
    tickers: Sequence[str],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Simple returns from the union of record-tier closes, including a warmup before ``start``."""
    if not tickers:
        raise ValueError("the walk-forward has no instruments to price")
    ids = [config.price_instrument_id(ticker) for ticker in tickers]
    # Two years of daily bars so 12-month momentum is warm at ``start`` when the store has them.
    lookback = start - timedelta(days=520)
    closes = PriceStore(conn).read_closes(list(ids), lookback, end)
    closes = closes.rename(columns=dict(zip(ids, tickers, strict=True)))
    closes = closes.dropna(axis=1, how="all").sort_index()
    if closes.empty or len(closes.index) < 3:
        raise ValueError(f"no usable closes between {lookback.isoformat()} and {end.isoformat()}")
    returns = closes.pct_change().iloc[1:]
    missing = [ticker for ticker in tickers if ticker not in returns.columns]
    if missing:
        raise ValueError(f"no closes for instruments the book held: {missing}")
    return returns


def _score_books(
    weights: pd.DataFrame,
    resting: pd.DataFrame,
    returns: pd.DataFrame,
    rebalance: str,
    cost_bp: float,
    mandate: Mandate,
    start: pd.Timestamp,
) -> dict[str, BacktestResult]:
    window = 63 if mandate.baseline is None else mandate.baseline.window_days
    books: list[tuple[str, WeightsFn]] = [
        ("system", _as_of_weights(weights)),
        ("resting", _as_of_weights(resting)),
    ]
    if "SPY" in returns.columns and "IEF" in returns.columns:
        books.append(("sixty_forty", lambda panel: sixty_forty(panel, "SPY", "IEF")))
    books.extend(
        [
            ("equal_weight", equal_weight),
            ("inverse_vol", lambda panel, n=window: inverse_vol(panel, n)),
            ("tsmom", tsmom_12m),
        ]
    )
    scored: dict[str, BacktestResult] = {}
    for name, fn in books:
        raw = backtest(fn, returns, rebalance=rebalance, cost_bp=cost_bp)
        scored[name] = _slice_result(raw, start)
    return scored


def _as_of_weights(weekly: pd.DataFrame) -> WeightsFn:
    """Hold the decision-date book from that date forward. Later rows of ``returns`` are ignored."""

    def weights_fn(returns: pd.DataFrame) -> pd.DataFrame:
        stamped = weekly.copy()
        stamped.index = pd.DatetimeIndex(pd.to_datetime(stamped.index))
        combined = returns.index.union(stamped.index)
        expanded = stamped.reindex(combined).sort_index().ffill()
        return expanded.reindex(index=returns.index, columns=returns.columns).fillna(0.0)

    return weights_fn


def _slice_result(result: BacktestResult, start: pd.Timestamp) -> BacktestResult:
    net = result.net_returns.loc[result.net_returns.index >= start]
    if net.empty:
        raise ValueError(f"no backtest returns on or after {start.date().isoformat()}")
    turnover = result.turnover.reindex(net.index).fillna(0.0)
    wealth = (1.0 + net).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    net.name = "net_returns"
    turnover.name = "turnover"
    drawdown.name = "drawdown"
    return BacktestResult(net_returns=net, turnover=turnover, drawdown=drawdown)


def _book_scores(books: Mapping[str, BacktestResult]) -> dict[str, BookScore]:
    scored = {name: _one_score(result) for name, result in books.items()}
    sharpes = [score.sharpe.value for score in scored.values() if score.sharpe is not None and score.sharpe.value is not None]
    n_trials = max(1, len(sharpes))
    var_sr = float(np.var(sharpes, ddof=1)) if len(sharpes) >= 2 else 0.0
    filled: dict[str, BookScore] = {}
    for name, score in scored.items():
        deflated = _deflate(score, books[name], n_trials, var_sr)
        filled[name] = score.model_copy(update={"deflated_sharpe": deflated})
    return filled


def _one_score(result: BacktestResult) -> BookScore:
    values = result.net_returns.to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    n = int(values.shape[0])
    if n < 2:
        raise ValueError("a scored book needs at least two net returns")
    mean = float(values.mean())
    sigma = float(values.std(ddof=1))
    vol = sigma * math.sqrt(TRADING_DAYS)
    sharpe = None if sigma <= 0.0 or not math.isfinite(sigma) else mean / sigma * math.sqrt(TRADING_DAYS)
    wealth = float(np.prod(1.0 + values))
    years = n / float(TRADING_DAYS)
    if years > 0.0 and wealth > 0.0:
        ann = wealth ** (1.0 / years) - 1.0
    elif wealth == 0.0:
        ann = -1.0
    else:
        ann = mean * TRADING_DAYS
    turnover = float(result.turnover.sum()) / years if years > 0.0 else 0.0
    drawdown = float(result.drawdown.min())
    try:
        n_eff = effective_sample_size(values) if n >= 3 else None
    except ValueError:
        n_eff = None
    provenance = "macrosys.eval.walkforward; spec §10.2"
    return BookScore(
        ann_return=_qty(ann, "share", provenance),
        vol=_qty(vol, "share", provenance),
        sharpe=None if sharpe is None else _qty(sharpe, "ratio", provenance),
        max_drawdown=_qty(drawdown, "share", provenance),
        turnover=_qty(turnover, "share", provenance),
        effective_n=n_eff,
        n_obs=n,
    )


def _deflate(score: BookScore, result: BacktestResult, n_trials: int, var_sr: float) -> Quantity | None:
    if score.sharpe is None or score.sharpe.value is None:
        return None
    values = result.net_returns.to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 4:
        return None
    sk = float(skew(values, bias=False))
    ku = float(kurtosis(values, fisher=False, bias=False))
    if not (math.isfinite(sk) and math.isfinite(ku) and math.isfinite(var_sr)):
        return None
    try:
        value = deflated_sharpe_ratio(
            score.sharpe.value,
            n_trials=n_trials,
            var_sr_trials=var_sr,
            n=int(len(values)),
            skew=sk,
            kurt=ku,
        )
    except ValueError:
        return None
    return _qty(value, "ratio", "Bailey & Lopez de Prado 2014; macrosys.eval.scoring.deflated_sharpe_ratio")


def _append_experiments(
    path: Path,
    scores: Mapping[str, BookScore],
    books: Mapping[str, BacktestResult],
    start: date,
    end: date,
    cost_bp: float,
    config: PipelineConfig,
    system_tag: str = "",
) -> list[str]:
    """Append each scored book. The system variant is last so its trial count is the full set.

    The experiment id names the book, the window, the cost, and for the system book the approver
    class (``system_tag``), so a different approver is a new trial. A rerun of an identical
    configuration is the same experiment: the log already has it, and that id is returned without
    a second line (spec §10.2 item 5 counts trials, not repetitions).
    """
    order = [name for name in ("resting", "sixty_forty", "equal_weight", "inverse_vol", "tsmom", "system") if name in scores]
    sharpes = [
        scores[name].sharpe.value
        for name in order
        if scores[name].sharpe is not None and scores[name].sharpe.value is not None
    ]
    var_sr = float(np.var(sharpes, ddof=1)) if len(sharpes) >= 2 else 0.0
    log = ExperimentLog(path)
    written: list[str] = []
    for name in order:
        score = scores[name]
        if score.sharpe is None or score.sharpe.value is None:
            continue
        values = books[name].net_returns.to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if len(values) < 4:
            continue
        sk = float(skew(values, bias=False))
        ku = float(kurtosis(values, fisher=False, bias=False))
        if not (math.isfinite(sk) and math.isfinite(ku)):
            continue
        try:
            # Same inputs the log will pass, apart from the trial count, which does not enter the
            # variance term. A series the deflator refuses is left off the log.
            deflated_sharpe_ratio(score.sharpe.value, n_trials=1, var_sr_trials=0.0, n=int(len(values)), skew=sk, kurt=ku)
        except ValueError:
            continue
        suffix = f"-{system_tag}" if name == "system" and system_tag else ""
        experiment_id = f"wf-{name}-{start.isoformat()}-{end.isoformat()}-c{cost_bp:g}{suffix}"
        try:
            record = log.append(
                experiment_id=experiment_id,
                hypothesis="walk-forward investment value",
                variant=name,
                sharpe=score.sharpe,
                code_commit=config.code_commit,
                parameter_set=config.parameter_set,
                n=int(len(values)),
                skew=sk,
                kurt=ku,
                var_sr_trials=var_sr,
            )
        except ValueError as exc:
            if "already in the log" not in str(exc):
                raise
            written.append(experiment_id)
            continue
        written.append(record.experiment_id)
    return written


def _write(out_dir: Path, result: WalkForwardResult) -> None:
    payload = result.report.model_dump(mode="json")
    (out_dir / "walkforward.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    frame = result.weights.copy()
    frame.index.name = "as_of"
    frame.to_csv(out_dir / "weights.csv", float_format="%.8f")


def _week_dates(start: date, end: date, step_days: int) -> list[date]:
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=step_days)
    if not days:
        raise ValueError("no decision dates in the window")
    return days


def _load_replay(path: Path) -> dict[date, list[Shock]]:
    """Group replay fires by the calendar date in ``as_of``. One JSON object per line."""
    if not path.is_file():
        raise ValueError(f"replay file does not exist: {path}")
    grouped: dict[date, list[Shock]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            shock = Shock.model_validate(obj["shock"])
            day = date.fromisoformat(str(obj["as_of"])[:10])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path} line {line_no} is not a replay fire: {exc}") from exc
        grouped.setdefault(day, []).append(shock)
    return grouped


def _by_cutoff(shocks: Sequence[Shock]) -> dict[date, list[Shock]]:
    grouped: dict[date, list[Shock]] = {}
    for shock in shocks:
        grouped.setdefault(shock.information_cutoff.date(), []).append(shock)
    return grouped


def _merge_day(filed: Sequence[Shock], replayed: Sequence[Shock]) -> tuple[list[Shock], set[str]]:
    seen: set[str] = set()
    merged: list[Shock] = []
    for shock in [*filed, *replayed]:
        if shock.id in seen:
            raise ValueError(f"shock id {shock.id} appears twice (filed and triggered, or filed twice)")
        seen.add(shock.id)
        merged.append(shock)
    return merged, {shock.id for shock in replayed}


def _ticker_list(
    mandate: Mandate,
    config: PipelineConfig,
    system_rows: Sequence[Mapping[str, float]],
    resting_rows: Sequence[Mapping[str, float]],
) -> list[str]:
    tickers: list[str] = []
    if mandate.baseline is not None:
        tickers.extend(mandate.baseline.instruments)
    for extra in ("SPY", "IEF"):
        if extra in tickers:
            continue
        try:
            config.price_instrument_id(extra)
        except KeyError:
            continue
        tickers.append(extra)
    for row in [*system_rows, *resting_rows]:
        for name in row:
            if name not in tickers:
                tickers.append(name)
    return tickers


def _frame(rows: Sequence[Mapping[str, float]], index: Sequence[pd.Timestamp], tickers: Sequence[str]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows), index=pd.DatetimeIndex(index))
    frame = frame.reindex(columns=list(tickers)).fillna(0.0)
    frame.index.name = "as_of"
    return frame


def _gate_hist(decision: WeekDecision) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for note in decision.notes:
        if note.failing_gate is not None:
            counts[note.failing_gate] += 1
    return dict(sorted(counts.items()))


def _sum_gates(rows: Sequence[Mapping[str, int]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts.update(row)
    return dict(sorted(counts.items()))


def _gap_edges(gap: Quantity) -> tuple[float, float] | None:
    low = gap.low if gap.low is not None else gap.value
    high = gap.high if gap.high is not None else gap.value
    if low is None or high is None:
        return None
    return float(low), float(high)


def _annual_edge(gap: Quantity) -> float:
    """Conservative gap edge as a fraction of NAV, for the Kelly ceiling only (spec §11.4).

    ``bp`` is divided by 10,000 and ``pct`` / ``pp`` by 100. A range that straddles zero, or a
    unit this table does not name, contributes no edge, so the ceiling is 0 rather than a guess.
    """
    edges = _gap_edges(gap)
    if edges is None:
        return 0.0
    low, high = edges
    if low <= 0.0 <= high:
        return 0.0
    edge = low if low > 0.0 else abs(high)
    scale = _TO_SHARE.get(gap.unit)
    if scale is None:
        return 0.0
    return edge * scale


def _factor_cap(mandate: Mandate, thesis: Thesis) -> float:
    limit = mandate.risk.factor_limit_pct
    if limit is None:
        return math.inf
    peak = max((abs(value) for value in thesis.sizing_inputs.factor_loadings.values()), default=0.0)
    if peak == 0.0:
        return math.inf
    return (limit / 100.0) / peak


def _worst_loss(scenario_pnl: Mapping[str, float]) -> float:
    if not scenario_pnl:
        return 0.0
    worst = min(scenario_pnl.values())
    return abs(worst) if worst < 0.0 else 0.0


def _qty(value: float, unit: str, provenance: str) -> Quantity:
    return Quantity(value=float(value), unit=unit, provenance=provenance)


def _library_dir() -> Path:
    """The bundled ``mechanisms`` directory, else ``./mechanisms`` (same rule as the weekly job)."""
    bundled = Path(__file__).resolve().parents[2] / "mechanisms"
    if bundled.is_dir():
        return bundled
    return Path("mechanisms")
