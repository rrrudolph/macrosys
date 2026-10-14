"""Bind parameters and evaluate one mechanism, including range endpoints (spec §3.2, §10.1)."""

from __future__ import annotations

from itertools import product
from typing import Any

from macrosys.core.quantity import Quantity
from macrosys.core.units import UnitError
from macrosys.mechanisms.arrays import ArrayQuantity
from macrosys.mechanisms.errors import MissingParameterError
from macrosys.mechanisms.registry import get_impl
from macrosys.mechanisms.spec import MechanismSpec

Value = Quantity | ArrayQuantity


def bind_parameters(spec: MechanismSpec, supplied: dict[str, Quantity] | None = None) -> dict[str, Quantity]:
    """Resolve a mechanism's parameters for one run (spec §3.1).

    A library parameter whose source is ``TODO_estimate`` raises ``MissingParameterError`` unless the
    caller passes an explicit override with a real magnitude and a provenance other than
    ``TODO_estimate``. Overrides never invent a default.
    """
    supplied = dict(supplied or {})
    known = {p.name for p in spec.parameters}
    extra = sorted(set(supplied) - known)
    if extra:
        raise ValueError(f"{spec.id}: unknown parameters {extra}")
    bound: dict[str, Quantity] = {}
    for p in spec.parameters:
        if p.name in supplied:
            got = supplied[p.name]
            if got.unit != p.unit:
                raise UnitError(f"{spec.id} parameter {p.name}: expected unit {p.unit}, got {got.unit}")
            if p.source == "TODO_estimate" and (not got.provenance or got.provenance == "TODO_estimate"):
                raise MissingParameterError(p.name)
            if got.provenance == "TODO_estimate" or (got.value is None and not got.is_range):
                raise MissingParameterError(p.name)
            bound[p.name] = got
            continue
        if p.source == "TODO_estimate":
            raise MissingParameterError(p.name)
        bound[p.name] = Quantity(
            value=p.value, unit=p.unit, low=p.low, high=p.high, provenance=p.source
        )
    return bound


def _point(q: Quantity, x: float) -> Quantity:
    return Quantity(value=x, unit=q.unit, provenance=q.provenance)


def _is_open_range(q: object) -> bool:
    return isinstance(q, Quantity) and q.is_range and q.low != q.high


def propagate_endpoints(apply, inputs: dict[str, Value], params: dict[str, Quantity]) -> dict[str, Value]:
    """Evaluate a monotone form at every range endpoint and return output ranges (spec §3.2, §10.1).

    Identities do not come through here; they return a residual at the point (or midpoint). When no
    input or parameter is a non-degenerate range, ``apply`` is called once.
    """
    ranged: list[tuple[str, str, Quantity]] = []
    for name, val in inputs.items():
        if _is_open_range(val):
            ranged.append(("in", name, val))  # type: ignore[arg-type]
    for name, val in params.items():
        if _is_open_range(val):
            ranged.append(("par", name, val))
    if not ranged:
        return apply(inputs, params)

    ends: list[tuple[float, float]] = []
    for _, _, q in ranged:
        assert q.low is not None and q.high is not None
        ends.append((q.low, q.high))

    collected: dict[str, list[float]] = {}
    units: dict[str, str] = {}
    provenance: dict[str, str | None] = {}
    for combo in product(*ends):
        ins = dict(inputs)
        pars = dict(params)
        for (kind, name, q), x in zip(ranged, combo, strict=True):
            if kind == "in":
                ins[name] = _point(q, x)
            else:
                pars[name] = _point(q, x)
        out = apply(ins, pars)
        for key, val in out.items():
            if not isinstance(val, Quantity):
                raise TypeError(f"range propagation cannot envelope array output {key!r}")
            collected.setdefault(key, []).append(val.mid)
            units[key] = val.unit
            provenance[key] = val.provenance

    result: dict[str, Value] = {}
    for key, vals in collected.items():
        lo, hi = min(vals), max(vals)
        if lo == hi:
            result[key] = Quantity(value=lo, unit=units[key], provenance=provenance[key])
        else:
            result[key] = Quantity(value=None, unit=units[key], low=lo, high=hi, provenance=provenance[key])
    return result


def _assert_declared(spec: MechanismSpec, outputs: dict[str, Value]) -> None:
    declared = {port.name: port.unit for port in spec.outputs}
    for name, val in outputs.items():
        if name not in declared:
            raise ValueError(f"{spec.id}: undeclared output {name!r}")
        if val.unit != declared[name]:
            raise UnitError(f"{spec.id} output {name}: expected {declared[name]}, got {val.unit}")


def evaluate(
    spec: MechanismSpec,
    inputs: dict[str, Value],
    params: dict[str, Quantity] | None = None,
    impl: Any | None = None,
) -> dict[str, Value]:
    """Run one mechanism (spec §3.2).

    Identities return a residual at the supplied point. Empirical and pricing forms that carry a
    range are evaluated at the interval endpoints and returned as a range ``Quantity``. A parameter
    still marked ``TODO_estimate`` raises ``MissingParameterError``.
    """
    bound = bind_parameters(spec, params)
    fn = impl if impl is not None else get_impl(spec.id)
    if spec.class_ == "identity":
        outputs = fn.apply(inputs, bound)
    else:
        outputs = propagate_endpoints(fn.apply, inputs, bound)
    _assert_declared(spec, outputs)
    return outputs
