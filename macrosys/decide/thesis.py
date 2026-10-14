"""Thesis object (spec §7.7).

The thesis is the only object that may become a trade. It keeps the propagated effect, what the market
already prices, the gap between them, the instrument expression, and the evidence and scenario fields that
must not be collapsed into one confidence number. Status changes go through ``transition``; the gap gate
and blocking evidence cannot be bypassed by setting ``status`` to approved or live.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible

EvidenceLabel = Literal["low", "medium", "high", "n/a"]
QualityLabel = EvidenceLabel


class IllegalThesisTransitionError(ValueError):
    """Raised when ``Thesis.transition`` is asked for a status the lifecycle does not allow."""


class ThesisStatus(StrEnum):
    """Lifecycle of a thesis (spec §7.7). Terminal states are ``exited`` and ``invalidated``."""

    proposed = "proposed"
    approved = "approved"
    live = "live"
    exited = "exited"
    invalidated = "invalidated"


# proposed → approved | invalidated; approved → live | exited | invalidated; live → exited | invalidated.
_TRANSITIONS: dict[ThesisStatus, frozenset[ThesisStatus]] = {
    ThesisStatus.proposed: frozenset({ThesisStatus.approved, ThesisStatus.invalidated}),
    ThesisStatus.approved: frozenset({ThesisStatus.live, ThesisStatus.exited, ThesisStatus.invalidated}),
    ThesisStatus.live: frozenset({ThesisStatus.exited, ThesisStatus.invalidated}),
    ThesisStatus.exited: frozenset(),
    ThesisStatus.invalidated: frozenset(),
}


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")
    return ts


def _with_value(node: Any) -> Any:
    """Allow the spec §7.7 shorthand ``{low, high, unit}`` by filling ``value: null``."""
    if isinstance(node, dict) and "value" not in node and "low" in node and "high" in node:
        return {**node, "value": None}
    return node


class ExpectedEffect(BaseModel):
    """Effect on the entity, taken from the propagation run (spec §7.7). Not the instrument return."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: EntityKey
    variable: str = Field(min_length=1)
    change: Quantity
    window_q: tuple[float, float]

    @model_validator(mode="before")
    @classmethod
    def _spec_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        entity = data.get("entity")
        if isinstance(entity, dict) and "type" in entity and "entity_type" not in entity:
            data["entity"] = {"entity_type": entity["type"], "entity_id": entity["id"]}
        if "change" in data:
            data["change"] = _with_value(data["change"])
        return data

    @model_validator(mode="after")
    def _window(self) -> Self:
        lo, hi = self.window_q
        if lo > hi:
            raise ValueError(f"window_q low {lo} > high {hi}")
        if lo < 0:
            raise ValueError("window_q cannot start before the shock")
        return self


class PricedIn(BaseModel):
    """What the market already implies for the same variable (spec §7.3, §7.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: list[str] = Field(min_length=1)
    implied_change: Quantity
    method_notes: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _qty(cls, data: Any) -> Any:
        if isinstance(data, dict) and "implied_change" in data:
            data = dict(data)
            data["implied_change"] = _with_value(data["implied_change"])
        return data


class CompetingEffect(BaseModel):
    """A competing mechanism the thesis accepts, with the observable that would show it winning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism: str = Field(min_length=1)
    observable: str = Field(min_length=1)
    threshold: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _threshold_text(cls, data: Any) -> Any:
        if isinstance(data, dict) and "threshold" in data and not isinstance(data["threshold"], str):
            data = dict(data)
            data["threshold"] = str(data["threshold"])
        return data


