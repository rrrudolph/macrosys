"""Prospective weekly decision (spec §11.2, §10.2 item 7, §10.3, §11.3).

``decide_week`` is the decision itself: shocks in, theses and gate results out, nothing written.
``forward_week`` refreshes world state from ``snapshot(as_of)``, calls ``decide_week``, and only
then appends theses to the frozen ledger and writes the report and the week log. The walk-forward
evaluator calls ``decide_week`` and does not freeze. The default action is no-trade. Coverage that
blocks is class B: the decision is skipped and the book is kept. The research sign bar is applied
from the mandate; this function does not place an order.

Amended 2026-10-15 (owner decisions): the week states the mandate's resting book, the baseline held
when no thesis passes, and files the shocks that ``config/triggers.yaml`` raises from stored data
next to the owner's hand-filed ones. A shock whose type has no library route fails the mechanism
gate and is recorded as such; it is not an error.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.quantity import Quantity
from macrosys.core.time import Horizon
from macrosys.decide.approve import Approval, Approver
from macrosys.decide.baseline import BaselineUnavailableError, RestingBook, resting_book
from macrosys.decide.gates import Decision, decide
from macrosys.decide.mandate import Mandate
from macrosys.decide.shock import Shock
from macrosys.decide.thesis import (
    CompetingEffect,
    EvidenceQuality,
    ExpectedEffect,
    ExpectedReturn,
    Expression,
    ForecastPrecision,
    Invalidation,
    InvalidationRule,
    MissingEvidence,
    PricedIn,
    Scenario,
    Thesis,
    Versions,
)
from macrosys.eval.ledger import ForecastLedger
from macrosys.eval.promotion import ResearchCounts, ResearchGateResult, research_to_paper
from macrosys.mechanisms import (
    AcceptedCompeting,
    MechanismInstance,
    MissingParameterError,
    PropagationPath,
    Propagator,
    UnitContinuityError,
    validate_path,
)
from macrosys.mechanisms import Shock as PropagationShock
from macrosys.mechanisms.library import Library, load_library
from macrosys.pipeline.jobs import PipelineConfig, load_config
from macrosys.pricing.errors import NoPricedInError
from macrosys.pricing.implied import StoreImpliedPaths
from macrosys.pricing.priced_in import PricedIn as MarketPricedIn
from macrosys.pricing.priced_in import priced_in
from macrosys.skeleton.preflight import ensure_runbook
from macrosys.skeleton.routing import Route, route_for
from macrosys.skeleton.run import as_of_datetime
from macrosys.skeleton.stubs import STUB_PROVENANCE
from macrosys.store.coverage import CoverageReport, coverage_report
from macrosys.store.manifest import build_manifest, write_manifest
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from macrosys.store.state import WorldStateStore
from macrosys.triggers.rules import load_triggers
from macrosys.triggers.scan import Fired, StoreReader, scan
from macrosys.world.refresh import refresh_world_state

if TYPE_CHECKING:
    from macrosys.cases.runner import SuiteReport

# Dry-run pin. Not a git sha: the replay tuple is the store snapshot, the library hash, and this
# ticket (spec §4.4, §10.3, X5.2).
CODE_COMMIT = "X5.2"
PROVENANCE = "forward_week"
# Spec §10.3: paper entry wants eight consecutive finished weeks. Ticket X5.2 starts that clock.
DRY_RUN_WEEKS = 8
# Same held-out default as ``macrosys.cases.cli_commands``.
_HELD_OUT = frozenset({"supply"})
_REPO = Path(__file__).resolve().parents[2]
WeekStatus = Literal["ok", "blocked", "error"]


class DuplicateWeekError(RuntimeError):
    """A forward week for this calendar date is already in the week log (spec §11.2)."""


class ShockNote(BaseModel):
    """One shock's recorded decision, or the error that kept it off the ledger."""

    model_config = ConfigDict(frozen=True)

    shock_id: str
    action: Literal["trade", "no_trade"] | None = None
    failing_gate: str | None = None
    reason: str
    thesis_id: str | None = None
    error: bool = False
    triggered: bool = False
    # From the approver (spec §11.3 expression and risk gates, §11.4 size). ``weight`` is the
    # unsigned fraction of NAV the approver would hold; ``None`` when no approver ran.
    weight: float | None = None
    approval_reasons: list[str] = Field(default_factory=list)


class WeekResult(BaseModel):
    """What one forward week wrote (spec §11.2, §10.2 item 7).

    ``baseline`` is the resting book's weights at ``as_of`` (``None`` for a cash mandate or a
    blocked week). ``triggered`` lists the data-filed shock ids this week raised.
    """

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    status: WeekStatus
    n_shocks: int
    n_theses: int
    paper_allowed: bool
    research_evaluable: bool
    reasons: list[str]
    manifest_digest: str | None = None
    decisions: list[ShockNote] = Field(default_factory=list)
    baseline: dict[str, float] | None = None
    triggered: list[str] = Field(default_factory=list)
    report_path: Path
    ledger_path: Path


