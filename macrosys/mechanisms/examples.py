"""Parse the ``tests.example`` block of a mechanism record into quantities (spec §10.1 item 3)."""

from __future__ import annotations

from typing import Any

from macrosys.core.quantity import Quantity
from macrosys.mechanisms.arrays import ArrayQuantity
from macrosys.mechanisms.spec import MechanismSpec

Value = Quantity | ArrayQuantity


def parse_value(raw: Any) -> Value:
    """Build a ``Quantity`` or ``ArrayQuantity`` from one example mapping (spec §10.1)."""
    if not isinstance(raw, dict):
        raise TypeError(f"example value must be a mapping, got {type(raw).__name__}")
    if "values" in raw:
        cols = raw.get("cols")
        return ArrayQuantity(
            values=raw["values"],
            unit=raw["unit"],
            rows=tuple(raw["rows"]),
            cols=None if cols is None else tuple(cols),
        )
    return Quantity(
        value=raw.get("value"),
        unit=raw["unit"],
        low=raw.get("low"),
        high=raw.get("high"),
        provenance=raw.get("provenance", "example_scenario"),
    )


def parse_map(raw: dict[str, Any]) -> dict[str, Value]:
    """Parse a name → example-value mapping (spec §10.1)."""
    return {name: parse_value(val) for name, val in raw.items()}


def load_example(spec: MechanismSpec) -> tuple[dict[str, Value], dict[str, Quantity], dict[str, Value]]:
    """Return ``(inputs, params, expected)`` from ``spec.tests.example`` (spec §10.1 items 3–4)."""
    block = spec.tests.example
    inputs = parse_map(block.inputs)
    params_raw = parse_map(block.params)
    params: dict[str, Quantity] = {}
    for name, val in params_raw.items():
        if not isinstance(val, Quantity):
            raise TypeError(f"{spec.id} example param {name} must be a scalar Quantity")
        params[name] = val
    expected = parse_map(block.expected)
    return inputs, params, expected
