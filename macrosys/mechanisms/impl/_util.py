"""Shared helpers for mechanism implementations. Not a public API."""

from __future__ import annotations

from macrosys.core.contracts import EntityKey, Gate, StateView
from macrosys.core.quantity import Quantity
from macrosys.mechanisms.errors import MissingParameterError


def num(q: Quantity) -> float:
    """Point value, or the midpoint when the quantity is a range."""
    if q.value is not None:
        return float(q.value)
    return float(q.mid)


def q(value: float, unit: str, provenance: str) -> Quantity:
    return Quantity(value=value, unit=unit, provenance=provenance)


def require_param(params: dict[str, Quantity], name: str) -> Quantity:
    """Refuse to run when ``name`` is missing or still marked ``TODO_estimate`` (spec §3.1)."""
    got = params.get(name)
    if got is None or got.provenance == "TODO_estimate" or (got.value is None and not got.is_range):
        raise MissingParameterError(name)
    return got


class Ungated:
    """Default applicability: the mechanism is on. B03 overrides this (spec §3.4 rule 5)."""

    def applicable(
        self, state: StateView, bindings: dict[str, str], params: dict[str, Quantity] | None = None
    ) -> Gate:
        """No state gate. Spec §3.4 rule 5; identities are checks and are never switched off (§3.2)."""
        del state, bindings, params
        return Gate(status="on", factor=1.0, reason="no state gate")


def entity_from_bindings(bindings: dict[str, str]) -> EntityKey | None:
    if "entity_type" not in bindings or "entity_id" not in bindings:
        return None
    return EntityKey(entity_type=bindings["entity_type"], entity_id=bindings["entity_id"])  # type: ignore[arg-type]