def forward_week(
    db_path: str | Path,
    as_of: date | datetime,
    *,
    pipeline_config: str | Path,
    shocks_path: str | Path,
    mandate_path: str | Path,
    ledger_path: str | Path,
    week_log_path: str | Path,
    out_dir: str | Path,
    runbook_path: str | Path = "docs/runbook.md",
    acknowledge_runbook_change: bool = False,
    research: ResearchGateResult | None = None,
    triggers_path: str | Path | None = None,
    approver: Approver | None = None,
) -> WeekResult:
    """Refresh, gate, and freeze one prospective week (spec §11.2, §10.2 item 7, §10.3, §11.3).

    ``approver`` (``macrosys.decide.approve.StoreApprover`` on the CLI) supplies the exposure
    check, cost, scenario loss, regime flag, and size from the store; without it the expression
    check stays pending and no position can be approved. The book passed to the approver is
    empty: this clock has no order manager wired, so every thesis is sized against a flat book.

    ``as_of`` is a calendar date, taken as the end of that UTC day, or an aware datetime. The
    runbook hash is checked before any thesis is frozen (spec §11.7). Required series are the
    pipeline config's ``required`` list plus each series group's ids at that group's
    ``max_lag_days``. A blocking coverage report is class B: the week is recorded, nothing is
    propagated, and no thesis is appended. A mandate baseline that cannot be computed from the
    closes visible at ``as_of`` blocks the same way. ``triggers_path`` names the data-filed shock
    rules; the shocks they raise at ``as_of`` are decided next to the filed ones. With no shock the
    action is no-trade and the book is the baseline. Each shock runs on the library path with stub
    coefficients dropped; a type with no route fails the mechanism gate. A missing parameter or a
    unit break is blocking missing evidence and still freezes a no-trade thesis. The expression check
    stays pending, so this clock records the forecast and does not approve a position. When the
    research sign bar is not met, the action is no-trade even if every gate passed. No order is
    written. A second call for the same UTC calendar date raises ``DuplicateWeekError`` and does
    not append another thesis.
    """
    as_of_dt = as_of_datetime(as_of)
    day = as_of_dt.date()
    log_path = Path(week_log_path)
    _assert_new_week(log_path, day)

    mandate = Mandate.load(_readable(mandate_path))
    research_result = research if research is not None else _research_from_cases(mandate, db_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ensure_runbook(
        _readable(runbook_path),
        out / "runbook.sha256",
        acknowledge=acknowledge_runbook_change,
    )

    config = load_config(_readable(pipeline_config))
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db))
    try:
        create_schema(conn)
        required = _required_pairs(config)
        coverage = coverage_report(conn, as_of_dt, required)
        book: RestingBook | None = None
        book_error: BaselineUnavailableError | None = None
        if not coverage.blocking:
            try:
                book = resting_book(conn, as_of_dt, mandate, config.price_instrument_id)
            except BaselineUnavailableError as exc:
                book_error = exc
        if coverage.blocking or book_error is not None:
            digest = _write_store_manifest(conn, config, out)
            prepared = _blocked(coverage, research_result, digest, book_error)
        else:
            refresh_world_state(conn, as_of_dt)
            prepared = _decide_shocks(
                conn,
                as_of_dt,
                config,
                shocks_path,
                mandate,
                research_result,
                out,
                book,
                triggers_path,
                approver,
            )
    finally:
        conn.close()

    ledger_file = Path(ledger_path)
    n_theses = _freeze_all(prepared.theses, ledger_file, as_of_dt)
    reasons = list(prepared.reasons)
    result = WeekResult(
        as_of=as_of_dt,
        status=prepared.status,
        n_shocks=prepared.n_shocks,
        n_theses=n_theses,
        paper_allowed=research_result.met,
        research_evaluable=research_result.evaluable,
        reasons=reasons,
        manifest_digest=prepared.manifest_digest,
        decisions=prepared.notes,
        baseline=None if prepared.baseline is None else dict(prepared.baseline.weights),
        triggered=[item.shock.id for item in prepared.fired],
        report_path=out / "report.md",
        ledger_path=ledger_file,
    )
    # The report is rewritten on a retry. The week log is the commit: a crash after the
    # ledger write and before this append can be run again without a second thesis.
    _write_report(result, coverage, research_result, prepared)
    _append_week(log_path, result, day)
    return result


def load_open_shocks(path: str | Path) -> list[Shock]:
    """Load filed shocks from YAML (spec §11.2). An empty ``shocks`` list is valid and means no-trade."""
    file_path = _readable(path)
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if loaded is None:
        return []
    if not isinstance(loaded, dict) or not isinstance(loaded.get("shocks", []), list):
        raise ValueError(f"{file_path} must be a mapping with a shocks list")
    return [Shock.model_validate(item) for item in loaded.get("shocks", [])]


def read_week_log(path: str | Path) -> list[dict[str, object]]:
    """Read the append-only week log. A missing file is an empty clock (spec §11.2)."""
    file_path = Path(path)
    if not file_path.exists():
        return []
    records: list[dict[str, object]] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError(f"{file_path} has a week record that is not an object")
        records.append(parsed)
    return records


def trailing_finished_weeks(records: list[dict[str, object]]) -> int:
    """Count the suffix of weeks whose status is ``ok`` or ``blocked`` (spec §10.3, §11.7).

    A blocked week is a finished class-B week: the decision was skipped and the book was kept.
    An ``error`` status breaks the run. The count is the trailing suffix, not a hand edit.
    """
    count = 0
    for record in reversed(records):
        if record.get("status") not in ("ok", "blocked"):
            break
        count += 1
    return count


