"""Time primitives: the three timestamps (spec §9.2) and mechanism horizons (spec §3.1, §3.4 rule 2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator


class Timestamps(BaseModel):
    """Reference period, publication time, ingestion time. All three are mandatory on observations."""

    model_config = ConfigDict(frozen=True)

    ref_period_start: date
    ref_period_end: date
    published_at: datetime
    ingested_at: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.ref_period_end < self.ref_period_start:
            raise ValueError("ref_period_end before ref_period_start")
        for name in ("published_at", "ingested_at"):
            ts = getattr(self, name)
            if ts.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.ingested_at < self.published_at:
            # Allowed only when published_at was reconstructed; callers must set quality_flag then.
            pass
        return self

    def visible_at(self, as_of: datetime) -> bool:
        """Point-in-time rule: an observation is usable iff it was published at or before `as_of`."""
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        return self.published_at <= as_of


class Horizon(BaseModel):
    """Impact, peak, decay in quarters (floats; 0.25 = ~1 month). `decay=None` means no decay (identities)."""

    model_config = ConfigDict(frozen=True)

    impact_q: float
    peak_q: float
    decay_q: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.peak_q < self.impact_q:
            raise ValueError("peak before impact")
        if self.decay_q is not None and self.decay_q < self.peak_q:
            raise ValueError("decay before peak")
        return self


def compose_horizons(steps: list[Horizon]) -> Horizon:
    """Path horizon per spec §3.4 rule 2: impact lags add; peak is the latest peak measured from the path
    start; decay is the latest decay (None if any step never decays)."""
    if not steps:
        raise ValueError("empty path")
    impact = 0.0
    peak = 0.0
    decay: float | None = 0.0
    offset = 0.0
    for h in steps:
        impact += h.impact_q
        peak = max(peak, offset + h.peak_q)
        if decay is not None:
            decay = None if h.decay_q is None else max(decay, offset + h.decay_q)
        offset += h.impact_q
    return Horizon(impact_q=impact, peak_q=max(peak, impact), decay_q=decay)
