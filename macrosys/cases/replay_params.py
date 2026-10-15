"""Replay fixtures for ``TODO_estimate`` parameters (spec §3.1, §10.1 item 2).

The library leaves locally estimated coefficients as ``TODO_estimate`` and the propagator refuses
to run without them. A historical case replay still needs the formula to emit a direction, so
``cases/fixtures/replay_parameters.yaml`` binds each such parameter either to a unit sign (exactly +1 or
-1, the direction the ``form`` already states) or to a placeholder level the formula needs to be
well defined. Neither is an estimate. Every bound quantity carries provenance
``case_fixture:replay_sign`` or ``case_fixture:replay_level`` so the runner can report any
outcome scored through one of them as non-evidential. The weekly decision job never loads this
file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml

from macrosys.core.quantity import Quantity
from macrosys.core.units import assert_unit
from macrosys.mechanisms.library import Library

ReplayParameterSet = dict[str, dict[str, Quantity]]
Role = Literal["sign", "level"]

PROVENANCE_PREFIX = "case_fixture:replay_"


def replay_parameters_path() -> Path:
    """Path to ``cases/fixtures/replay_parameters.yaml`` (spec §10.1 item 2)."""
    return Path(__file__).resolve().parents[2] / "cases" / "fixtures" / "replay_parameters.yaml"


def is_replay_fixture(quantity: Quantity) -> bool:
    """True when ``quantity`` was bound from the replay fixture file rather than the library."""
    return bool(quantity.provenance) and quantity.provenance.startswith(PROVENANCE_PREFIX)


def load_replay_parameters(path: str | Path | None = None) -> ReplayParameterSet:
    """Load replay fixtures keyed by mechanism id, then parameter name (spec §3.1, §10.1).

    A ``sign`` entry must be exactly ``1.0`` or ``-1.0``. A ``level`` entry must say ``why`` the
    placeholder value is admissible. Any other shape is rejected so a magnitude cannot enter the
    replay under a fixture label.
    """
    file_path = Path(path) if path is not None else replay_parameters_path()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("parameters"), dict):
        raise ValueError(f"{file_path} must contain a parameters mapping")
    found: ReplayParameterSet = {}
    for mechanism_id, params in loaded["parameters"].items():
        if not isinstance(mechanism_id, str) or not isinstance(params, dict):
            raise ValueError(f"{file_path} parameter entry {mechanism_id!r} is not a mapping")
        bound: dict[str, Quantity] = {}
        for name, raw in params.items():
            bound[str(name)] = _parse_entry(f"{mechanism_id}.{name}", raw)
        found[mechanism_id] = bound
    return found


def _parse_entry(label: str, raw: object) -> Quantity:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    allowed = {"role", "value", "unit", "why"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys {unknown}; a replay fixture is role, value, unit, why")
    role = raw.get("role")
    if role not in ("sign", "level"):
        raise ValueError(f"{label} role must be 'sign' or 'level'")
    unit = str(raw["unit"])
    assert_unit(unit)
    value = float(raw["value"])
    if role == "sign":
        if value not in (1.0, -1.0):
            raise ValueError(f"{label} is a sign fixture and must be exactly 1.0 or -1.0, got {value}")
        if raw.get("why") is not None:
            raise ValueError(f"{label} is a sign fixture; 'why' belongs on level fixtures")
    else:
        why = raw.get("why")
        if not isinstance(why, str) or not why.strip():
            raise ValueError(f"{label} is a level fixture and must state why the placeholder is admissible")
    return Quantity(value=value, unit=unit, provenance=f"{PROVENANCE_PREFIX}{role}")


def missing_replay_parameters(library: Library, fixtures: ReplayParameterSet) -> list[str]:
    """Names of ``TODO_estimate`` parameters the fixture file does not cover (spec §3.1, §10.1)."""
    missing: list[str] = []
    for mechanism_id in library.ids():
        spec = library.get(mechanism_id)
        covered = fixtures.get(mechanism_id, {})
        for param in spec.parameters:
            if param.source == "TODO_estimate" and param.name not in covered:
                missing.append(f"{mechanism_id}.{param.name}")
    return missing