def week_status_text(path: str | Path) -> str:
    """Describe the dry-run clock (spec §10.3, ticket X5.2).

    Prints how many week records exist and how many trailing records are ``ok`` or ``blocked``.
    The 8-week gate is reported met only when that trailing count is at least 8. Paper allowed
    is taken from the latest record.
    """
    records = read_week_log(path)
    n = len(records)
    trailing = trailing_finished_weeks(records)
    noun = "record" if n == 1 else "records"
    if trailing >= DRY_RUN_WEEKS:
        gate = "The 8-week gate is met."
    else:
        gate = "The 8-week gate is not met."
    if not records:
        paper = "Paper allowed is not known; no week is recorded."
    else:
        allowed = "yes" if records[-1].get("paper_allowed") is True else "no"
        paper = f"Paper allowed: {allowed}."
    return (
        f"{n} {noun}.\n"
        f"{trailing} trailing records have status ok or blocked.\n"
        f"{gate}\n"
        f"{paper}\n"
    )


class _Prepared(BaseModel):
    """In-memory week, before the ledger and the week log are appended."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    status: WeekStatus
    reasons: list[str]
    n_shocks: int
    notes: list[ShockNote]
    theses: list[Thesis]
    manifest_digest: str | None
    baseline: RestingBook | None = None
    baseline_error: str | None = None
    fired: list[Fired] = Field(default_factory=list)


def _blocked(
    coverage: CoverageReport,
    research: ResearchGateResult,
    digest: str,
    book_error: BaselineUnavailableError | None = None,
) -> _Prepared:
    reasons: list[str] = []
    if coverage.blocking:
        bad = [f"{row.series_id} is {row.status}" for row in coverage.rows if row.status != "ok"]
        detail = "; ".join(bad) if bad else "a required series is not fresh"
        reasons.append(f"coverage blocked the week ({detail}); the decision is skipped and the book is kept")
    if book_error is not None:
        reasons.append(f"the resting book cannot be stated ({book_error}); the decision is skipped and the book is kept")
    reasons.extend(_research_reasons(research))
    return _Prepared(
        status="blocked",
        reasons=reasons,
        n_shocks=0,
        notes=[],
        theses=[],
        manifest_digest=digest,
        baseline_error=None if book_error is None else str(book_error),
    )


class WeekDecision(BaseModel):
    """Theses and gate results for one week, before any ledger or report write (spec §11.2, §11.3)."""

    model_config = ConfigDict(frozen=True)

    status: WeekStatus
    reasons: list[str]
    n_shocks: int
    notes: list[ShockNote]
    theses: list[Thesis]


def decide_week(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    shocks: Sequence[Shock],
    *,
    mandate: Mandate,
    research: ResearchGateResult,
    book: RestingBook | None,
    triggered_ids: Collection[str] | None = None,
    exposure_check: Literal["pending", "passed"] = "pending",
    library: Library | None = None,
    approver: Approver | None = None,
    current: Mapping[str, float] | None = None,
) -> WeekDecision:
    """Gate ``shocks`` at ``as_of`` and return the theses. Writes nothing (spec §11.2, §11.3, §10.2).

    World state and priced-in paths are read at ``as_of`` (``published_at`` / state rows at or
    before that instant). No ledger line, week log, report, or manifest is written. ``research``
    is applied after the four gates, and a bar that is not met forces no-trade: that bar is the
    paper-promotion rule (spec §10.3), so a walk-forward passes a met result and does not treat
    it as an input. ``exposure_check`` defaults to ``pending``, the paper clock refusing to
    approve a position; ``passed`` is only for an evaluation that is not asking that question.
    With an ``approver`` (spec §7.4, §7.5, §11.4; ``macrosys.decide.approve``) the exposure
    check, cost, scenario loss, and regime flag come from the store at ``as_of`` and are written
    onto the thesis before the gates run; ``current`` is the signed book already on, by ticker.
    The expression gate still fails a failed check, a horizon outside the mandate, or a short
    the mandate does not allow. A shock whose ``information_cutoff`` is after ``as_of`` raises
    ``ValueError``. ``library`` defaults to the bundled mechanism directory.
    """
    if exposure_check not in ("pending", "passed"):
        raise ValueError("exposure_check must be 'pending' or 'passed'")
    held = dict(current or {})
    for shock in shocks:
        if shock.information_cutoff > as_of:
            raise ValueError(
                f"shock {shock.id} information_cutoff is after as_of "
                f"({shock.information_cutoff.isoformat()} > {as_of.isoformat()})"
            )
    triggered = set(triggered_ids or ())
    loaded = library if library is not None else load_library(_mechanisms_dir())
    state = WorldStateStore(conn).view(as_of)
    notes: list[ShockNote] = []
    theses: list[Thesis] = []
    reasons: list[str] = []
    if not shocks:
        reasons.append("no open shock filed and no trigger fired")
    for shock in shocks:
        is_triggered = shock.id in triggered
        try:
            route = route_for(shock)
        except KeyError as exc:
            # Spec §11.3: a propagation that cannot run fails the mechanism gate. The shock stays on
            # record and the missing route is a backlog line, not a broken week.
            message = f"no library route for shock type {shock.type!r}; {exc.args[0]}"
            notes.append(
                ShockNote(
                    shock_id=shock.id,
                    action="no_trade",
                    failing_gate="mechanism",
                    reason=message,
                    triggered=is_triggered,
                )
            )
            reasons.append(f"{shock.id}: {message}")
            continue
        thesis, decision, approval = _one_shock(
            conn,
            as_of,
            shock,
            route,
            loaded,
            mandate,
            research,
            state,
            book,
            exposure_check,
            approver,
            held,
        )
        theses.append(thesis)
        notes.append(
            ShockNote(
                shock_id=shock.id,
                action=decision.action,
                failing_gate=decision.failing_gate,
                reason=decision.reason,
                thesis_id=thesis.thesis_id,
                triggered=is_triggered,
                weight=None if approval is None else approval.weight,
                approval_reasons=[] if approval is None else list(approval.reasons),
            )
        )
    if any(note.error for note in notes):
        status: WeekStatus = "error"
    else:
        status = "ok"
    reasons.extend(_research_reasons(research))
    return WeekDecision(
        status=status,
        reasons=reasons,
        n_shocks=len(shocks),
        notes=notes,
        theses=theses,
    )


def _decide_shocks(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    config: PipelineConfig,
    shocks_path: str | Path,
    mandate: Mandate,
    research: ResearchGateResult,
    out: Path,
    book: RestingBook | None,
    triggers_path: str | Path | None,
    approver: Approver | None = None,
) -> _Prepared:
    filed = load_open_shocks(shocks_path)
    for shock in filed:
        if shock.information_cutoff > as_of:
            raise ValueError(
                f"shock {shock.id} information_cutoff is after as_of "
                f"({shock.information_cutoff.isoformat()} > {as_of.isoformat()})"
            )
    fired = _scan_triggers(conn, as_of, config, triggers_path)
    shocks = _merge_shocks(filed, fired)
    decision = decide_week(
        conn,
        as_of,
        shocks,
        mandate=mandate,
        research=research,
        book=book,
        triggered_ids={item.shock.id for item in fired},
        approver=approver,
    )
    digest = _write_store_manifest(conn, config, out)
    return _Prepared(
        status=decision.status,
        reasons=list(decision.reasons),
        n_shocks=decision.n_shocks,
        notes=list(decision.notes),
        theses=list(decision.theses),
        manifest_digest=digest,
        baseline=book,
        fired=fired,
    )


def _scan_triggers(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    config: PipelineConfig,
    triggers_path: str | Path | None,
) -> list[Fired]:
    """Run the trigger rules at ``as_of`` against the store, or nothing when no file is named."""
    if triggers_path is None:
        return []
    rules = load_triggers(_readable(triggers_path))
    return scan(StoreReader(conn, config.price_instrument_id), rules, as_of)


def _merge_shocks(filed: list[Shock], fired: list[Fired]) -> list[Shock]:
    """Hand-filed shocks first, then triggered ones; a shared id is a configuration error."""
    seen: set[str] = set()
    merged: list[Shock] = []
    for shock in [*filed, *(item.shock for item in fired)]:
        if shock.id in seen:
            raise ValueError(f"shock id {shock.id} appears twice (filed and triggered, or filed twice)")
        seen.add(shock.id)
        merged.append(shock)
    return merged


def _one_shock(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    shock: Shock,
    route: Route,
    library: Library,
    mandate: Mandate,
    research: ResearchGateResult,
    state: object,
    book: RestingBook | None,
    exposure_check: Literal["pending", "passed"] = "pending",
    approver: Approver | None = None,
    current: Mapping[str, float] | None = None,
) -> tuple[Thesis, Decision, Approval | None]:
    path = _library_path(route, library)
    missing: str | None = None
    unit_error: UnitContinuityError | None = None
    try:
        horizon = validate_path(path, library, []).horizon
    except UnitContinuityError as exc:
        unit_error = exc
        horizon = library.get(route.steps[0].mechanism_id).horizon.to_core()
    propagation = None
    param_version = _parameter_version(route)
    run_id = _run_id(shock, as_of, library.version, route, param_version)
    if unit_error is None:
        try:
            propagation = Propagator(library).run(
                PropagationShock(
                    shock_id=shock.id,
                    magnitude=shock.size,
                    target_input=route.target_input,
                ),
                state,  # type: ignore[arg-type]
                path,
                run_id=run_id,
            )
        except MissingParameterError as exc:
            missing = exc.parameter
        except UnitContinuityError as exc:
            unit_error = exc
    market, priced_error = _resolve_priced_in(route, horizon, conn, as_of)
    thesis = _thesis(
        shock,
        as_of,
        route,
        path,
        library,
        horizon,
        mandate,
        propagation,
        missing,
        unit_error,
        run_id,
        param_version,
        market,
        priced_error,
        book,
        exposure_check,
    )
    approval: Approval | None = None
    if approver is not None:
        approval = approver.approve(thesis, conn, as_of, mandate, dict(current or {}))
        thesis = _with_approval(thesis, approval)
    decision = _apply_research(
        _decision(thesis, mandate, horizon, propagation is not None, approval), research
    )
    return thesis, decision, approval


def _with_approval(thesis: Thesis, approval: Approval) -> Thesis:
    """Write the approver's expression and risk facts onto the thesis record (spec §7.7).

    The gates read ``expression.exposure_vector_check`` and ``expression.cost_estimate_bp`` from
    the thesis, so the approval has to be on the record before ``decide`` runs. An infinite cost
    (an uncostable futures roll or borrow) is kept as infinity so the gap gate fails it.
    """
    expression = thesis.expression.model_copy(
        update={
            "exposure_vector_check": "passed" if approval.exposure_match else "failed",
            "cost_estimate_bp": approval.cost_estimate_bp,
            "structure_notes": " ".join(approval.reasons) or thesis.expression.structure_notes,
        }
    )
    sizing = thesis.sizing_inputs.model_copy(
        update={
            "vol_annual": approval.vol_annual if approval.vol_annual > 0 else thesis.sizing_inputs.vol_annual,
            "scenario_pnl": {"worst": -abs(approval.scenario_loss)},
        }
    )
    return thesis.model_copy(update={"expression": expression, "sizing_inputs": sizing})


def _library_path(route: Route, library: Library) -> PropagationPath:
    """Build the route with no stub coefficients (spec §3.4, §11.2).

    Parameter overrides stay empty and no unit converter is attached, so a ``stub:S0.1``
    elasticity or bridge is not an input. Competing effects are closed the same way as
    ``Route.propagation_path``: a competitor that is not a later step is accepted. The
    acceptance threshold and the materiality threshold use provenance ``forward_week``.
    [J] The materiality threshold is zero shares, a closure setting so a non-zero effect is
    not stopped. It is not a live materiality judgment about this thesis.
    """
    accepted: list[AcceptedCompeting] = []
    seen: set[str] = set()
    for index, step in enumerate(route.steps):
        spec = library.get(step.mechanism_id)
        later = {route.steps[j].mechanism_id for j in range(index + 1, len(route.steps))}
        for competitor in spec.competing:
            if competitor in later or competitor in seen:
                continue
            accepted.append(
                AcceptedCompeting(
                    mechanism=competitor,
                    observable="not on this forward path",
                    threshold=_qty(0.01, "share"),
                )
            )
            seen.add(competitor)
    return PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id=step.mechanism_id,
                bindings=dict(step.bindings),
                parameter_overrides={},
                fixed_inputs={
                    name: qty
                    for name, qty in step.fixed_inputs.items()
                    if qty.provenance != STUB_PROVENANCE
                },
                consumes=list(step.consumes),
            )
            for step in route.steps
        ],
        accepted_competing=accepted,
        materiality_threshold=_qty(0.0, "share"),
    )


def _qty(value: float, unit: str) -> Quantity:
    return Quantity(value=value, unit=unit, provenance=PROVENANCE)


def _expression_notes(instrument: str, exposure_check: Literal["pending", "passed"]) -> str:
    if exposure_check == "pending":
        return (
            f"Forward week records the forecast only. {instrument} is not an approved "
            "position; the exposure vector check has not been run."
        )
    return (
        f"Walk-forward evaluation records the exposure check as passed for {instrument}. "
        "The paper clock leaves this check pending so it does not approve a position; "
        "that stub is not an evaluation input (spec §10.2, §11.3)."
    )


def _research_from_cases(mandate: Mandate, db_path: str | Path) -> ResearchGateResult:
    """Score the case library and apply the mandate's research-to-paper bar (spec §10.3).

    Held-out group is ``supply``, the same default as the cases CLI. Counts are evidential
    outcomes only; ``research_to_paper`` decides whether the bar is met. When the store exists it
    is opened read-only so case inputs come from ``snapshot(cutoff)`` exactly as ``cases score``
    reads them; the connection is closed before the week opens the store for writing.
    """
    from macrosys.cases.runner import CaseRunner, CaseSuite
    from macrosys.cases.schema import default_cases_dir, load_cases

    library = load_library(_mechanisms_dir())
    store_file = Path(db_path)
    store = duckdb.connect(str(store_file), read_only=True) if store_file.is_file() else None
    try:
        suite = CaseSuite(CaseRunner(store=store))
        report = suite.run(load_cases(default_cases_dir()), library, set(_HELD_OUT))
    finally:
        if store is not None:
            store.close()
    gate = mandate.promotion_gates.research_to_paper
    return research_to_paper(
        min_cases_scored=gate.min_cases_scored,
        min_sign_accuracy=gate.min_sign_accuracy,
        counts=_counts(report),
    )


def _counts(report: SuiteReport) -> ResearchCounts:
    results = report.results
    named = [item for item in results if item.dominant_competing_listed is not None]
    return ResearchCounts(
        n_scored=sum(item.n_scored for item in results),
        n_evidential=sum(item.n_evidential for item in results),
        n_evidential_correct=sum(item.n_evidential_correct for item in results),
        n_cases_with_evidential=sum(1 for item in results if item.n_evidential > 0),
        held_out_evidential=report.held_out.n_evidential,
        held_out_evidential_correct=report.held_out.n_evidential_correct,
        n_cases_naming_competing=len(named),
        n_cases_competing_listed=sum(1 for item in named if item.dominant_competing_listed),
    )


def _research_reasons(research: ResearchGateResult) -> list[str]:
    if research.met:
        return []
    if research.reasons:
        return list(research.reasons)
    return ["research sign bar is not met"]


def _apply_research(decision: Decision, research: ResearchGateResult) -> Decision:
    """Force no-trade when the research sign bar is not met (spec §10.3).

    The four gates still run. When they all pass and the bar does not, the recorded reason is
    the first research-gate reason. A gate that already failed keeps its own reason.
    """
    if research.met or decision.action != "trade":
        return decision
    reason = research.reasons[0] if research.reasons else "research sign bar is not met"
    return decision.model_copy(update={"action": "no_trade", "reason": reason})


def _required_pairs(config: PipelineConfig) -> list[tuple[str, int]]:
    required = [(item.series_id, item.max_lag_days) for item in config.required]
    for group in config.series:
        required.extend((series_id, group.max_lag_days) for series_id in group.series_ids)
    return required


def _write_store_manifest(conn: duckdb.DuckDBPyConnection, config: PipelineConfig, out: Path) -> str:
    manifest = build_manifest(
        conn,
        config.raw_root,
        config.library_version,
        config.parameter_set,
        config.code_commit,
    )
    write_manifest(out / "manifest.json", manifest)
    return manifest.digest


def _freeze_all(theses: list[Thesis], ledger_path: Path, as_of: datetime) -> int:
    """Append theses that are not already on the ledger (spec §10.2).

    A retry after the ledger write and before the week log must not add a second line for the
    same thesis id. Already-frozen ids still count toward ``n_theses``.
    """
    if not theses:
        return 0
    ledger = ForecastLedger(ledger_path)
    seen = set()
    if ledger_path.exists():
        seen = {entry.thesis_id for entry in ledger.entries(datetime(9999, 12, 31, tzinfo=UTC))}
    frozen = 0
    for thesis in theses:
        if thesis.thesis_id in seen:
            frozen += 1
            continue
        ledger.freeze(thesis, frozen_at=as_of)
        seen.add(thesis.thesis_id)
        frozen += 1
    return frozen


def _assert_new_week(path: Path, day: date) -> None:
    for record in read_week_log(path):
        if _record_day(record) == day:
            raise DuplicateWeekError(f"a forward week for {day.isoformat()} is already in {path}")


def _record_day(record: dict[str, object]) -> date:
    text = str(record.get("as_of", ""))
    if "T" in text:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.date()
        return parsed.astimezone(UTC).date()
    return date.fromisoformat(text[:10])


def _append_week(path: Path, result: WeekResult, day: date) -> None:
    payload = {
        "as_of": day.isoformat(),
        "baseline": result.baseline,
        "manifest_digest": result.manifest_digest,
        "n_shocks": result.n_shocks,
        "n_theses": result.n_theses,
        "paper_allowed": result.paper_allowed,
        "reasons": result.reasons,
        "research_evaluable": result.research_evaluable,
        "status": result.status,
        "triggered": result.triggered,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(prefix + line + "\n")


def _write_report(
    result: WeekResult,
    coverage: CoverageReport,
    research: ResearchGateResult,
    prepared: _Prepared,
) -> None:
    result.report_path.write_text(_render_report(result, coverage, research, prepared), encoding="utf-8")


def _render_report(
    result: WeekResult,
    coverage: CoverageReport,
    research: ResearchGateResult,
    prepared: _Prepared,
) -> str:
    """Plain-sentence week note: as_of, coverage, the resting book, triggers, each decision, paper,
    and the ledger path."""
    lines = [
        f"This is the forward week as of {result.as_of.date().isoformat()} ({result.as_of.isoformat()}).",
        "",
        *_coverage_sentences(coverage),
    ]
    if prepared.baseline_error is not None:
        lines.append(
            f"The resting book cannot be stated: {prepared.baseline_error}. "
            "The decision is skipped and the book is kept."
        )
    lines.append("")
    if result.status != "blocked":
        if prepared.baseline is None:
            lines.append("The mandate has no resting book: with no passing thesis the book is cash.")
        else:
            lines.append(prepared.baseline.sentence() + " It is held when no thesis passes.")
        lines.append("")
    for item in prepared.fired:
        lines.append(item.sentence())
    if prepared.fired:
        lines.append("")
    if result.n_shocks == 0 and result.status != "blocked":
        lines.append("No open shock is filed and no trigger fired, so the recorded action is no-trade.")
    for note in result.decisions:
        source = "Triggered shock" if note.triggered else "Shock"
        if note.error:
            lines.append(f"{source} {note.shock_id} was not frozen. {note.reason}")
            continue
        if note.action == "no_trade" and note.failing_gate is None:
            gate = "The four gates passed, and the research bar kept this from a trade."
        elif note.failing_gate is None:
            gate = "No gate failed."
        else:
            gate = f"The failing gate is {note.failing_gate}."
        lines.append(f"{source} {note.shock_id} decision is {note.action}. {gate} {note.reason}")
        if note.weight is not None:
            size = f"The approver's size is {note.weight * 100:.1f}% of NAV."
            detail = " ".join(note.approval_reasons)
            lines.append(f"{size} {detail}".rstrip())
    lines.append("")
    if result.paper_allowed:
        lines.append("Paper is allowed.")
    else:
        lines.append("Paper is not allowed.")
        if research.reasons:
            lines.append(research.reasons[0])
        elif not research.met:
            lines.append("The research sign bar is not met.")
    lines.append(f"The ledger path is {result.ledger_path}.")
    return "\n".join(lines) + "\n"


def _coverage_sentences(coverage: CoverageReport) -> list[str]:
    if not coverage.rows:
        return ["No series is required, so coverage is clear."]
    sentences = [f"{row.series_id} is {row.status}." for row in coverage.rows]
    if coverage.blocking:
        sentences.append("Coverage blocked the week. The decision is skipped and the book is kept.")
    else:
        sentences.append("Coverage is clear.")
    return sentences


def _resolve_priced_in(
    route: Route,
    horizon: Horizon,
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
) -> tuple[MarketPricedIn | None, NoPricedInError | None]:
    """Market-implied change for the route's entity at ``as_of``, or the named gap (spec §7.3)."""
    implied = StoreImpliedPaths(ObservationStore(conn).snapshot(as_of))
    try:
        return priced_in(
            route.effect_entity,
            route.effect_variable,
            _window(horizon),
            implied,
            as_of.date(),
        ), None
    except NoPricedInError as exc:
        return None, exc


