"""Interval arithmetic that keeps a point when the other side is only a band.

``Quantity.add`` drops a range when the other operand has no low/high. Premia marked
``TODO_estimate`` are bands with no point, and they must widen a market rate without
moving it (spec §3.3 C04, C09).
"""

from __future__ import annotations

from macrosys.core.quantity import Quantity
from macrosys.core.units import UnitError, compatible


def edges(q: Quantity) -> tuple[float, float]:
    """Closed interval, using the point for a missing edge."""
    if q.value is None and (q.low is None or q.high is None):
        raise ValueError("quantity has no edges")
    lo = q.low if q.low is not None else q.value
    hi = q.high if q.high is not None else q.value
    if lo is None or hi is None:
        raise ValueError("quantity has no edges")
    return float(lo), float(hi)


def _ranged(q: Quantity) -> bool:
    return q.low is not None or q.high is not None


def map_unit(q: Quantity, fn, unit: str, provenance: str | None = None) -> Quantity:
    """Apply ``fn`` to every edge and relabel the unit. ``fn`` is not a unit conversion."""

    def apply(x: float | None) -> float | None:
        return None if x is None else float(fn(x))

    return Quantity(
        value=apply(q.value),
        unit=unit,
        low=apply(q.low),
        high=apply(q.high),
        provenance=provenance or q.provenance,
    )


def to_share(q: Quantity) -> Quantity:
    """Read a rate as a decimal share. ``pct`` is divided by 100; ``share`` is unchanged."""
    if q.unit == "share":
        return q
    if q.unit == "pct":
        return map_unit(q, lambda x: x / 100.0, "share")
    raise UnitError(f"{q.unit} is not a share or a percent")


def to_pct(q: Quantity) -> Quantity:
    """Read a rate as percent. ``share`` is multiplied by 100; ``pct`` is unchanged."""
    if q.unit == "pct":
        return q
    if q.unit == "share":
        return map_unit(q, lambda x: x * 100.0, "pct")
    raise UnitError(f"{q.unit} is not a share or a percent")


def relabel(q: Quantity, unit: str, provenance: str | None = None) -> Quantity:
    """Keep the numbers and change the unit label (a percent-level difference is ``pp``)."""
    return Quantity(
        value=q.value,
        unit=unit,
        low=q.low,
        high=q.high,
        provenance=provenance or q.provenance,
    )


def _align(a: Quantity, b: Quantity) -> Quantity:
    if a.unit == b.unit:
        return b
    if not compatible(a.unit, b.unit):
        raise UnitError(f"cannot combine {a.unit!r} and {b.unit!r}")
    return b.to(a.unit)


def _finish(value: float | None, lo: float, hi: float, unit: str, provenance: str, ranged: bool) -> Quantity:
    if hi < lo:
        lo, hi = hi, lo
    if value is not None:
        if value < lo and lo - value < 1e-9:
            value = lo
        elif value > hi and value - hi < 1e-9:
            value = hi
        elif value < lo or value > hi:
            raise ValueError(f"point {value} falls outside [{lo}, {hi}]")
    if not ranged:
        return Quantity(value=value if value is not None else lo, unit=unit, provenance=provenance)
    return Quantity(value=value, unit=unit, low=lo, high=hi, provenance=provenance)


def add(a: Quantity, b: Quantity, *, provenance: str, unshifted: bool = False) -> Quantity:
    """Interval sum in ``a.unit``. A valueless ``b`` widens ``a`` and, if ``unshifted``, does not move it."""
    b = _align(a, b)
    a_lo, a_hi = edges(a)
    b_lo, b_hi = edges(b)
    lo, hi = a_lo + b_lo, a_hi + b_hi
    if unshifted and b.value is None and a.value is not None:
        value: float | None = float(a.value)
    elif a.value is None or b.value is None:
        value = None
    else:
        value = float(a.value) + float(b.value)
    return _finish(value, lo, hi, a.unit, provenance, _ranged(a) or _ranged(b))


def sub(a: Quantity, b: Quantity, *, provenance: str, unshifted: bool = False) -> Quantity:
    """Interval difference ``a − b``. See ``add`` for a valueless subtrahend."""
    return add(a, b.scale(-1.0), provenance=provenance, unshifted=unshifted)


def mul(a: Quantity, b: Quantity, *, unit: str, provenance: str) -> Quantity:
    """Endpoint-corner product, labeled ``unit`` (no unit algebra; the caller names the result)."""
    a_lo, a_hi = edges(a)
    b_lo, b_hi = edges(b)
    corners = (a_lo * b_lo, a_lo * b_hi, a_hi * b_lo, a_hi * b_hi)
    lo, hi = min(corners), max(corners)
    value = None if a.value is None or b.value is None else float(a.value) * float(b.value)
    return _finish(value, lo, hi, unit, provenance, _ranged(a) or _ranged(b))


def lerp(a: Quantity, b: Quantity, weight: float, *, provenance: str) -> Quantity:
    """Linear blend ``(1−w)·a + w·b`` on each edge. ``weight`` is a known fraction, not a range."""
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"interpolation weight {weight} is outside [0, 1]")
    b = _align(a, b)
    a_lo, a_hi = edges(a)
    b_lo, b_hi = edges(b)
    lo = (1.0 - weight) * a_lo + weight * b_lo
    hi = (1.0 - weight) * a_hi + weight * b_hi
    if a.value is None or b.value is None:
        value = None
    else:
        value = (1.0 - weight) * float(a.value) + weight * float(b.value)
    return _finish(value, lo, hi, a.unit, provenance, _ranged(a) or _ranged(b))


def provenance_code(code: str, *quantities: Quantity) -> str:
    """``code``, plus ``TODO_estimate`` when any input still carries that placeholder."""
    if any(q.provenance and "TODO_estimate" in q.provenance for q in quantities):
        return f"{code}|TODO_estimate"
    return code
