"""One pass of the decision loop on the stub world (spec §1.1, §4.4, §7.7, §11.3, §13 S0.1).

``run_once`` does not branch on shock type. The path comes from ``route_for``. A mechanism that
refuses because a parameter is still ``TODO_estimate`` is recorded as blocking missing evidence;
the pass still freezes a ledger entry and writes a no-trade decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

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
from macrosys.eval.ledger import ForecastLedger, LedgerEntry
from macrosys.mechanisms import (
    MissingParameterError,
    PropagationRun,
    Propagator,
    validate_path,
)
from macrosys.mechanisms import Shock as PropagationShock
from macrosys.mechanisms.library import Library, load_library
from macrosys.skeleton.report import render_report, write_report
from macrosys.skeleton.routing import Route, route_for
from macrosys.skeleton.stubs import STUB_PROVENANCE, Clock, build_stub_world
from macrosys.store.manifest import Manifest, build_manifest, write_manifest
from macrosys.store.schema import create_schema
from macrosys.store.state import WorldStateStore

# Walking-skeleton pin. Not a git sha: other packages move under the same checkout, and the
# replay tuple for this pass is the stub world, the library hash, and this label (spec §4.4).
CODE_COMMIT = "S0.1"

_REPO = Path(__file__).resolve().parents[2]


class SkeletonResult(BaseModel):
    """Artefacts of one skeleton pass (spec §4.4, §13 S0.1).

    ``digest`` is sha256 of the manifest digest and the ledger entry hash, so two passes with
    the same inputs and an empty starting ledger match.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    shock_id: str
    as_of: datetime
    thesis: Thesis
    decision: Decision
    ledger_entry: LedgerEntry
    manifest: Manifest
    report_path: Path
    digest: str
    mechanism_ids: list[str]
    identity_checks: dict[str, float]
    missing_parameter: str | None = None


def run_once(
    shock: Shock,
    as_of: date | datetime,
    *,
    ledger_path: str | Path,
    out_dir: str | Path,
    library_dir: str | Path = "mechanisms",
    mandate_path: str | Path = "config/mandate.example.yaml",
    clock: Clock | None = None,
) -> SkeletonResult:
    """Run shock → propagation → gap → gates → ledger → manifest → report (spec §1.1, §13 S0.1).

    ``as_of`` is a calendar date, taken as 23:59:59 UTC, or an aware datetime. ``clock``, when
    passed, supplies ``ingested_at`` for the stub observations; the default is ``as_of`` itself,
    so a replay does not call the wall clock. The priced-in range is a stub constructed to
    overlap the propagated change, and the gap is expected minus that range (spec §7.7). A gap
    that includes zero, or blocking missing evidence, yields ``no_trade`` (spec §11.3).
    """
    as_of_dt = as_of_datetime(as_of)
    if shock.information_cutoff > as_of_dt:
        raise ValueError(
            f"shock {shock.id} information_cutoff is after as_of "
            f"({shock.information_cutoff.isoformat()} > {as_of_dt.isoformat()})"
        )
    library = load_library(_existing(library_dir))
    mandate = Mandate.load(_existing(mandate_path))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ledger_file = Path(ledger_path)
    ledger_file.parent.mkdir(parents=True, exist_ok=True)

    route = route_for(shock)
    conn = duckdb.connect(database=":memory:")
    try:
        create_schema(conn)
        world = build_stub_world(conn, as_of_dt, clock=clock)
        state = WorldStateStore(conn).view(as_of_dt)
        path = route.propagation_path(library)
        converters = list(route.converters)
        horizon = validate_path(path, library, converters).horizon
        missing: str | None = None
        propagation: PropagationRun | None = None
        try:
            propagation = Propagator(library, converters).run(
                PropagationShock(
                    shock_id=shock.id,
                    magnitude=shock.size,
                    target_input=route.target_input,
                ),
                state,
                path,
            )
        except MissingParameterError as exc:
            missing = exc.parameter
        param_version = _parameter_version(route)
        run_id = _run_id(shock, as_of_dt, library.version, route, param_version)
        if propagation is not None:
            propagation = propagation.model_copy(update={"run_id": run_id})
        thesis = _thesis(
            shock,
            as_of_dt,
            route,
            library,
            horizon,
            mandate,
            propagation,
            missing,
            run_id,
            param_version,
        )
        decision = _decision(thesis, mandate, horizon, propagation is not None)
        manifest = build_manifest(conn, out / "raw", library.version, param_version, CODE_COMMIT)
    finally:
        conn.close()

    entry = ForecastLedger(ledger_file).freeze(thesis, frozen_at=as_of_dt)
    digest = combine_digest(manifest.digest, entry.entry_hash)
    text = render_report(
        shock=shock,
        mechanism_ids=route.mechanism_ids(),
        library=library,
        horizon=horizon,
        identity_checks={} if propagation is None else dict(propagation.identity_checks),
        expected=thesis.expected_effect.change,
        priced=thesis.priced_in.implied_change,
        gap=thesis.gap,
        decision=decision,
        world=world,
        missing_parameter=missing,
        entry_hash=entry.entry_hash,
        manifest_digest=manifest.digest,
        digest=digest,
    )
    report_path = out / "report.md"
    write_report(report_path, text)
    write_manifest(out / "manifest.json", manifest)
    return SkeletonResult(
        shock_id=shock.id,
        as_of=as_of_dt,
        thesis=thesis,
        decision=decision,
        ledger_entry=entry,
        manifest=manifest,
        report_path=report_path,
        digest=digest,
        mechanism_ids=route.mechanism_ids(),
        identity_checks={} if propagation is None else dict(propagation.identity_checks),
        missing_parameter=missing,
    )