def _thesis(
    shock: Shock,
    as_of: datetime,
    route: Route,
    path: PropagationPath,
    library: Library,
    horizon: Horizon,
    mandate: Mandate,
    propagation: object,
    missing: str | None,
    unit_error: UnitContinuityError | None,
    run_id: str,
    param_version: str,
    market: MarketPricedIn | None,
    priced_error: NoPricedInError | None,
    book: RestingBook | None,
    exposure_check: Literal["pending", "passed"] = "pending",
) -> Thesis:
    expected = _effect(route, propagation)
    priced, gap, priced_missing = _thesis_priced(expected, market, priced_error)
    months = _horizon_months(horizon, mandate)
    missing_evidence: list[MissingEvidence] = []
    day = as_of.date().isoformat()
    if missing is None and unit_error is None:
        claim = (
            f"Forward week {shock.id} as of {day}: propagated {route.effect_variable} minus the "
            f"priced-in quantity over {months:g} months (spec §7.7, §11.2)."
        )
    elif missing is not None:
        owner = _parameter_owner(library, route, missing)
        unset = [item.name for item in library.get(owner).parameters if item.source == "TODO_estimate"]
        detail = ", ".join(unset) if unset else missing
        claim = (
            f"Forward week {shock.id} as of {day}: {owner} refused to run because parameter "
            f"{missing} is TODO_estimate, so this is not a trade (spec §3.1, §11.2)."
        )
        missing_evidence.append(
            MissingEvidence(
                description=(
                    f"{owner} parameter {missing} is TODO_estimate; the mechanism refuses to run "
                    f"(spec §3.1). Unset parameters on that record: {detail}."
                ),
                blocking=True,
            )
        )
    else:
        assert unit_error is not None
        claim = (
            f"Forward week {shock.id} as of {day}: the path has no declared unit converter, "
            f"so this is not a trade (spec §3.4, §11.2)."
        )
        missing_evidence.append(
            MissingEvidence(
                description=(
                    f"unit continuity failed: {unit_error.source} outputs {unit_error.from_unit}, "
                    f"{unit_error.dest} consumes {unit_error.to_unit}, and no converter is declared "
                    "(spec §3.4)."
                ),
                blocking=True,
            )
        )
    if priced_missing is not None:
        missing_evidence.append(priced_missing)
    high = expected.high if expected.high is not None else expected.mid
    low = expected.low if expected.low is not None else expected.mid
    width = abs(float(high) - float(low))
    return Thesis(
        thesis_id=f"T-{shock.id}-{day}",
        created=as_of,
        shock_ref=shock.id,
        claim=claim,
        mechanism_path=route.mechanism_ids(),
        propagation_run=run_id,
        expected_effect=ExpectedEffect(
            entity=route.effect_entity,
            variable=route.effect_variable,
            change=expected,
            window_q=_window(horizon),
        ),
        priced_in=priced,
        gap=gap,
        competing_effects_accepted=[
            CompetingEffect(
                mechanism=item.mechanism,
                observable=item.observable,
                threshold=f"{item.threshold.mid:g} {item.threshold.unit}",
            )
            for item in path.accepted_competing
        ],
        expression=Expression(
            instrument=route.instrument,
            direction="long",
            # [J] Pending on purpose when this is the paper clock: the week records the forecast and
            # does not approve a position, so the expression gate fails on its own terms. Evaluation
            # passes "passed" because that stub is not an input to a historical score (spec §10.2).
            exposure_vector_check=exposure_check,
            structure_notes=_expression_notes(route.instrument, exposure_check),
            cost_estimate_bp=0.0,
        ),
        sizing_inputs={
            "vol_annual": 0.20,
            "scenario_pnl": {"unestimated": 0.0},
            "factor_loadings": {"unestimated": 0.0},
        },
        invalidation=Invalidation(
            rules=[
                InvalidationRule(
                    observable="world state at the decision instant",
                    condition="a later vintage replaces an input this week used",
                    action="downgrade",
                )
            ],
            time_stop=as_of.date() + timedelta(days=365),
        ),
        information_cutoff=as_of,
        evidence_quality=EvidenceQuality(observations="low", claims="low", inferences="n/a"),
        scenarios=[
            Scenario(name="base", weight=None, outcome_range=expected),
            Scenario(
                name="premise_fails",
                weight=None,
                outcome_range=Quantity(
                    value=None, unit=expected.unit, low=-1.0, high=0.0, provenance=PROVENANCE
                ),
            ),
        ],
        forecast_precision=ForecastPrecision(range_width_pct=width, interval_level=0.8),
        expected_return=ExpectedReturn(net_of_costs_range=gap, horizon_m=months),
        no_trade_alternative=_no_trade_alternative(book),
        versions=Versions(
            library=library.version,
            parameters=param_version,
            code_commit=CODE_COMMIT,
            model_ids={},
        ),
        missing_evidence=missing_evidence,
        review_schedule="weekly",
        status="proposed",
    )