class Expression(BaseModel):
    """How the thesis is held (spec §7.4–§7.6). Exactly one of ``instrument`` or ``pair``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: str | None = None
    pair: tuple[str, str] | None = None
    direction: Literal["long", "short", "underweight"]
    exposure_vector_check: Literal["passed", "failed", "pending"]
    structure_notes: str = Field(min_length=1)
    cost_estimate_bp: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _one_vehicle(self) -> Self:
        has_instrument = self.instrument is not None
        has_pair = self.pair is not None
        if has_instrument == has_pair:
            raise ValueError("expression needs an instrument or a pair, and not both")
        if self.pair is not None and self.pair[0] == self.pair[1]:
            raise ValueError("pair legs must be different instruments")
        if self.direction == "short" and self.pair is None:
            raise ValueError("a short view is expressed as a pair or an underweight (spec §7.5)")
        return self


class SizingInputs(BaseModel):
    """Numbers the sizer reads (spec §11.4). Vol is an annualized fraction (0.16 = 16%), not a percent.

    ``hedge_ratio`` is optional and is not in the §7.7 sketch. When the expression is a pair, the
    constructor sizes the second leg at ``-hedge_ratio`` times the thesis weight (spec §11.5). Default 1.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    vol_annual: float = Field(gt=0.0)
    scenario_pnl: dict[str, float]
    factor_loadings: dict[str, float]
    hedge_ratio: float | None = Field(default=None, gt=0.0)


class InvalidationRule(BaseModel):
    """One falsifier (spec §7.7, §11.6). ``action`` is ``exit`` or ``downgrade`` in the spec sketch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observable: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    action: str = Field(min_length=1)


class Invalidation(BaseModel):
    """Observable falsifiers plus a time stop. Accepts the spec's mixed YAML list or this object."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: list[InvalidationRule] = Field(min_length=1)
    time_stop: date

    @model_validator(mode="before")
    @classmethod
    def _from_spec_list(cls, data: Any) -> Any:
        if not isinstance(data, list):
            return data
        rules: list[Any] = []
        time_stop: Any = None
        for item in data:
            if isinstance(item, dict) and "time_stop" in item and "observable" not in item:
                time_stop = item["time_stop"]
            else:
                rules.append(item)
        if time_stop is None:
            raise ValueError("invalidation needs a time_stop")
        return {"rules": rules, "time_stop": time_stop}