def as_of_datetime(value: date | datetime) -> datetime:
    """Normalise a decision instant to UTC (spec §9.2).

    A date is the end of that UTC day, so a release published earlier the same day is visible.
    A datetime must already carry a timezone.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=UTC)
    raise TypeError("as_of must be a date or a datetime")


def combine_digest(manifest_digest: str, entry_hash: str) -> str:
    """sha256 over the manifest digest and the ledger entry hash (spec §4.4)."""
    return hashlib.sha256(f"{manifest_digest}\n{entry_hash}\n".encode()).hexdigest()


def _existing(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    from_cwd = Path.cwd() / candidate
    if from_cwd.exists():
        return from_cwd
    from_repo = _REPO / candidate
    if from_repo.exists():
        return from_repo
    return from_cwd


def _run_id(shock: Shock, as_of: datetime, library_version: str, route: Route, param_version: str) -> str:
    digest = _sha(
        shock.model_dump_json(),
        as_of.isoformat(),
        library_version,
        ",".join(route.mechanism_ids()),
        param_version,
    )
    return "R-" + digest[:32]


def _parameter_version(route: Route) -> str:
    rows = []
    for step in route.steps:
        for name, qty in sorted(step.parameter_overrides.items()):
            rows.append(
                {
                    "high": qty.high,
                    "low": qty.low,
                    "mechanism": step.mechanism_id,
                    "name": name,
                    "provenance": qty.provenance,
                    "unit": qty.unit,
                    "value": qty.value,
                }
            )
    return _sha(json.dumps(rows, sort_keys=True, separators=(",", ":")))


def _sha(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _parameter_owner(library: Library, route: Route, parameter: str) -> str:
    for step in route.steps:
        spec = library.get(step.mechanism_id)
        if any(item.name == parameter for item in spec.parameters):
            return spec.id
    return route.steps[0].mechanism_id


def _effect(route: Route, propagation: PropagationRun | None) -> Quantity:
    if propagation is None:
        return Quantity(value=None, unit=route.effect_unit, low=-1.0, high=1.0, provenance=STUB_PROVENANCE)
    for step in reversed(propagation.steps):
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
        provenance=qty.provenance or STUB_PROVENANCE,
    )


def _priced_stub(expected: Quantity) -> Quantity:
    """A stub priced-in range that overlaps ``expected`` and crosses zero (spec §7.7, §13 S0.1).

    S0.1's acceptance test is a gap that straddles zero. This range is built so that
    expected − priced includes zero for any finite expected change. It is not a curve, a
    consensus print, or an implied-growth estimate. [J]
    """
    mid = expected.mid
    return Quantity(
        value=None,
        unit=expected.unit,
        low=min(-1.0, mid - 1.0),
        high=max(1.0, mid + 1.0),
        provenance=STUB_PROVENANCE,
    )


def _gap(expected: Quantity, priced: Quantity) -> Quantity:
    """Interval subtraction, expected minus priced, in ``expected.unit`` (spec §7.7)."""
    if expected.unit != priced.unit:
        priced = priced.to(expected.unit)
    e_lo = expected.low if expected.low is not None else expected.value
    e_hi = expected.high if expected.high is not None else expected.value
    p_lo = priced.low if priced.low is not None else priced.value
    p_hi = priced.high if priced.high is not None else priced.value
    if e_lo is None or e_hi is None or p_lo is None or p_hi is None:
        raise ValueError("gap needs a low and a high on both the expected change and the priced-in range")
    return Quantity(
        value=None,
        unit=expected.unit,
        low=float(e_lo) - float(p_hi),
        high=float(e_hi) - float(p_lo),
        provenance=STUB_PROVENANCE,
    )


def _horizon_months(horizon: Horizon, mandate: Mandate) -> float:
    months = max(horizon.peak_q, horizon.impact_q) * 3.0
    if months <= 0.0:
        return float(mandate.horizon_months.min)
    return months


def _horizon_ok(horizon: Horizon, months: float, mandate: Mandate) -> bool:
    """True when ``months`` sits in the mandate band and inside the path window (spec §7.6, §11.3).

    Quarters convert to months at 3 months per quarter. A path with no decay has no upper bound
    from the mechanisms. [J]
    """
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


def _thesis(
    shock: Shock,
    as_of: datetime,
    route: Route,
    library: Library,
    horizon: Horizon,
    mandate: Mandate,
    propagation: PropagationRun | None,
    missing: str | None,
    run_id: str,
    param_version: str,
) -> Thesis:
    expected = _effect(route, propagation)
    priced = _priced_stub(expected)
    gap = _gap(expected, priced)
    months = _horizon_months(horizon, mandate)
    if missing is None:
        claim = (
            f"Stub pass {shock.id}: propagated {route.effect_variable} minus a stub priced-in "
            f"range does not clear zero over {months:g} months (spec §7.7, §11.3)."
        )
        missing_evidence: list[MissingEvidence] = []
    else:
        owner = _parameter_owner(library, route, missing)
        unset = [item.name for item in library.get(owner).parameters if item.source == "TODO_estimate"]
        detail = ", ".join(unset) if unset else missing
        claim = (
            f"Stub pass {shock.id}: {owner} refused to run because parameter {missing} is "
            f"TODO_estimate, so this is not a trade (spec §3.1, §11.3)."
        )
        missing_evidence = [
            MissingEvidence(
                description=(
                    f"{owner} parameter {missing} is TODO_estimate; the mechanism refuses to run "
                    f"(spec §3.1). Unset parameters on that record: {detail}."
                ),
                blocking=True,
            )
        ]
    high = expected.high if expected.high is not None else expected.mid
    low = expected.low if expected.low is not None else expected.mid
    width = abs(float(high) - float(low))
    return Thesis(
        thesis_id=f"T-{shock.id}",
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
        priced_in=PricedIn(
            source=[STUB_PROVENANCE],
            implied_change=priced,
            method_notes=(
                "Stub range built to overlap the propagated change and to cross zero. "
                "Not a futures curve, a survey, or an implied-growth reading (spec §7.3, §13 S0.1)."
            ),
        ),
        gap=gap,
        competing_effects_accepted=[
            CompetingEffect(
                mechanism=item.mechanism,
                observable=item.observable,
                threshold=f"{item.threshold.mid:g} {item.threshold.unit}",
            )
            for item in route.propagation_path(library).accepted_competing
        ],
        expression=Expression(
            instrument=route.instrument,
            direction="long",
            exposure_vector_check="pending",
            structure_notes=route.structure_notes,
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
                    observable="stub world state",
                    condition="a published vintage replaces the stub",
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
                outcome_range=Quantity(value=None, unit=expected.unit, low=-1.0, high=0.0, provenance=STUB_PROVENANCE),
            ),
        ],
        forecast_precision=ForecastPrecision(range_width_pct=width, interval_level=0.8),
        expected_return=ExpectedReturn(net_of_costs_range=gap, horizon_m=months),
        no_trade_alternative="Hold the current book. This pass does not size a position (spec §11.3).",
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
    """Run the four gates. The skeleton adds no position (spec §11.2, §11.3).

    The example mandate leaves the extra gap margin null; the gate then uses a zero extra margin
    on top of the expression's cost, which is also zero because cost is not estimated. The
    correlation regime is not computed here and is not flagged deteriorating. The expression
    check stays pending, so that gate fails on its own terms. [J]
    """
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

