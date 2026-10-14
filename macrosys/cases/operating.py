"""Operating sign ranges that let a case replay run (spec §3.3, §10.1).

The library leaves locally estimated coefficients as ``TODO_estimate``, and the weekly decision
does not read this file. Case scoring loads it so each formula can emit a sign. Every range
keeps the direction already written into the mechanism. The width is an operating assumption,
not a fitted magnitude and not a literature point.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from macrosys.core.quantity import Quantity
from macrosys.core.units import assert_unit
from macrosys.mechanisms.library import Library

OperatingSet = dict[str, dict[str, Quantity]]


def operating_parameters_path() -> Path:
    """Path to ``config/operating_parameters.yaml`` (spec §3.3, §10.1)."""
    return Path(__file__).resolve().parents[2] / "config" / "operating_parameters.yaml"


def load_operating_parameters(path: str | Path | None = None) -> OperatingSet:
    """Load sign ranges keyed by mechanism id, then parameter name (spec §3.3, §10.1).

    A range must not include both signs, and its provenance must name ``operating_sign``.
    That keeps a replay from treating the width as an estimate.
    """
    file_path = Path(path) if path is not None else operating_parameters_path()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("parameters"), dict):
        raise ValueError(f"{file_path} must contain a parameters mapping")
    found: OperatingSet = {}
    for mechanism_id, params in loaded["parameters"].items():
        if not isinstance(mechanism_id, str) or not isinstance(params, dict):
            raise ValueError(f"{file_path} parameter entry {mechanism_id!r} is not a mapping")
        bound: dict[str, Quantity] = {}
        for name, raw in params.items():
            if not isinstance(raw, dict):
                raise ValueError(f"{mechanism_id}.{name} must be a mapping")
            unit = str(raw["unit"])
            assert_unit(unit)
            source = str(raw["source"])
            if not source.startswith("operating_sign:"):
                raise ValueError(f"{mechanism_id}.{name} source must start with operating_sign:")
            quantity = Quantity(
                value=None,
                unit=unit,
                low=float(raw["low"]),
                high=float(raw["high"]),
                provenance=source,
            )
            if quantity.straddles_zero() and quantity.low != 0.0:
                raise ValueError(f"{mechanism_id}.{name} range crosses zero")
            bound[str(name)] = quantity
        found[mechanism_id] = bound
    return found


def missing_operating_parameters(library: Library, operating: OperatingSet) -> list[str]:
    """Names of ``TODO_estimate`` parameters this operating set does not cover (spec §3.1, §10.1)."""
    missing: list[str] = []
    for mechanism_id in library.ids():
        spec = library.get(mechanism_id)
        covered = operating.get(mechanism_id, {})
        for param in spec.parameters:
            if param.source == "TODO_estimate" and param.name not in covered:
                missing.append(f"{mechanism_id}.{param.name}")
    return missing
