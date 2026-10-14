"""Shared thesis and shock builders for decision tests. No network."""

from __future__ import annotations

from datetime import UTC, datetime

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.decide.shock import Shock
from macrosys.decide.thesis import Thesis

CREATED = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)


def qty(low: float, high: float, unit: str = "bp") -> Quantity:
    return Quantity(value=None, unit=unit, low=low, high=high, provenance="test")


def make_shock(**overrides: object) -> Shock:
    payload: dict[str, object] = {
        "id": "S-2026-0119",
        "type": "demand",
        "subtype": "investment",
        "origin": EntityKey(entity_type="economy", entity_id="USA"),
        "size": Quantity(value=12.0, unit="USD_bn", low=8.0, high=15.0, provenance="test"),
        "start": CREATED.date(),
        "stage": "contracted",
        "certainty": 0.6,
        "evidence_refs": ["doc-capex-guidance"],
        "information_cutoff": CUTOFF,
    }
    payload.update(overrides)
    return Shock(**payload)  # type: ignore[arg-type]


def make_thesis(**overrides: object) -> Thesis:
    payload: dict[str, object] = {
        "thesis_id": "T-2026-0412",
        "created": CREATED,
        "shock_ref": "S-2026-0119",
        "claim": "Copper price rises 5 to 15 percent within six quarters on contracted data-center demand.",
        "mechanism_path": ["B01", "B03", "B05"],
        "propagation_run": "R-2026-100",
        "expected_effect": {
            "entity": {"type": "commodity", "id": "Cu"},
            "variable": "price",
            "change": {"unit": "bp", "low": 40.0, "high": 90.0},
            "window_q": [2, 6],
        },
        "priced_in": {
            "source": ["C07 curve"],
            "implied_change": {"unit": "bp", "low": 10.0, "high": 20.0},
            "method_notes": "deferred futures curve versus spot",
        },
        "gap": {"unit": "bp", "low": 40.0, "high": 90.0},
        "competing_effects_accepted": [
            {
                "mechanism": "B07",
                "observable": "scrap substitution share",
                "threshold": "share rises above 0.15",
            }
        ],
        "expression": {
            "instrument": "CPER",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "futures-based copper ETF",
            "cost_estimate_bp": 10.0,
        },
        "sizing_inputs": {
            "vol_annual": 0.18,
            "scenario_pnl": {"stress": -0.08},
            "factor_loadings": {"commodity": 0.7},
        },
        "invalidation": [
            {"observable": "exchange stocks", "condition": "rise > 8% over 8 weeks", "action": "exit"},
            {"time_stop": "2027-04-14"},
        ],
        "information_cutoff": CUTOFF,
        "evidence_quality": {"observations": "medium", "claims": "low", "inferences": "n/a"},
        "scenarios": [
            {"name": "base", "weight": None, "outcome_range": {"unit": "bp", "low": 40.0, "high": 90.0}},
            {
                "name": "premise_fails",
                "weight": None,
                "outcome_range": {"unit": "bp", "low": -30.0, "high": 0.0},
            },
        ],
        "forecast_precision": {"range_width_pct": 50.0, "interval_level": 0.8},
        "expected_return": {
            "net_of_costs_range": {"unit": "pct", "low": 4.0, "high": 12.0},
            "horizon_m": 9,
        },
        "no_trade_alternative": "hold current exposure; revisit on the next inventory print",
        "versions": {
            "library": "mech-0",
            "parameters": "params-0",
            "code_commit": "abc123",
            "model_ids": {},
        },
        "missing_evidence": [],
        "review_schedule": "weekly",
        "status": "proposed",
        "forecast_log_entry": None,
    }
    payload.update(overrides)
    return Thesis.model_validate(payload)