def _no_trade_alternative(book: RestingBook | None) -> str:
    if book is None:
        return "Hold the current book. This week does not size a position (spec §11.3)."
    return f"Hold the resting book. {book.sentence()} This week does not size a position (spec §11.3)."


def _decision(
    thesis: Thesis,
    mandate: Mandate,
    horizon: Horizon,
    run_finished: bool,
    approval: Approval | None = None,
) -> Decision:
    """Run the four gates (spec §11.2, §11.3).

    Without an approval the expression and risk facts are the paper clock's placeholders (match
    assumed, book assumed inside limits, regime stable) and the pending exposure check fails the
    expression gate on its own. With one, those facts are the approver's.
    """
    configured = mandate.gap_gate.min_expected_gap_after_costs_bp
    min_gap_bp = 0.0 if configured is None else float(configured)
    months = thesis.expected_return.horizon_m
    return decide(
        thesis,
        run_valid=run_finished,
        identity_residuals_ok=run_finished,
        min_gap_bp=min_gap_bp,
        exposure_match=True if approval is None else approval.exposure_match,
        horizon_ok=_horizon_ok(horizon, months, mandate),
        post_trade_ok=True if approval is None else approval.post_trade_ok,
        regime_flag="stable" if approval is None else approval.regime_flag,
        shorting_allowed=mandate.shorting.allowed,
    )