class EvidenceQuality(BaseModel):
    """Separate labels for observations, claims, and inferences (spec §7.7). Not one confidence number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: EvidenceLabel
    claims: EvidenceLabel
    inferences: EvidenceLabel


class Scenario(BaseModel):
    """A named outcome. ``weight`` is null when the scenario is explicitly unweighted (spec §7.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    outcome_range: Quantity

    @model_validator(mode="before")
    @classmethod
    def _qty(cls, data: Any) -> Any:
        if isinstance(data, dict) and "outcome_range" in data:
            data = dict(data)
            data["outcome_range"] = _with_value(data["outcome_range"])
        return data


class ForecastPrecision(BaseModel):
    """How sharp the forecast is (spec §7.7). ``interval_level`` is the claimed coverage, e.g. 0.8."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    range_width_pct: float = Field(ge=0.0)
    interval_level: float = Field(gt=0.0, le=1.0)


class ExpectedReturn(BaseModel):
    """Net-of-cost return range on the expression, and the holding horizon in months (spec §7.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    net_of_costs_range: Quantity
    horizon_m: float = Field(gt=0.0)

    @model_validator(mode="before")
    @classmethod
    def _qty(cls, data: Any) -> Any:
        if isinstance(data, dict) and "net_of_costs_range" in data:
            data = dict(data)
            data["net_of_costs_range"] = _with_value(data["net_of_costs_range"])
        return data


class Versions(BaseModel):
    """The run tuple pinned on the thesis (spec §4.4, §7.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library: str = Field(min_length=1)
    parameters: str = Field(min_length=1)
    code_commit: str = Field(min_length=1)
    model_ids: dict[str, str]


class MissingEvidence(BaseModel):
    """One gap in the information set. ``blocking`` items fail the mechanism gate (spec §11.3).

    A bare string, as in the §7.7 sketch, is accepted and treated as non-blocking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    blocking: bool

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"description": data, "blocking": False}
        return data


class Thesis(BaseModel):
    """The decision object (spec §7.7). Every field in the specification sketch is present.

    ``forecast_log_entry`` stays null until a ledger freeze assigns a content hash. Constructing a
    thesis already in ``approved`` or ``live`` is rejected when the gap straddles zero or any missing
    evidence is blocking; ``transition`` is the supported way to move status.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    thesis_id: str = Field(min_length=1)
    created: datetime
    shock_ref: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    mechanism_path: list[str] = Field(min_length=1)
    propagation_run: str = Field(min_length=1)
    expected_effect: ExpectedEffect
    priced_in: PricedIn
    gap: Quantity
    competing_effects_accepted: list[CompetingEffect]
    expression: Expression
    sizing_inputs: SizingInputs
    invalidation: Invalidation
    information_cutoff: datetime
    evidence_quality: EvidenceQuality
    scenarios: list[Scenario] = Field(min_length=1)
    forecast_precision: ForecastPrecision
    expected_return: ExpectedReturn
    no_trade_alternative: str = Field(min_length=1)
    versions: Versions
    missing_evidence: list[MissingEvidence]
    review_schedule: Literal["daily", "weekly", "on_release"]
    status: ThesisStatus = ThesisStatus.proposed
    forecast_log_entry: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _spec_quantities(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "gap" in data:
            data["gap"] = _with_value(data["gap"])
        return data

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _require_aware(self.created, "created")
        _require_aware(self.information_cutoff, "information_cutoff")
        if self.information_cutoff > self.created:
            raise ValueError("information_cutoff cannot be later than created")
        if any(not step for step in self.mechanism_path):
            raise ValueError("mechanism_path entries must be non-empty mechanism ids")
        if not compatible(self.gap.unit, self.expected_effect.change.unit):
            raise ValueError(
                f"gap unit {self.gap.unit!r} is incompatible with expected_effect.change unit "
                f"{self.expected_effect.change.unit!r}"
            )
        # The gap is expected minus priced, so the priced-in range has to share that unit too.
        if not compatible(self.priced_in.implied_change.unit, self.expected_effect.change.unit):
            raise ValueError(
                f"priced_in.implied_change unit {self.priced_in.implied_change.unit!r} is incompatible "
                f"with expected_effect.change unit {self.expected_effect.change.unit!r}"
            )
        names = [s.name for s in self.scenarios]
        if len(names) != len(set(names)):
            raise ValueError("scenario names must be unique")
        weights = [s.weight for s in self.scenarios]
        if all(w is not None for w in weights):
            total = float(sum(w for w in weights if w is not None))
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"scenario weights must sum to 1 when all are set (got {total})")
        if self.status in (ThesisStatus.approved, ThesisStatus.live):
            self._require_approvable()
        return self

    def _require_approvable(self) -> None:
        if self.gap.straddles_zero():
            raise ValueError("an approved thesis cannot have a gap that straddles zero (spec §7.7, §11.3)")
        blocking = [item.description for item in self.missing_evidence if item.blocking]
        if blocking:
            joined = "; ".join(blocking)
            raise ValueError(f"an approved thesis cannot carry blocking missing evidence: {joined}")

    def transition(self, to: ThesisStatus | str) -> Thesis:
        """Return a copy in status ``to``, or raise if that move is illegal (spec §7.7, §11.3).

        Approval and going live re-check the gap and blocking evidence. ``exited`` and ``invalidated``
        are terminal. A revision is a new thesis id, not a step backward.
        """
        dest = to if isinstance(to, ThesisStatus) else ThesisStatus(to)
        allowed = _TRANSITIONS[self.status]
        if dest not in allowed:
            raise IllegalThesisTransitionError(f"cannot transition {self.status.value} -> {dest.value}")
        # Rebuild through validation so the approval checks run on the new status.
        payload = self.model_dump()
        payload["status"] = dest
        return Thesis.model_validate(payload)
