"""Series-id lookup for the pricing layer (spec §8.5, §9.6).

The table is ``series_map.yaml``. Connectors that do not yet write a series are named
there anyway, so a missing print becomes a named gap rather than a guessed id in code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parent / "series_map.yaml"


@lru_cache(maxsize=1)
def series_map() -> dict:
    """Parsed mapping of currencies, commodities, and instruments to series ids."""
    raw = yaml.safe_load(_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{_PATH.name} is not a mapping")
    return raw


def variable_kind(variable: str) -> str | None:
    """``policy``, ``inflation``, ``price``, or ``growth`` when ``variable`` is a direct market kind."""
    kinds = series_map().get("variables") or {}
    for kind, names in kinds.items():
        if variable in names:
            return str(kind)
    return None


def _by_entity(section: str, entity_id: str) -> dict | None:
    block = series_map().get(section) or {}
    for item in block.values():
        if isinstance(item, dict) and entity_id in (item.get("entity_ids") or []):
            return item
    return None


def policy_for_entity(entity_id: str) -> dict | None:
    """Policy-path block for an entity id, or None."""
    return _by_entity("policy", entity_id)


def policy_for_ccy(ccy: str) -> dict | None:
    """Policy-path block keyed by currency code."""
    item = (series_map().get("policy") or {}).get(ccy)
    return item if isinstance(item, dict) else None


def inflation_for_entity(entity_id: str) -> dict | None:
    """Breakeven block for an entity id, or None."""
    return _by_entity("breakeven", entity_id)


def inflation_for_ccy(ccy: str) -> dict | None:
    """Breakeven block keyed by currency code."""
    item = (series_map().get("breakeven") or {}).get(ccy)
    return item if isinstance(item, dict) else None


def commodity_for_entity(entity_id: str) -> dict | None:
    """Futures-curve block for a commodity id, or None."""
    return _by_entity("commodities", entity_id)


def commodity_for_name(name: str) -> dict | None:
    """Futures-curve block keyed by the commodity name in the map."""
    block = series_map().get("commodities") or {}
    item = block.get(name)
    if isinstance(item, dict):
        return item
    return commodity_for_entity(name)


def instrument_block(instrument_id: str) -> dict | None:
    """Earnings-yield mapping for one instrument, or None."""
    item = (series_map().get("instruments") or {}).get(instrument_id)
    return item if isinstance(item, dict) else None


def spread_for_entity(entity_id: str) -> dict | None:
    """Credit-spread block for an entity id, or None (spec §3.3 C06)."""
    return _by_entity("credit_spread", entity_id)


def named_gap(entity_type: str, entity_id: str, variable: str) -> str | None:
    """Declared gap text for one entity and variable, or None (spec §7.3, §9.8)."""
    for item in series_map().get("named_gaps") or []:
        if not isinstance(item, dict):
            continue
        if item.get("entity_type") != entity_type or item.get("entity_id") != entity_id:
            continue
        if item.get("variable") != variable:
            continue
        missing = item.get("missing")
        if not missing:
            return None
        return " ".join(str(missing).split())
    return None


def nearest_instrument(entity_id: str) -> dict | None:
    """Declared nearest listed instrument for an entity with no direct market quantity."""
    item = (series_map().get("nearest_instrument") or {}).get(entity_id)
    return item if isinstance(item, dict) else None


def nominal_block(ccy: str) -> dict | None:
    """Nominal curve series for a currency, or None."""
    item = (series_map().get("nominal_zero") or {}).get(ccy)
    return item if isinstance(item, dict) else None


def term_premium_series(model: str, tenor_y: float) -> str:
    """Series id for a published term premium at ``tenor_y`` years (spec §3.3 C03)."""
    block = (series_map().get("term_premium") or {}).get(model)
    if not isinstance(block, dict):
        raise KeyError(f"no term-premium mapping for {model}")
    tenor = int(round(tenor_y))
    if model == "KW":
        series = (block.get("series") or {}).get(str(tenor))
        if not series:
            known = ", ".join(sorted(block.get("series") or {})) or "none"
            raise KeyError(f"Kim–Wright mapping has no tenor {tenor}; mapped tenors: {known}")
        return str(series)
    pattern = block.get("pattern")
    if not pattern:
        raise KeyError(f"{model} mapping has no pattern")
    return str(pattern).format(tenor=tenor)