def _effect(route: Route, propagation: object) -> Quantity:
    if propagation is None:
        return Quantity(value=None, unit=route.effect_unit, low=-1.0, high=1.0, provenance=PROVENANCE)
    steps = getattr(propagation, "steps", None)
    if not steps:
        raise KeyError(f"{route.mechanism_ids()[-1]} did not produce {route.effect_output}")
    for step in reversed(steps):
        found = step.outputs.get(route.effect_output)
        if found is not None:
            return _as_closed_range(found)
    raise KeyError(f"{route.mechanism_ids()[-1]} did not produce {route.effect_output}")


def _as_closed_range(qty: Quantity) -> Quantity:
    low = qty.low if qty.low is not None else qty.value
    high = qty.high if qty.high is not None else qty.value
    if low is None or high is None:
        raise ValueError("propagated effect has no edges")
    return Quantity(
        value=qty.value if qty.value is not None else qty.mid,
        unit=qty.unit,
        low=low,
        high=high,
        provenance=qty.provenance or PROVENANCE,
    )


def _thesis_priced(
    expected: Quantity,
    market: MarketPricedIn | None,
    error: NoPricedInError | None,
) -> tuple[PricedIn, Quantity, MissingEvidence | None]:
    if market is not None:
        gap = market.to_gap(expected)
        return (
            PricedIn(
                source=list(market.sources),
                implied_change=market.implied_change,
                method_notes=market.method_notes,
                confidence=market.confidence,
            ),
            gap,
            None,
        )
    assert error is not None
    placeholder = Quantity(value=0.0, unit=expected.unit, provenance=PROVENANCE)
    note = f"NoPricedInError: {error}"
    return (
        PricedIn(
            source=["NoPricedInError"],
            implied_change=placeholder,
            method_notes=note,
            confidence=None,
        ),
        expected.sub(placeholder, provenance="priced_in"),
        MissingEvidence(description=note, blocking=True),
    )


