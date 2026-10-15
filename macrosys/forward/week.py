"""Prospective weekly decision (spec §11.2, §10.2 item 7, §10.3, §11.3).

``forward_week`` refreshes world state from ``snapshot(as_of)``, runs each filed shock on the
library path, applies the four gates, and appends any thesis to the frozen ledger. The default
action is no-trade. Coverage that blocks is class B: the decision is skipped and the book is
kept. The research sign bar is applied from the mandate; this function does not place an order.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field

from macrosys.cases.runner import CaseSuite, SuiteReport
from macrosys.cases.schema import default_cases_dir, load_cases
from macrosys.core.quantity import Quantity
from macrosys.core.time import Horizon
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
from macrosys.world.refresh import refresh_world_state

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


class WeekResult(BaseModel):
    """What one forward week wrote (spec §11.2, §10.2 item 7)."""

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
) -> WeekResult:
    """Refresh, gate, and freeze one prospective week (spec §11.2, §10.2 item 7, §10.3, §11.3).

    ``as_of`` is a calendar date, taken as the end of that UTC day, or an aware datetime. The
    runbook hash is checked before any thesis is frozen (spec §11.7). Required series are the
    pipeline config's ``required`` list plus each series group's ids at that group's
    ``max_lag_days``. A blocking coverage report is class B: the week is recorded, nothing is
    propagated, and no thesis is appended. With no filed shock the action is no-trade. Each
    shock runs on the library path with stub coefficients dropped. A missing parameter or a unit
    break is blocking missing evidence and still freezes a no-trade thesis. The expression check
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
    research_result = research if research is not None else _research_from_cases(mandate)
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
        if coverage.blocking:
            digest = _write_store_manifest(conn, config, out)
            prepared = _blocked(coverage, research_result, digest)
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
        report_path=out / "report.md",
        ledger_path=ledger_file,
    )
    # The report is rewritten on a retry. The week log is the commit: a crash after the
    # ledger write and before this append can be run again without a second thesis.
    _write_report(result, coverage, research_result)
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


def _blocked(coverage: CoverageReport, research: ResearchGateResult, digest: str) -> _Prepared:
    bad = [f"{row.series_id} is {row.status}" for row in coverage.rows if row.status != "ok"]
    detail = "; ".join(bad) if bad else "a required series is not fresh"
    reasons = [
        f"coverage blocked the week ({detail}); the decision is skipped and the book is kept",
        *_research_reasons(research),
    ]
    return _Prepared(
        status="blocked",
        reasons=reasons,
        n_shocks=0,
        notes=[],
        theses=[],
        manifest_digest=digest,
    )


def _decide_shocks(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    config: PipelineConfig,
    shocks_path: str | Path,
    mandate: Mandate,
    research: ResearchGateResult,
    out: Path,
) -> _Prepared:
    state = WorldStateStore(conn).view(as_of)
    shocks = load_open_shocks(shocks_path)
    for shock in shocks:
        if shock.information_cutoff > as_of:
            raise ValueError(
                f"shock {shock.id} information_cutoff is after as_of "
                f"({shock.information_cutoff.isoformat()} > {as_of.isoformat()})"
            )
    library = load_library(_mechanisms_dir())
    notes: list[ShockNote] = []
    theses: list[Thesis] = []
    reasons: list[str] = []
    if not shocks:
        reasons.append("no open shock filed")
    for shock in shocks:
        try:
            route = route_for(shock)
        except KeyError as exc:
            message = str(exc)
            notes.append(ShockNote(shock_id=shock.id, reason=message, error=True))
            reasons.append(f"{shock.id}: {message}")
            continue
        thesis, decision = _one_shock(conn, as_of, shock, route, library, mandate, research, state)
        theses.append(thesis)
        notes.append(
            ShockNote(
                shock_id=shock.id,
                action=decision.action,
                failing_gate=decision.failing_gate,
                reason=decision.reason,
                thesis_id=thesis.thesis_id,
            )
        )
    if any(note.error for note in notes):
        status: WeekStatus = "error"
    else:
        status = "ok"
    reasons.extend(_research_reasons(research))
    digest = _write_store_manifest(conn, config, out)
    return _Prepared(
        status=status,
        reasons=reasons,
        n_shocks=len(shocks),
        notes=notes,
        theses=theses,
        manifest_digest=digest,
    )


def _one_shock(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    shock: Shock,
    route: Route,
    library: Library,
    mandate: Mandate,
    research: ResearchGateResult,
    state: object,
) -> tuple[Thesis, Decision]:
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
    )
    decision = _apply_research(_decision(thesis, mandate, horizon, propagation is not None), research)
    return thesis, decision


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


def _research_from_cases(mandate: Mandate) -> ResearchGateResult:
    """Score the case library and apply the mandate's research-to-paper bar (spec §10.3).

    Held-out group is ``supply``, the same default as the cases CLI. Counts are evidential
    outcomes only; ``research_to_paper`` decides whether the bar is met.
    """
    library = load_library(_mechanisms_dir())
    report = CaseSuite().run(load_cases(default_cases_dir()), library, set(_HELD_OUT))
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
        "manifest_digest": result.manifest_digest,
        "n_shocks": result.n_shocks,
        "n_theses": result.n_theses,
        "paper_allowed": result.paper_allowed,
        "reasons": result.reasons,
        "research_evaluable": result.research_evaluable,
        "status": result.status,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(prefix + line + "\n")


def _write_report(result: WeekResult, coverage: CoverageReport, research: ResearchGateResult) -> None:
    result.report_path.write_text(_render_report(result, coverage, research), encoding="utf-8")


def _render_report(result: WeekResult, coverage: CoverageReport, research: ResearchGateResult) -> str:
    """Plain-sentence week note: as_of, coverage, each decision, paper, and the ledger path."""
    lines = [
        f"This is the forward week as of {result.as_of.date().isoformat()} ({result.as_of.isoformat()}).",
        "",
        *_coverage_sentences(coverage),
        "",
    ]
    if result.n_shocks == 0 and result.status != "blocked":
        lines.append("No open shock is filed, so the recorded action is no-trade.")
    for note in result.decisions:
        if note.error:
            lines.append(f"Shock {note.shock_id} was not frozen. {note.reason}")
            continue
        if note.action == "no_trade" and note.failing_gate is None:
            gate = "The four gates passed, and the research bar kept this from a trade."
        elif note.failing_gate is None:
            gate = "No gate failed."
        else:
            gate = f"The failing gate is {note.failing_gate}."
        lines.append(f"Shock {note.shock_id} decision is {note.action}. {gate} {note.reason}")
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
            # [J] Pending on purpose. This clock records the forecast and does not approve a position,
            # so the expression gate fails on its own terms.
            exposure_vector_check="pending",
            structure_notes=(
                f"Forward week records the forecast only. {route.instrument} is not an approved "
                "position; the exposure vector check has not been run."
            ),
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
        no_trade_alternative="Hold the current book. This week does not size a position (spec §11.3).",
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


def _decision(thesis: Thesis, mandate: Mandate, horizon: Horizon, run_finished: bool) -> Decision:
    """Run the four gates. This week adds no position (spec §11.2, §11.3)."""
    configured = mandate.gap_gate.min_expected_gap_after_costs_bp
    min_gap_bp = 0.0 if configured is None else float(configured)
    months = thesis.expected_return.horizon_m
    return decide(
        thesis,
        run_valid=run_finished,
        identity_residuals_ok=run_finished,
        min_gap_bp=min_gap_bp,
        exposure_match=True,
        horizon_ok=_horizon_ok(horizon, months, mandate),
        post_trade_ok=True,
        regime_flag="stable",
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
