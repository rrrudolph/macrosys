"""Trigger rules: how a stored series becomes a filed shock (spec §4.1, §11.2; owner decision 2026-10-15).

A rule names one series, one signal computed from it, one threshold, and the ``Shock`` fields the
signal does not supply. Thresholds are design parameters [J] in ``config/triggers.yaml``; nothing here
is an estimate. The scan (``macrosys.triggers.scan``) evaluates rules point-in-time and the forward
week files what fires next to the owner's hand-written shocks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.contracts import EntityKey
from macrosys.core.units import assert_unit
from macrosys.decide.shock import INVESTMENT_STAGES, POLICY_STAGES, SUBTYPES, ShockFamily

SignalKind = Literal["drawdown", "change", "pct_change", "level"]
Comparison = Literal["<=", ">="]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeriesRef(_Strict):
    """A record-tier price (by universe ticker) or an observation series (by series id)."""

    kind: Literal["price", "observation"]
    instrument: str | None = None
    series_id: str | None = None

    @model_validator(mode="after")
    def _one_key(self) -> Self:
        if self.kind == "price" and (not self.instrument or self.series_id):
            raise ValueError("a price series names an instrument and no series_id")
        if self.kind == "observation" and (not self.series_id or self.instrument):
            raise ValueError("an observation series names a series_id and no instrument")
        return self

    @property
    def label(self) -> str:
        return self.instrument if self.kind == "price" else str(self.series_id)


class SignalSpec(_Strict):
    """What is computed from the series.

    ``drawdown``: percent below the trailing ``lookback_days`` high. ``change``: value minus the value
    ``lookback_days`` earlier, in the series' own difference unit. ``pct_change``: percent change over
    ``lookback_days``. ``level``: the value itself. Lookbacks are calendar days so a weekly scan reads
    the same window whatever the series frequency.
    """

    kind: SignalKind
    lookback_days: int | None = None

    @model_validator(mode="after")
    def _lookback(self) -> Self:
        needs = self.kind in ("drawdown", "change", "pct_change")
        if needs and (self.lookback_days is None or self.lookback_days < 1):
            raise ValueError(f"signal {self.kind} needs lookback_days >= 1")
        if not needs and self.lookback_days is not None:
            raise ValueError("a level signal has no lookback")
        return self


class ThresholdSpec(_Strict):
    """The crossing that fires the rule. ``unit`` is the shock size unit; it must fit the signal."""

    op: Comparison
    value: float
    unit: str

    @model_validator(mode="after")
    def _unit_known(self) -> Self:
        assert_unit(self.unit)
        return self

    def holds(self, value: float) -> bool:
        return value <= self.value if self.op == "<=" else value >= self.value


class ShockTemplate(_Strict):
    """The ``Shock`` fields a signal cannot supply (spec §4.1)."""

    type: ShockFamily
    subtype: str
    origin: EntityKey
    stage: str
    certainty: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _taxonomy(self) -> Self:
        if self.subtype not in SUBTYPES[self.type]:
            raise ValueError(f"subtype {self.subtype!r} is not in the {self.type} taxonomy")
        if self.stage not in INVESTMENT_STAGES | POLICY_STAGES:
            raise ValueError(f"stage {self.stage!r} is not on either realization ladder")
        return self


class TriggerRule(_Strict):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    series: SeriesRef
    signal: SignalSpec
    threshold: ThresholdSpec
    shock: ShockTemplate
    note: str | None = None

    @model_validator(mode="after")
    def _unit_fits_signal(self) -> Self:
        unit = assert_unit(self.threshold.unit)
        if self.signal.kind in ("drawdown", "pct_change") and self.threshold.unit != "pct":
            raise ValueError(f"{self.id}: a {self.signal.kind} signal is in pct")
        if self.signal.kind == "change" and unit.dimension == "rate_level":
            raise ValueError(f"{self.id}: a change in a rate is pp or bp, not pct")
        return self


class TriggerConfig(_Strict):
    """``fresh_days``: the crossing must be at most this old at the scan instant. ``rearm_days``:
    the condition must have been false for at least this long before the crossing, so a signal that
    stays past its threshold files once and a flicker does not file every week. ``stale_days``: a
    last observation older than this cannot fire."""

    fresh_days: int = Field(default=7, ge=1)
    rearm_days: int = Field(default=20, ge=1)
    stale_days: int = Field(default=7, ge=1)
    rules: list[TriggerRule]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [rule.id for rule in self.rules]
        if len(set(ids)) != len(ids):
            raise ValueError("trigger rule ids must be unique")
        return self


def load_triggers(path: str | Path) -> TriggerConfig:
    """Read ``config/triggers.yaml``. Unknown keys, unknown units, and bad taxonomy are rejected."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"trigger file {path} must contain a mapping")
    return TriggerConfig.model_validate(raw)
