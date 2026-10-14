"""Path validation and the propagator (spec §3.4, §4.5)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.contracts import PathStepResult, StateView
from macrosys.core.quantity import Quantity
from macrosys.core.time import Horizon, compose_horizons
from macrosys.core.units import compatible
from macrosys.mechanisms.arrays import ArrayQuantity
from macrosys.mechanisms.errors import CompetingClosureError, IdentityViolationError, UnitContinuityError
from macrosys.mechanisms.eval import bind_parameters, evaluate
from macrosys.mechanisms.library import Library
from macrosys.mechanisms.registry import get_impl
from macrosys.mechanisms.spec import MechanismSpec

Value = Quantity | ArrayQuantity


class Wire(BaseModel):
    """Binds an earlier step's output to this step's input (spec §3.4 rule 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input: str
    from_step: int
    output: str


class MechanismInstance(BaseModel):
    """One bound mechanism on a path: id, entity bindings, and parameter overrides (spec §3.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism_id: str
    bindings: dict[str, str] = Field(default_factory=dict)
    parameter_overrides: dict[str, Quantity] = Field(default_factory=dict)
    fixed_inputs: dict[str, Quantity] = Field(default_factory=dict)
    consumes: list[Wire] = Field(default_factory=list)
    materiality_output: str | None = None


class AcceptedCompeting(BaseModel):
    """An explicitly accepted competing effect, with the observable that would falsify the acceptance (spec §3.4 rule 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism: str
    observable: str
    threshold: Quantity

    @model_validator(mode="after")
    def _observable(self) -> AcceptedCompeting:
        if not self.observable.strip():
            raise ValueError("accepted competing observable is empty")
        return self


class Loop(BaseModel):
    """A feedback loop: step indices, iteration cap, damping, and tolerance (spec §4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[int]
    max_iter: int
    damping: float
    tol: float
    feedback_output: str
    feedback_input: str

    @model_validator(mode="after")
    def _sane(self) -> Loop:
        if not self.steps:
            raise ValueError("loop has no steps")
        if len(set(self.steps)) != len(self.steps):
            raise ValueError("loop step indices must be unique")
        if self.max_iter < 1:
            raise ValueError("loop max_iter must be >= 1")
        if not 0.0 <= self.damping <= 1.0:
            raise ValueError("loop damping must be between 0 and 1")
        if self.tol < 0.0:
            raise ValueError("loop tol must be >= 0")
        return self


class UnitConverter(BaseModel):
    """Declared conversion between incompatible units. The coefficient carries provenance (spec §3.4 rule 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    from_unit: str
    to_unit: str
    coefficient: Quantity

    @model_validator(mode="after")
    def _provenance(self) -> UnitConverter:
        if not self.coefficient.provenance:
            raise ValueError("converter coefficient requires provenance")
        return self


class Shock(BaseModel):
    """The exogenous impulse applied to the first step (spec §4.1 shock, §3.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shock_id: str
    magnitude: Quantity
    target_input: str


class PropagationPath(BaseModel):
    """Ordered mechanism instances plus closure, materiality, and loops (spec §3.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[MechanismInstance] = Field(min_length=1)
    accepted_competing: list[AcceptedCompeting] = Field(default_factory=list)
    materiality_threshold: Quantity
    loops: list[Loop] = Field(default_factory=list)


class StoppedBranch(BaseModel):
    """A branch the materiality rule halted (spec §3.4 rule 4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_index: int
    mechanism_id: str
    effect: Quantity
    threshold: Quantity
    reason: str


class UnstableLoop(BaseModel):
    """A loop that did not converge. This is a result, not an exception (spec §3.4 rule 6, §4.5)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[int]
    iterations: int
    max_abs_delta: float
    note: str


class PathValidation(BaseModel):
    """What ``validate_path`` returns after the composition rules pass (spec §3.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon: Horizon


class PropagationRun(BaseModel):
    """One propagation. ``identity_checks`` stores residual magnitudes keyed by mechanism id (spec §3.4 rule 7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    library_version: str
    steps: list[PathStepResult]
    identity_checks: dict[str, float]
    stopped_branches: list[StoppedBranch]
    unstable_loops: list[UnstableLoop]


def _find_converter(converters: list[UnitConverter], from_unit: str, to_unit: str) -> UnitConverter | None:
    for conv in converters:
        if conv.from_unit == from_unit and conv.to_unit == to_unit:
            return conv
    return None


def _accounted(spec: MechanismSpec, path: PropagationPath, index: int) -> list[str]:
    later = {path.steps[j].mechanism_id for j in range(index + 1, len(path.steps))}
    accepted = {item.mechanism for item in path.accepted_competing}
    return [cid for cid in spec.competing if cid in later or cid in accepted]


def validate_path(path: PropagationPath, library: Library, converters: list[UnitConverter] | None = None) -> PathValidation:
    """Enforce unit continuity, horizon addition, competing-effects closure, and the materiality unit (spec §3.4).

    Rule 1: each consumed output is compatible with the receiving input, or a converter whose coefficient
    has provenance is declared. Consecutive steps must be wired, so a gap cannot skip the check.
    Rule 2: horizons compose through ``compose_horizons``. Rule 3: every competing id is a later step or
    listed in ``accepted_competing``. Rule 4: the materiality threshold is in ``share`` units. An earlier
    step does not close a competitor; the ticket requires a later step or an explicit acceptance.
    """
    converters = list(converters or [])
    if path.materiality_threshold.unit != "share":
        raise ValueError("materiality threshold must use unit 'share'")

    specs: list[MechanismSpec] = []
    for step in path.steps:
        try:
            specs.append(library.get(step.mechanism_id))
        except KeyError:
            raise ValueError(f"unknown mechanism {step.mechanism_id}") from None

    all_competing = {cid for spec in specs for cid in spec.competing}
    for item in path.accepted_competing:
        if item.mechanism not in all_competing:
            raise ValueError(f"accepted competing {item.mechanism} is not listed by any step")

    for index, step in enumerate(path.steps):
        spec = specs[index]
        if index > 0 and not any(wire.from_step == index - 1 for wire in step.consumes):
            raise UnitContinuityError(
                specs[index - 1].id, spec.id, specs[index - 1].outputs[0].unit, spec.inputs[0].unit
            )
        for wire in step.consumes:
            if wire.from_step < 0 or wire.from_step >= index:
                raise ValueError(f"{spec.id}: wire {wire.input} must come from an earlier step")
            src = specs[wire.from_step]
            try:
                from_unit = src.output_unit(wire.output)
            except KeyError:
                raise ValueError(f"{src.id} has no output {wire.output}") from None
            try:
                to_unit = spec.input_unit(wire.input)
            except KeyError:
                raise ValueError(f"{spec.id} has no input {wire.input}") from None
            if from_unit == to_unit or compatible(from_unit, to_unit):
                continue
            if _find_converter(converters, from_unit, to_unit) is None:
                raise UnitContinuityError(src.id, spec.id, from_unit, to_unit)
        if step.materiality_output is not None:
            try:
                unit = spec.output_unit(step.materiality_output)
            except KeyError:
                raise ValueError(f"{spec.id} has no materiality output {step.materiality_output}") from None
            if unit != "share":
                raise ValueError(f"{spec.id} materiality output {step.materiality_output} must be share, got {unit}")
        later = {path.steps[j].mechanism_id for j in range(index + 1, len(path.steps))}
        accepted = {item.mechanism for item in path.accepted_competing}
        missing = [cid for cid in spec.competing if cid not in later and cid not in accepted]
        if missing:
            raise CompetingClosureError(spec.id, missing)

    for loop in path.loops:
        for index in loop.steps:
            if index < 0 or index >= len(path.steps):
                raise ValueError(f"loop step {index} is outside the path")
        last = specs[loop.steps[-1]]
        first = specs[loop.steps[0]]
        try:
            from_unit = last.output_unit(loop.feedback_output)
        except KeyError:
            raise ValueError(f"loop feedback output {loop.feedback_output} is not on {last.id}") from None
        try:
            to_unit = first.input_unit(loop.feedback_input)
        except KeyError:
            raise ValueError(f"loop feedback input {loop.feedback_input} is not on {first.id}") from None
        if not (from_unit == to_unit or compatible(from_unit, to_unit)):
            raise UnitContinuityError(last.id, first.id, from_unit, to_unit)

    return PathValidation(horizon=compose_horizons([spec.horizon.to_core() for spec in specs]))


def _apply_converter(value: Quantity, conv: UnitConverter) -> Quantity:
    k = conv.coefficient.mid
    low, high = value.low, value.high
    if k < 0 and low is not None and high is not None:
        low, high = high, low

    def scale(x: float | None) -> float | None:
        return None if x is None else x * k

    return Quantity(
        value=scale(value.value),
        unit=conv.to_unit,
        low=scale(low),
        high=scale(high),
        provenance=conv.coefficient.provenance,
    )


def _transfer(value: Value, target_unit: str, converters: list[UnitConverter], src: str, dest: str) -> Value:
    if isinstance(value, ArrayQuantity):
        if value.unit != target_unit:
            raise UnitContinuityError(src, dest, value.unit, target_unit)
        return value
    if value.unit == target_unit:
        return value
    if compatible(value.unit, target_unit):
        return value.to(target_unit)
    conv = _find_converter(converters, value.unit, target_unit)
    if conv is None:
        raise UnitContinuityError(src, dest, value.unit, target_unit)
    return _apply_converter(value, conv)


def _flatten(values: dict[str, Value]) -> dict[str, Quantity]:
    """Expand arrays to labeled scalar quantities so the result fits ``PathStepResult`` (spec §8.5)."""
    flat: dict[str, Quantity] = {}
    for name, val in values.items():
        if isinstance(val, Quantity):
            flat[name] = val
            continue
        arr = val.values
        if arr.ndim == 1:
            for i, label in enumerate(val.rows):
                flat[f"{name}[{label}]"] = Quantity(value=float(arr[i]), unit=val.unit, provenance=name)
        else:
            assert val.cols is not None
            for r_i, row in enumerate(val.rows):
                for c_i, col in enumerate(val.cols):
                    flat[f"{name}[{row},{col}]"] = Quantity(
                        value=float(arr[r_i, c_i]), unit=val.unit, provenance=name
                    )
    return flat


def _share_effect(outputs: dict[str, Value], name: str | None) -> Quantity | None:
    if not name:
        return None
    val = outputs.get(name)
    if isinstance(val, Quantity) and val.unit == "share":
        return val
    return None


class Propagator:
    """Execute a validated path: gates, loops, materiality, then identity residuals (spec §3.4, §4.5)."""

    def __init__(self, library: Library, converters: list[UnitConverter] | None = None) -> None:
        self.library = library
        self.converters = list(converters or [])

    def run(self, shock: Shock, state: StateView, path: PropagationPath) -> PropagationRun:
        """Propagate ``shock`` along ``path`` and return the run record (spec §3.4 rules 1–7, §4.5).

        Steps run in order. A loop is iterated with damping ``x <- (1-damping)*x + damping*f(x)``;
        if it does not converge it is recorded on ``unstable_loops`` and the run continues. Outputs are
        scaled by the applicability gate. After the path, each identity is re-evaluated and a residual
        beyond tolerance fails the run. ``identity_checks`` holds those residual magnitudes.
        """
        validate_path(path, self.library, self.converters)
        n = len(path.steps)
        raw_inputs: list[dict[str, Value] | None] = [None] * n
        raw_outputs: list[dict[str, Value] | None] = [None] * n
        gates: list = [None] * n
        halted: set[int] = set()
        stopped: list[StoppedBranch] = []
        unstable: list[UnstableLoop] = []

        def assemble(index: int, overrides: dict[str, Quantity] | None) -> dict[str, Value]:
            inst = path.steps[index]
            spec = self.library.get(inst.mechanism_id)
            inputs: dict[str, Value] = dict(inst.fixed_inputs)
            if index == 0:
                inputs[shock.target_input] = shock.magnitude
            for wire in inst.consumes:
                src_out = raw_outputs[wire.from_step]
                if src_out is None or wire.output not in src_out:
                    raise UnitContinuityError(
                        path.steps[wire.from_step].mechanism_id, inst.mechanism_id, wire.output, wire.input
                    )
                src_spec = self.library.get(path.steps[wire.from_step].mechanism_id)
                inputs[wire.input] = _transfer(
                    src_out[wire.output],
                    spec.input_unit(wire.input),
                    self.converters,
                    src_spec.id,
                    spec.id,
                )
            if overrides:
                inputs.update(overrides)
            return inputs

        def execute(index: int, overrides: dict[str, Quantity] | None = None) -> None:
            inst = path.steps[index]
            if any(wire.from_step in halted for wire in inst.consumes):
                if index not in halted:
                    effect = path.materiality_threshold
                    for wire in inst.consumes:
                        if wire.from_step in halted and raw_outputs[wire.from_step] is not None:
                            found = _share_effect(
                                raw_outputs[wire.from_step],  # type: ignore[arg-type]
                                path.steps[wire.from_step].materiality_output,
                            )
                            if found is not None:
                                effect = found
                                break
                    halted.add(index)
                    stopped.append(
                        StoppedBranch(
                            step_index=index,
                            mechanism_id=inst.mechanism_id,
                            effect=effect,
                            threshold=path.materiality_threshold,
                            reason="upstream effect below materiality",
                        )
                    )
                return
            spec = self.library.get(inst.mechanism_id)
            impl = get_impl(spec.id)
            inputs = assemble(index, overrides)
            gate = impl.applicable(state, inst.bindings)
            produced = evaluate(spec, inputs, inst.parameter_overrides, impl=impl)
            scaled = {name: val.scale(gate.factor) for name, val in produced.items()}
            raw_inputs[index] = inputs
            raw_outputs[index] = scaled
            gates[index] = gate
            effect = _share_effect(scaled, inst.materiality_output)
            if effect is not None and abs(effect.mid) < path.materiality_threshold.mid and index not in halted:
                halted.add(index)
                stopped.append(
                    StoppedBranch(
                        step_index=index,
                        mechanism_id=inst.mechanism_id,
                        effect=effect,
                        threshold=path.materiality_threshold,
                        reason="effect below materiality",
                    )
                )

        covered: set[int] = set()
        for loop in path.loops:
            covered.update(loop.steps[1:])

        index = 0
        while index < n:
            if index in covered:
                index += 1
                continue
            loop = next((item for item in path.loops if item.steps[0] == index), None)
            if loop is not None:
                self._iterate(loop, execute, raw_inputs, raw_outputs, unstable)
                index += 1
                continue
            execute(index)
            index += 1

        identity_checks: dict[str, float] = {}
        for index, inst in enumerate(path.steps):
            if raw_inputs[index] is None:
                continue
            spec = self.library.get(inst.mechanism_id)
            if spec.class_ != "identity":
                continue
            params = bind_parameters(spec, inst.parameter_overrides)
            fresh = get_impl(spec.id).apply(raw_inputs[index], params)
            residual = float(fresh["residual"].mid)
            identity_checks[spec.id] = residual
            tolerance = float(params["tolerance"].mid)
            if abs(residual) > tolerance:
                raise IdentityViolationError(spec.id, residual, tolerance)

        results: list[PathStepResult] = []
        for index, inst in enumerate(path.steps):
            if raw_outputs[index] is None or raw_inputs[index] is None or gates[index] is None:
                continue
            results.append(
                PathStepResult(
                    mechanism_id=inst.mechanism_id,
                    inputs=_flatten(raw_inputs[index]),  # type: ignore[arg-type]
                    outputs=_flatten(raw_outputs[index]),  # type: ignore[arg-type]
                    gate=gates[index],
                    competing_accounted=_accounted(self.library.get(inst.mechanism_id), path, index),
                )
            )
        return PropagationRun(
            run_id=uuid.uuid4().hex,
            library_version=self.library.version,
            steps=results,
            identity_checks=identity_checks,
            stopped_branches=stopped,
            unstable_loops=unstable,
        )

    def _iterate(self, loop, execute, raw_inputs, raw_outputs, unstable: list[UnstableLoop]) -> None:
        """Damp a loop. Non-convergence is appended, not raised (spec §3.4 rule 6, §4.5)."""
        first = loop.steps[0]
        last = loop.steps[-1]
        feedback: Quantity | None = None
        max_delta = 0.0
        converged = False
        for _ in range(loop.max_iter):
            overrides = None if feedback is None else {loop.feedback_input: feedback}
            for step_index in loop.steps:
                execute(step_index, overrides if step_index == first else None)
            if raw_outputs[last] is None or loop.feedback_output not in raw_outputs[last]:
                raise ValueError(f"loop did not produce {loop.feedback_output}")
            y = raw_outputs[last][loop.feedback_output]
            if not isinstance(y, Quantity):
                raise TypeError("loop feedback must be a scalar Quantity")
            previous = feedback if feedback is not None else raw_inputs[first][loop.feedback_input]
            if not isinstance(previous, Quantity):
                raise TypeError("loop state must be a scalar Quantity")
            feedback = previous.scale(1.0 - loop.damping).add(y.scale(loop.damping), provenance="loop")
            delta = abs(feedback.mid - previous.mid)
            max_delta = max(max_delta, delta)
            if delta < loop.tol:
                converged = True
                break
        if not converged:
            unstable.append(
                UnstableLoop(
                    steps=list(loop.steps),
                    iterations=loop.max_iter,
                    max_abs_delta=max_delta,
                    note="unstable under current parameters",
                )
            )