def _parameter_owner(library: Library, route: Route, parameter: str) -> str:
    for step in route.steps:
        spec = library.get(step.mechanism_id)
        if any(item.name == parameter for item in spec.parameters):
            return spec.id
    return route.steps[0].mechanism_id


def _horizon_months(horizon: Horizon, mandate: Mandate) -> float:
    months = max(horizon.peak_q, horizon.impact_q) * 3.0
    if months <= 0.0:
        return float(mandate.horizon_months.min)
    return months


def _horizon_ok(horizon: Horizon, months: float, mandate: Mandate) -> bool:
    """True when ``months`` sits in the mandate band and inside the path window (spec §7.6, §11.3)."""
    if not (mandate.horizon_months.min <= months <= mandate.horizon_months.max):
        return False
    if months < horizon.impact_q * 3.0:
        return False
    if horizon.decay_q is not None and months > horizon.decay_q * 3.0:
        return False
    return True


def _window(horizon: Horizon) -> tuple[float, float]:
    end = horizon.decay_q if horizon.decay_q is not None else horizon.peak_q
    return (horizon.impact_q, max(end, horizon.impact_q))


def _parameter_version(route: Route) -> str:
    rows = [{"mechanism": step.mechanism_id, "overrides": []} for step in route.steps]
    return _sha(json.dumps(rows, sort_keys=True, separators=(",", ":")))


def _run_id(shock: Shock, as_of: datetime, library_version: str, route: Route, param_version: str) -> str:
    digest = _sha(
        shock.model_dump_json(),
        as_of.isoformat(),
        library_version,
        ",".join(route.mechanism_ids()),
        param_version,
    )
    return "R-" + digest[:32]


def _sha(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _mechanisms_dir() -> Path:
    bundled = _REPO / "mechanisms"
    if bundled.is_dir():
        return bundled
    return Path("mechanisms")


def _readable(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        bundled = _REPO / candidate
        if bundled.is_file():
            return bundled
    return candidate
