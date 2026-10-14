"""Shock records (spec §4.1).

A shock is the exogenous change a propagation run starts from: a typed origin, a sized disturbance, and a
stage on the realization ladder. Mechanisms read the stage because an announcement is not spending and
spending is not capacity. This module stores the record; it does not propagate it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity

ShockFamily = Literal["demand", "supply", "price", "financial", "policy", "institutional"]

# Subtypes are the spec §4.1 taxonomy, slugged so they are stable identifiers.
SUBTYPES: dict[str, frozenset[str]] = {
    "demand": frozenset({"investment", "consumption", "government", "external"}),
    "supply": frozenset({"capacity_loss", "capacity_gain", "input_disruption", "productivity"}),
    "price": frozenset({"commodity", "fx", "rates"}),
    "financial": frozenset({"funding", "credit", "collateral", "flows"}),
    "policy": frozenset({"monetary", "fiscal", "trade", "regulatory"}),
    "institutional": frozenset({"sanctions", "contract_changes"}),
}

# Investment ladder: announced → committed → contracted → under construction → delivered → utilized.
INVESTMENT_STAGES: frozenset[str] = frozenset(
    {"announced", "committed", "contracted", "under_construction", "delivered", "utilized"}
)
# Policy ladder: proposed → enacted → implemented → enforced → observed.
POLICY_STAGES: frozenset[str] = frozenset({"proposed", "enacted", "implemented", "enforced", "observed"})


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return ts


class Shock(BaseModel):
    """One exogenous disturbance (spec §4.1).

    ``certainty`` is a degree of belief in the shock record itself, on [0, 1]. It is not a forecast
    probability and it is not an input to position sizing; scenario weights on the thesis carry that job.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    type: ShockFamily
    subtype: str = Field(min_length=1)
    origin: EntityKey
    size: Quantity
    start: date
    stage: str = Field(min_length=1)
    certainty: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(min_length=1)
    information_cutoff: datetime

    @model_validator(mode="after")
    def _taxonomy_and_stage(self) -> Self:
        _require_aware(self.information_cutoff, "information_cutoff")
        known = SUBTYPES[self.type]
        if self.subtype not in known:
            raise ValueError(f"subtype {self.subtype!r} is not in the {self.type} taxonomy {sorted(known)}")
        if self.stage not in INVESTMENT_STAGES | POLICY_STAGES:
            raise ValueError(
                f"stage {self.stage!r} is not on the investment ladder {sorted(INVESTMENT_STAGES)} "
                f"or the policy ladder {sorted(POLICY_STAGES)}"
            )
        # [J] The spec names a ladder for investment shocks and one for policy shocks. Other families
        # may sit on either ladder (a capacity addition is "announced"; a sanction is "enacted").
        if self.type == "policy" and self.stage not in POLICY_STAGES:
            raise ValueError("policy shocks use the policy realization ladder (spec §4.1)")
        if self.subtype == "investment" and self.stage not in INVESTMENT_STAGES:
            raise ValueError("investment shocks use the investment realization ladder (spec §4.1)")
        if any(not ref for ref in self.evidence_refs):
            raise ValueError("evidence_refs entries are document or claim ids and must be non-empty")
        return self
