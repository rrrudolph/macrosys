"""Placeholder bands for premia the spec leaves as ``TODO_estimate`` (spec §3.1, §3.3).

The numbers live in ``params.yaml`` next to this file. They are declared widths, not
fitted premia. A band contains zero so the market point is not shifted.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from macrosys.core.quantity import Quantity

_PATH = Path(__file__).resolve().parent / "params.yaml"


@lru_cache(maxsize=1)
def load_params() -> dict:
    """Return the parsed placeholder file (spec §3.1: parameters live in data)."""
    raw = yaml.safe_load(_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{_PATH.name} is not a mapping")
    return raw


def band(name: str) -> Quantity:
    """One placeholder quantity. Missing keys raise; a silent zero is not substituted."""
    try:
        item = load_params()[name]
    except KeyError as exc:
        raise KeyError(f"pricing params have no {name}") from exc
    if not isinstance(item, dict):
        raise ValueError(f"pricing param {name} is not a mapping")
    return Quantity(
        value=item.get("value"),
        unit=str(item["unit"]),
        low=item.get("low"),
        high=item.get("high"),
        provenance=item.get("provenance"),
    )
