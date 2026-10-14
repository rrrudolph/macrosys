"""Mandate file (spec §11.1, §15).

Limits live in the YAML, not in code. Nulls are valid on the example file; ``require_complete`` lists
every owner decision that is still null so the paper phase cannot start on a blank mandate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

# Fields spec §15 leaves to the owner. The example file ships them as null.
_PAPER_FIELDS: tuple[str, ...] = (
    "risk.vol_target_annual_pct",
    "risk.max_drawdown_halt_pct",
    "risk.position_limit_pct",
    "risk.factor_limit_pct",
    "risk.country_limit_pct",
    "risk.currency_net_limit_pct",
    "risk.scenario_loss_budget_pct",
    "gap_gate.min_expected_gap_after_costs_bp",
    "promotion_gates.research_to_paper.min_sign_accuracy",
)


class MandateIncompleteError(RuntimeError):
    """Raised by ``Mandate.require_complete`` when a paper-phase field is still null."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = list(missing)
        super().__init__(
            "mandate is incomplete; the paper phase cannot start (spec §15): " + ", ".join(self.missing)
        )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionClock(_Strict):
    weekday: str
    time_local: str
    tz: str


class HorizonMonths(_Strict):
    min: int
    max: int

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError("horizon_months.min cannot exceed max")
        return self


class Permission(_Strict):
    allowed: bool


class RiskLimits(_Strict):
    vol_target_annual_pct: float | None
    max_drawdown_halt_pct: float | None
    position_limit_pct: float | None
    factor_limit_pct: float | None
    country_limit_pct: float | None
    currency_net_limit_pct: float | None
    scenario_loss_budget_pct: float | None

    @model_validator(mode="after")
    def _non_negative(self) -> Self:
        for name in (
            "vol_target_annual_pct",
            "max_drawdown_halt_pct",
            "position_limit_pct",
            "factor_limit_pct",
            "country_limit_pct",
            "currency_net_limit_pct",
            "scenario_loss_budget_pct",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        return self


class GapGateConfig(_Strict):
    min_expected_gap_after_costs_bp: float | None

    @model_validator(mode="after")
    def _non_negative(self) -> Self:
        if self.min_expected_gap_after_costs_bp is not None and self.min_expected_gap_after_costs_bp < 0:
            raise ValueError("min_expected_gap_after_costs_bp cannot be negative")
        return self


class SizingConfig(_Strict):
    conviction_multiplier_bounds: tuple[float, float]
    fractional_kelly_ceiling: float

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        lo, hi = self.conviction_multiplier_bounds
        if not 0 < lo <= hi:
            raise ValueError("conviction_multiplier_bounds must satisfy 0 < low <= high")
        if not 0 < self.fractional_kelly_ceiling <= 1:
            raise ValueError("fractional_kelly_ceiling must be in (0, 1]")
        return self


class ResearchToPaper(_Strict):
    min_cases_scored: int
    min_sign_accuracy: float | None


class PaperToLiveSmall(_Strict):
    min_weeks: int
    max_class_a_incidents: int


class LiveSmallToScale(_Strict):
    min_weeks: int


class PromotionGates(_Strict):
    """Numeric promotion thresholds (spec §10.3). Nulls are fixed before paper trading, not at runtime."""

    research_to_paper: ResearchToPaper
    paper_to_live_small: PaperToLiveSmall
    live_small_to_scale: LiveSmallToScale


class Mandate(_Strict):
    """Versioned investment mandate (spec §11.1). Load from YAML; do not hard-code limits."""

    base_ccy: str
    universe_file: str
    decision: DecisionClock
    horizon_months: HorizonMonths
    leverage: Permission
    shorting: Permission
    excluded_structures: list[str]
    risk: RiskLimits
    gap_gate: GapGateConfig
    sizing: SizingConfig
    promotion_gates: PromotionGates

    @classmethod
    def load(cls, path: str | Path) -> Mandate:
        """Read a mandate YAML file. Unknown keys are rejected; nulls are preserved."""
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"mandate file {path} must contain a mapping")
        return cls.model_validate(raw)

    def require_complete(self) -> None:
        """Raise ``MandateIncompleteError`` listing every spec §15 field that is still null.

        The example mandate is intentionally incomplete. Call this before paper trading; do not
        invent the missing numbers in code.
        """
        missing = [path for path in _PAPER_FIELDS if _lookup(self, path) is None]
        if missing:
            raise MandateIncompleteError(missing)


def _lookup(mandate: Mandate, dotted: str) -> object:
    cursor: object = mandate
    for part in dotted.split("."):
        cursor = getattr(cursor, part)
    return cursor


def example_mandate_path() -> Path:
    """Path to ``config/mandate.example.yaml`` at the repository root."""
    return Path(__file__).resolve().parents[2] / "config" / "mandate.example.yaml"


def load_example_mandate() -> Mandate:
    """Load the checked-in example mandate. It still has nulls; ``require_complete`` will say which."""
    return Mandate.load(example_mandate_path())
