"""Replay a historical case and score the structural result (spec §10.1 item 2, §4.4).

The runner loads the case fixture into an in-memory store, refreshes world state from the
recipes, and runs the hinted mechanisms through ``Propagator``. When ``compose_path`` is
false, as on every historical case file, each hint is its own one-step path. When it is true,
the hint runs once as a wired ``PropagationPath`` (spec §3.4). A mechanism that refuses
because a parameter is still ``TODO_estimate`` is ``blocked``. That is not a wrong sign.
Rank correlation is ``None`` (n/a) unless both a predicted magnitude and an observed magnitude
exist; this library does not store observed magnitudes.

Every scored outcome is classed as **evidential** or not. An outcome is evidential only when the
mechanism received the sign of the recorded case shock (not a per-mechanism ``shock_value`` the
case author chose) and ran on library parameters (not replay fixtures). A non-evidential hit
shows the formula has the direction its ``form`` states; it says nothing about whether the
library reaches the historical outcome from the shock. Only evidential outcomes may count toward
the research promotion gate (spec §10.3).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from macrosys.cases.replay_params import ReplayParameterSet, load_replay_parameters
from macrosys.cases.schema import Case, ObservedPoint, ReplayStep, StructurePoint, default_cases_dir
from macrosys.core.contracts import Observation
from macrosys.core.quantity import Quantity
from macrosys.mechanisms import (
    AcceptedCompeting,
    MechanismInstance,
    MechanismSpec,
    MissingParameterError,
    PropagationPath,
    Propagator,
    bind_parameters,
)
from macrosys.mechanisms import Shock as PropagationShock
from macrosys.mechanisms.library import Library
from macrosys.skeleton.routing import route_for
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from macrosys.store.state import WorldStateStore
from macrosys.world.recipes import Recipe, load_recipes
from macrosys.world.refresh import RefreshReport, refresh_world_state

_CLOSURE = "case_replay"


class BlockedMechanism(BaseModel):
    """A mechanism that refused to run (spec §3.1, §10.1)."""

    model_config = ConfigDict(frozen=True)

    mechanism_id: str
    parameter: str


class CaseResult(BaseModel):
    """Scores for one replay. ``rank_correlation`` is ``None`` when it is n/a (spec §10.1 item 2)."""

    model_config = ConfigDict(frozen=True)

    case_id: str
    as_of: datetime
    held_out_group: str
    blocked: list[BlockedMechanism] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    n_blocked: int
    n_scored: int
    n_sign_correct: int
    n_sign_wrong: int
    sign_accuracy: float | None
    rank_correlation: float | None
    horizon_error: float | None
    dominant_competing_listed: bool | None
    refresh_skipped: int
    refresh_written: int
    unmapped: list[str] = Field(default_factory=list)
    n_unmapped: int = 0
    n_author_fed: int = 0
    n_fixture_params: int = 0
    n_evidential: int = 0
    n_evidential_correct: int = 0


class GroupAggregate(BaseModel):
    """Counts for one held-out group or for a split (spec §10.1 item 1)."""

    model_config = ConfigDict(frozen=True)

    label: str
    n_cases: int
    n_blocked: int
    n_scored: int
    n_sign_correct: int
    n_sign_wrong: int
    n_evidential: int = 0
    n_evidential_correct: int = 0


class SuiteReport(BaseModel):
    """Per-group scores plus the held-out versus development split (spec §10.1 items 1–2)."""

    model_config = ConfigDict(frozen=True)

    results: list[CaseResult]
    by_group: list[GroupAggregate]
    held_out: GroupAggregate
    development: GroupAggregate


class _StepOut(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    mechanism_id: str
    outputs: dict[str, Quantity]
    competing: list[str]
    peak_q: float
    author_fed: bool = False
    fixture_params: bool = False


class CaseRunner:
    """Replay one case from its fixture and its information cutoff (spec §10.1 item 2).

    ``replay_parameters`` is ``True`` to load ``cases/fixtures/replay_parameters.yaml``, ``False`` to run
    on library parameters only (mechanisms with ``TODO_estimate`` parameters are then blocked),
    or an explicit fixture set.
    """

    def __init__(
        self,
        recipes: list[Recipe] | None = None,
        case_root: str | Path | None = None,
        replay_parameters: ReplayParameterSet | None | bool = True,
    ) -> None:
        self.recipes = recipes
        self.case_root = Path(case_root) if case_root is not None else default_cases_dir()
        if replay_parameters is True:
            self.replay_parameters: ReplayParameterSet = load_replay_parameters()
        elif replay_parameters is False or replay_parameters is None:
            self.replay_parameters = {}
        else:
            self.replay_parameters = replay_parameters

    def replay(self, case: Case, library: Library, as_of: datetime | None = None) -> CaseResult:
        """Refresh state at ``as_of`` and score the hinted path (spec §10.1 item 2, §9.2).

        ``as_of`` defaults to the case information cutoff. Observations published later are
        invisible. Mechanisms that raise ``MissingParameterError`` are counted in ``blocked``
        and are not treated as sign errors. Other failures are listed on ``errors`` and are
        also kept out of the sign score.
        """
        instant = as_of if as_of is not None else case.information_cutoff
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        conn = duckdb.connect(database=":memory:")
        try:
            create_schema(conn)
            ObservationStore(conn).append(_read_fixture(self._fixture(case)))
            recipes = self.recipes if self.recipes is not None else load_recipes()
            report = refresh_world_state(conn, instant, recipes)
            state_store = WorldStateStore(conn)
            for row in case.state_rows:
                state_store.write(
                    instant,
                    row.entity.key(),
                    row.var,
                    Quantity(value=row.value, unit=row.unit, provenance=row.provenance),
                    f"case_fixture:{case.case_id} quality:{row.quality_flag}",
                )
            state = state_store.view(instant)
            steps, blocked, errors = _propagate(case, library, state, self.replay_parameters)
        finally:
            conn.close()
        return _score(case, instant, steps, blocked, errors, report)

    def _fixture(self, case: Case) -> Path:
        path = Path(case.fixture_observations)
        if path.is_absolute():
            return path
        return self.case_root / path


class CaseSuite:
    """Score a set of cases and split held-out groups from the development set (spec §10.1)."""

    def __init__(self, runner: CaseRunner | None = None) -> None:
        self.runner = runner or CaseRunner()

    def run(self, cases: list[Case], library: Library, held_out: set[str]) -> SuiteReport:
        """Replay ``cases`` and aggregate by ``held_out_group`` (spec §10.1 items 1–2).

        A case whose ``held_out_group`` is in ``held_out`` is the held-out split. The rest are
        development. Group totals add up to the same case, blocked, and scored counts as the
        two splits together.
        """
        results = [self.runner.replay(case, library) for case in cases]
        groups = sorted({result.held_out_group for result in results})
        by_group = [_aggregate(group, [item for item in results if item.held_out_group == group]) for group in groups]
        held = [item for item in results if item.held_out_group in held_out]
        dev = [item for item in results if item.held_out_group not in held_out]
        return SuiteReport(
            results=results,
            by_group=by_group,
            held_out=_aggregate("held_out", held),
            development=_aggregate("development", dev),
        )


def _aggregate(label: str, results: list[CaseResult]) -> GroupAggregate:
    return GroupAggregate(
        label=label,
        n_cases=len(results),
        n_blocked=sum(item.n_blocked for item in results),
        n_scored=sum(item.n_scored for item in results),
        n_sign_correct=sum(item.n_sign_correct for item in results),
        n_sign_wrong=sum(item.n_sign_wrong for item in results),
        n_evidential=sum(item.n_evidential for item in results),
        n_evidential_correct=sum(item.n_evidential_correct for item in results),
    )


def _propagate(
    case: Case, library: Library, state, fixtures: ReplayParameterSet
) -> tuple[list[_StepOut], list[BlockedMechanism], list[str]]:
    if case.compose_path:
        return _propagate_composed(case, library, state, fixtures)
    ids = list(case.mechanism_path_hint or [])
    if not ids:
        try:
            route = route_for(case.shock)
        except KeyError as exc:
            return [], [], [str(exc)]
        ids = route.mechanism_ids()
        try:
            run = Propagator(library, list(route.converters)).run(
                PropagationShock(
                    shock_id=case.shock.id,
                    magnitude=case.shock.size,
                    target_input=route.target_input,
                ),
                state,
                route.propagation_path(library),
            )
        except MissingParameterError as exc:
            return [], [BlockedMechanism(mechanism_id=ids[0], parameter=exc.parameter)], []
        except Exception as exc:
            return [], [], [f"{ids[0]}: {type(exc).__name__}: {exc}"]
        steps = [
            _StepOut(
                mechanism_id=step.mechanism_id,
                outputs=dict(step.outputs),
                competing=list(step.competing_accounted),
                peak_q=library.get(step.mechanism_id).horizon.peak,
            )
            for step in run.steps
        ]
        return steps, [], []

    steps: list[_StepOut] = []
    blocked: list[BlockedMechanism] = []
    errors: list[str] = []
    for mechanism_id in ids:
        try:
            spec = library.get(mechanism_id)
        except KeyError:
            errors.append(f"{mechanism_id}: unknown mechanism")
            continue
        binding = case.replay.get(mechanism_id)
        target = binding.shock_input if binding is not None and binding.shock_input else spec.inputs[0].name
        overrides = todo_fixture_overrides(spec, fixtures.get(mechanism_id, {}))
        author_fed = binding is not None and binding.shock_value is not None
        path = _single_path(
            mechanism_id,
            spec.competing,
            overrides=overrides,
            fixed_inputs=_fixed_inputs(binding),
            origin_type=case.shock.origin.entity_type,
            origin_id=case.shock.origin.entity_id,
        )
        try:
            run = Propagator(library).run(
                PropagationShock(shock_id=case.shock.id, magnitude=_magnitude(case, binding), target_input=target),
                state,
                path,
            )
        except MissingParameterError as exc:
            blocked.append(BlockedMechanism(mechanism_id=mechanism_id, parameter=exc.parameter))
            continue
        except Exception as exc:
            errors.append(f"{mechanism_id}: {type(exc).__name__}: {exc}")
            continue
        for step in run.steps:
            steps.append(
                _StepOut(
                    mechanism_id=step.mechanism_id,
                    outputs=dict(step.outputs),
                    competing=list(step.competing_accounted),
                    peak_q=spec.horizon.peak,
                    author_fed=author_fed,
                    fixture_params=bool(overrides),
                )
            )
    return steps, blocked, errors


def _single_path(
    mechanism_id: str,
    competing: list[str],
    *,
    overrides: dict[str, Quantity],
    fixed_inputs: dict[str, Quantity],
    origin_type: str,
    origin_id: str,
) -> PropagationPath:
    """One mechanism with its competitors accepted so the path closes (spec §3.4 rule 3).

    The acceptance threshold is a closure token for replay, not an estimated economic magnitude.
    Provenance is ``case_replay``. Replay fixtures arrive as parameter overrides. Companion
    inputs are fixed levels from the case file.
    """
    token = Quantity(value=0.01, unit="share", provenance=_CLOSURE)
    return PropagationPath(
        steps=[
            MechanismInstance(
                mechanism_id=mechanism_id,
                parameter_overrides=overrides,
                fixed_inputs=fixed_inputs,
                bindings={"entity_type": origin_type, "entity_id": origin_id},
            )
        ],
        accepted_competing=[
            AcceptedCompeting(mechanism=name, observable="not on this case path", threshold=token)
            for name in competing
        ],
        materiality_threshold=Quantity(value=0.0, unit="share", provenance=_CLOSURE),
    )


def _propagate_composed(
    case: Case, library: Library, state, fixtures: ReplayParameterSet
) -> tuple[list[_StepOut], list[BlockedMechanism], list[str]]:
    """Replay the hint as one propagation (spec §3.4, §10.1).

    The path is the hinted mechanisms in order, and it must name at least two. Wires are only
    those listed on each replay step; no converter and no coefficient are invented, so a unit
    break is an error and the case is not scored. A parameter that is still ``TODO_estimate``
    blocks that mechanism, as in the one-step replay. The shock is the case shock on the first
    step's target input. A later step that sets ``shock_value`` is author-fed, so its outcomes
    are not evidential, and replay parameter overrides are fixture parameters, so those outcomes
    are not evidential either.
    """
    ids = list(case.mechanism_path_hint or [])
    if len(ids) < 2:
        return [], [], ["compose_path requires at least two mechanism ids"]
    specs: list[MechanismSpec] = []
    for mechanism_id in ids:
        try:
            specs.append(library.get(mechanism_id))
        except KeyError:
            return [], [], [f"{mechanism_id}: unknown mechanism"]
    path = _composed_path(case, ids, specs, fixtures)
    first = case.replay.get(ids[0])
    target = first.shock_input if first is not None and first.shock_input else specs[0].inputs[0].name
    try:
        run = Propagator(library).run(
            PropagationShock(shock_id=case.shock.id, magnitude=case.shock.size, target_input=target),
            state,
            path,
        )
    except MissingParameterError as exc:
        return [], _blocked_missing(ids, library, fixtures, exc), []
    except Exception as exc:
        return [], [], [f"{' -> '.join(ids)}: {type(exc).__name__}: {exc}"]
    author_fed = _author_fed_flags(case, ids)
    steps = [
        _StepOut(
            mechanism_id=step.mechanism_id,
            outputs=dict(step.outputs),
            competing=list(step.competing_accounted),
            peak_q=library.get(step.mechanism_id).horizon.peak,
            author_fed=author_fed.get(step.mechanism_id, False),
            fixture_params=bool(
                todo_fixture_overrides(library.get(step.mechanism_id), fixtures.get(step.mechanism_id, {}))
            ),
        )
        for step in run.steps
    ]
    return steps, [], []


def _composed_path(
    case: Case, ids: list[str], specs: list[MechanismSpec], fixtures: ReplayParameterSet
) -> PropagationPath:
    """Build one wired path for a composed case replay (spec §3.4, §10.1).

    Steps follow ``mechanism_path_hint``. Each step carries that mechanism's fixed inputs and
    replay-parameter overrides, bound to the shock's origin. A wire is copied from the replay
    step's ``consumes`` list and is never inferred. Competitors that are not later steps are
    accepted with the ``case_replay`` closure token, the same marker one-step replay uses; it
    closes the path and is not an estimated magnitude. No unit converter is attached.
    """
    token = Quantity(value=0.01, unit="share", provenance=_CLOSURE)
    accepted: list[AcceptedCompeting] = []
    seen: set[str] = set()
    for index, spec in enumerate(specs):
        later = set(ids[index + 1 :])
        for name in spec.competing:
            if name in later or name in seen:
                continue
            seen.add(name)
            accepted.append(
                AcceptedCompeting(mechanism=name, observable="not on this case path", threshold=token)
            )
    steps = []
    for mechanism_id, spec in zip(ids, specs, strict=True):
        binding = case.replay.get(mechanism_id)
        steps.append(
            MechanismInstance(
                mechanism_id=mechanism_id,
                parameter_overrides=todo_fixture_overrides(spec, fixtures.get(mechanism_id, {})),
                fixed_inputs=_fixed_inputs(binding),
                bindings={
                    "entity_type": case.shock.origin.entity_type,
                    "entity_id": case.shock.origin.entity_id,
                },
                consumes=list(binding.consumes) if binding is not None else [],
            )
        )
    return PropagationPath(
        steps=steps,
        accepted_competing=accepted,
        materiality_threshold=Quantity(value=0.0, unit="share", provenance=_CLOSURE),
    )


def todo_fixture_overrides(spec: MechanismSpec, bound: dict[str, Quantity]) -> dict[str, Quantity]:
    """Keep replay stand-ins only for parameters the library still marks ``TODO_estimate`` (spec §3.1, §10.1).

    A coefficient that has been estimated from the store is a library parameter. The replay
    fixture must not cover it, or the outcome would stay non-evidential after the estimate.
    """
    todo = {item.name for item in spec.parameters if item.source == "TODO_estimate"}
    return {name: quantity for name, quantity in bound.items() if name in todo}


def _author_fed_flags(case: Case, ids: list[str]) -> dict[str, bool]:
    """Which composed steps are author-fed (spec §10.1).

    The case shock is the only impulse, and it lands on the first step, so a ``shock_value`` on
    that step is not used. A later step that sets ``shock_value`` keeps the wired input and is
    marked author-fed, so its outcomes are not evidential.
    """
    flags: dict[str, bool] = {}
    for index, mechanism_id in enumerate(ids):
        binding = case.replay.get(mechanism_id)
        # [J] Later shock_value is a scoring flag, not a second impulse. Injecting it would
        # invent a magnitude the wire did not produce (spec §10.1).
        flags[mechanism_id] = index > 0 and binding is not None and binding.shock_value is not None
    return flags


def _blocked_missing(
    ids: list[str], library: Library, fixtures: ReplayParameterSet, raised: MissingParameterError
) -> list[BlockedMechanism]:
    """Name the hinted mechanisms that refuse ``raised`` (spec §3.1, §10.1).

    The propagator stops at the first missing estimate. Each hinted mechanism is checked the
    same way, and every one that refuses that parameter is blocked rather than scored as a miss.
    """
    blocked: list[BlockedMechanism] = []
    for mechanism_id in ids:
        try:
            bind_parameters(library.get(mechanism_id), dict(fixtures.get(mechanism_id, {})))
        except MissingParameterError as exc:
            if exc.parameter == raised.parameter:
                blocked.append(BlockedMechanism(mechanism_id=mechanism_id, parameter=exc.parameter))
    if not blocked:
        blocked.append(BlockedMechanism(mechanism_id=ids[0], parameter=raised.parameter))
    return blocked


def _score(
    case: Case,
    as_of: datetime,
    steps: list[_StepOut],
    blocked: list[BlockedMechanism],
    errors: list[str],
    report: RefreshReport,
) -> CaseResult:
    correct = 0
    scored = 0
    author_fed = 0
    fixture_params = 0
    evidential = 0
    evidential_correct = 0
    horizon_errors: list[float] = []
    dominant: list[bool] = []
    predicted: list[float] = []
    unmapped: list[str] = []
    seen: set[tuple[str, str, str, str | None, str | None, str, int]] = set()
    opened: set[str] = set()
    for point in case.observed_structure:
        # Two write-ups of one outcome are one fact. A second source with the same sign
        # does not add a second trial (spec §10.1 item 10).
        key = (
            point.entity.type,
            point.entity.id,
            point.variable,
            point.mechanism_id,
            point.output,
            point.polarity,
            point.sign,
        )
        match = _match_observed(point, case, steps)
        if match is None:
            if point.variable not in opened:
                unmapped.append(point.variable)
                opened.add(point.variable)
            continue
        if key in seen:
            continue
        seen.add(key)
        step, quantity = match
        scored += 1
        agrees = _agrees(quantity, point)
        if agrees:
            correct += 1
        if step.author_fed:
            author_fed += 1
        if step.fixture_params:
            fixture_params += 1
        if not step.author_fed and not step.fixture_params:
            evidential += 1
            if agrees:
                evidential_correct += 1
        midpoint = 0.5 * (point.horizon_q[0] + point.horizon_q[1])
        horizon_errors.append(abs(step.peak_q - midpoint))
        if point.dominant_competing is not None:
            listed = point.dominant_competing in step.competing or any(
                point.dominant_competing in other.competing for other in steps
            )
            dominant.append(listed)
        if quantity.value is not None:
            predicted.append(quantity.value)
    accuracy = (correct / scored) if scored else None
    # Observed rows carry signs, not magnitudes, so there is no second series to correlate.
    rank = _rank(predicted, case)
    horizon = sum(horizon_errors) / len(horizon_errors) if horizon_errors else None
    dominant_listed = None if not dominant else all(dominant)
    return CaseResult(
        case_id=case.case_id,
        as_of=as_of,
        held_out_group=case.held_out_group,
        blocked=blocked,
        errors=errors,
        n_blocked=len(blocked),
        n_scored=scored,
        n_sign_correct=correct,
        n_sign_wrong=scored - correct,
        sign_accuracy=accuracy,
        rank_correlation=rank,
        horizon_error=horizon,
        dominant_competing_listed=dominant_listed,
        refresh_skipped=len(report.skipped),
        refresh_written=len(report.written),
        unmapped=unmapped,
        n_unmapped=len(unmapped),
        n_author_fed=author_fed,
        n_fixture_params=fixture_params,
        n_evidential=evidential,
        n_evidential_correct=evidential_correct,
    )


def _rank(predicted: list[float], case: Case) -> float | None:
    """Spearman correlation of magnitudes. ``None`` means n/a (spec §10.1 item 2).

    Observed structure in this library has signs and sources, not numeric outcomes, so the
    correlation is n/a until a case carries an observed magnitude series.
    """
    del predicted, case
    return None


def _magnitude(case: Case, binding: ReplayStep | None) -> Quantity:
    """Shock quantity for one mechanism (spec §10.1). A case may retarget the sign-carrier."""
    if binding is not None and binding.shock_value is not None and binding.shock_unit is not None:
        return Quantity(value=binding.shock_value, unit=binding.shock_unit, provenance="case_fixture")
    return case.shock.size


def _fixed_inputs(binding: ReplayStep | None) -> dict[str, Quantity]:
    """Companion inputs declared on the case, labelled as fixtures (spec §10.1)."""
    if binding is None:
        return {}
    return {
        name: Quantity(value=level.value, unit=level.unit, provenance="case_fixture")
        for name, level in binding.inputs.items()
    }


def _agrees(quantity: Quantity, point: ObservedPoint) -> bool:
    """True when the propagated sign matches the observed sign under ``polarity`` (spec §10.1)."""
    sign = _sign(quantity)
    if point.polarity == "opposite":
        return sign == -point.sign
    return sign == point.sign


def _match_observed(
    point: ObservedPoint, case: Case, steps: list[_StepOut]
) -> tuple[_StepOut, Quantity] | None:
    """Find the step output this observed row names (spec §10.1 item 2)."""
    if point.mechanism_id and point.output:
        for step in steps:
            if step.mechanism_id != point.mechanism_id:
                continue
            quantity = step.outputs.get(point.output)
            if quantity is not None:
                return step, quantity
        return None
    return _find(point, case, steps)


def _find(point: StructurePoint, case: Case, steps: list[_StepOut]) -> tuple[_StepOut, Quantity] | None:
    origin = case.shock.origin
    if point.entity.type != origin.entity_type or point.entity.id != origin.entity_id:
        return None
    for step in steps:
        for name, quantity in step.outputs.items():
            if _names_match(point.variable, name):
                return step, quantity
    return None


def _names_match(expected: str, output: str) -> bool:
    if expected == output:
        return True
    return output == f"d_{expected}" or expected == output.removeprefix("d_")


def _sign(quantity: Quantity) -> int:
    if quantity.straddles_zero():
        return 0
    value = quantity.mid
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _read_fixture(path: Path) -> list[Observation]:
    frame = pd.read_csv(path)
    rows: list[Observation] = []
    for record in frame.to_dict(orient="records"):
        published = _stamp(record["published_at"])
        ingested = _stamp(record["ingested_at"])
        rows.append(
            Observation(
                series_id=str(record["series_id"]),
                ref_period_start=_day(record["ref_period_start"]),
                ref_period_end=_day(record["ref_period_end"]),
                value=float(record["value"]) if pd.notna(record["value"]) else None,
                unit=str(record["unit"]),
                published_at=published,
                ingested_at=ingested,
                vintage_id=str(record["vintage_id"]),
                source_url=str(record["source_url"]),
                source_hash=str(record["source_hash"]),
                quality_flag=str(record["quality_flag"]),  # type: ignore[arg-type]
            )
        )
    return rows


def _day(value: object) -> date:
    return date.fromisoformat(str(value)[:10])


def _stamp(value: object) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("fixture timestamps must be timezone-aware")
    return parsed
