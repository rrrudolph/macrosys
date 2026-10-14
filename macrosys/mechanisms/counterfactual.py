"""Sign tests: perturb one input and check the declared direction (spec §10.1 item 3)."""

from __future__ import annotations

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.arrays import ArrayQuantity
from macrosys.mechanisms.eval import bind_parameters, evaluate
from macrosys.mechanisms.spec import MechanismSpec

Value = Quantity | ArrayQuantity


def _perturb(value: Value, bump: float) -> Value:
    if isinstance(value, ArrayQuantity):
        return value.bump(bump)
    level = value.mid
    delta = bump if level == 0.0 else abs(level) * bump
    return Quantity(value=level + delta, unit=value.unit, provenance=value.provenance)


def _level(value: Value) -> float:
    if isinstance(value, ArrayQuantity):
        return value.total()
    return value.mid


def check_monotone(
    spec: MechanismSpec,
    impl,
    base_inputs: dict[str, Value],
    base_params: dict[str, Quantity],
    bump: float = 0.01,
) -> None:
    """Perturb each ``monotone`` input and check direction, units, and the base identity residual.

    Spec §10.1 item 3. The input is increased. ``+1`` requires a non-residual output to rise and
    forbids any non-residual output from falling; ``-1`` is the opposite. Output units must be
    unchanged. For an identity, the unperturbed residual must lie within ``tolerance``. The perturbed
    residual is not required to: the perturbation opens the identity on purpose, and the propagator
    is what fails a run when a residual is past tolerance (spec §3.4 rule 7).
    """
    bound = bind_parameters(spec, base_params)
    base = evaluate(spec, base_inputs, bound, impl=impl)
    for name, val in base.items():
        if val.unit != spec.output_unit(name):
            raise AssertionError(f"{spec.id} output {name} unit {val.unit}")
    if spec.class_ == "identity":
        residual = abs(base["residual"].mid)
        tolerance = bound["tolerance"].mid
        if residual > tolerance:
            raise AssertionError(f"{spec.id} base residual {residual} exceeds tolerance {tolerance}")

    for name, sign in spec.monotone.items():
        if name not in base_inputs:
            raise AssertionError(f"{spec.id}: monotone input {name} missing from the example")
        bumped = dict(base_inputs)
        bumped[name] = _perturb(base_inputs[name], bump)
        alt = evaluate(spec, bumped, bound, impl=impl)
        moved = False
        for key, before in base.items():
            after = alt[key]
            if after.unit != before.unit:
                raise AssertionError(f"{spec.id} output {key} changed unit under a bump of {name}")
            if key == "residual" or key.startswith("residual"):
                continue
            delta = _level(after) - _level(before)
            if abs(delta) <= 1e-12:
                continue
            if (delta > 0.0) != (sign > 0):
                raise AssertionError(
                    f"{spec.id}: bumping {name} moved {key} by {delta}, monotone sign is {sign}"
                )
            moved = True
        if not moved:
            raise AssertionError(f"{spec.id}: bumping {name} did not move any non-residual output")
