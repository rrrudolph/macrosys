"""Quantity: a number with a unit, an optional range, and provenance (spec §4.7 typed objects).

Ranges are first-class because most mechanism parameters are ranges, not points (spec §3.1 `uncertainty`).
Arithmetic is deliberately minimal: add/sub require identical units; scaling by a dimensionless float is
allowed; anything else is an explicit mechanism step.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from macrosys.core.units import UnitError, assert_unit, compatible, convert


class Quantity(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: float | None
    unit: str
    low: float | None = None
    high: float | None = None
    provenance: str | None = None  # series_id, mechanism id, doc_id, or "TODO_estimate"

    @field_validator("unit")
    @classmethod
    def _unit_known(cls, v: str) -> str:
        assert_unit(v)
        return v

    @model_validator(mode="after")
    def _range_consistent(self) -> Self:
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"low {self.low} > high {self.high}")
        if self.value is not None:
            if self.low is not None and self.value < self.low:
                raise ValueError(f"value {self.value} < low {self.low}")
            if self.high is not None and self.value > self.high:
                raise ValueError(f"value {self.value} > high {self.high}")
        if self.value is None and (self.low is None or self.high is None):
            raise ValueError("a Quantity needs a value or a (low, high) range")
        return self

    # -- helpers -------------------------------------------------------------------------------------

    @property
    def is_range(self) -> bool:
        return self.low is not None and self.high is not None

    @property
    def mid(self) -> float:
        if self.value is not None:
            return self.value
        assert self.low is not None and self.high is not None
        return 0.5 * (self.low + self.high)

    def straddles_zero(self) -> bool:
        """True when the range includes zero — the thesis-gate condition in spec §11.3 (gap gate)."""
        lo = self.low if self.low is not None else self.value
        hi = self.high if self.high is not None else self.value
        assert lo is not None and hi is not None
        return lo <= 0.0 <= hi

    def to(self, unit: str) -> Quantity:
        """Scale-convert within the same dimension (see units.convert)."""
        conv = lambda x: None if x is None else convert(x, self.unit, unit)  # noqa: E731
        return Quantity(value=conv(self.value), unit=unit, low=conv(self.low), high=conv(self.high), provenance=self.provenance)

    def scale(self, k: float, provenance: str | None = None) -> Quantity:
        """Multiply by a dimensionless factor (e.g. a cost share). Ranges flip if k < 0."""
        lo, hi = self.low, self.high
        if k < 0 and lo is not None and hi is not None:
            lo, hi = hi, lo
        f = lambda x: None if x is None else x * k  # noqa: E731
        return Quantity(value=f(self.value), unit=self.unit, low=f(lo), high=f(hi), provenance=provenance or self.provenance)

    def mul(self, other: Quantity, unit: str, provenance: str | None = None) -> Quantity:
        """Product with an explicitly declared result unit (there is no unit algebra on purpose: the
        caller states what volume x price *is*, e.g. `USD_mn`, and the registry validates it). Ranges
        use interval arithmetic: the result range is the min/max over the endpoint products."""
        assert_unit(unit)
        a_lo = self.low if self.low is not None else self.value
        a_hi = self.high if self.high is not None else self.value
        b_lo = other.low if other.low is not None else other.value
        b_hi = other.high if other.high is not None else other.value
        assert a_lo is not None and a_hi is not None and b_lo is not None and b_hi is not None
        corners = (a_lo * b_lo, a_lo * b_hi, a_hi * b_lo, a_hi * b_hi)
        v = None if self.value is None or other.value is None else self.value * other.value
        lo, hi = min(corners), max(corners)
        if not (self.is_range or other.is_range):
            lo = hi = None
        return Quantity(value=v, unit=unit, low=lo, high=hi, provenance=provenance or self.provenance)

    def add(self, other: Quantity, provenance: str | None = None) -> Quantity:
        """Add two quantities in compatible units (result in self.unit). Ranges add end-to-end."""
        if not compatible(self.unit, other.unit):
            raise UnitError(f"cannot add {self.unit!r} and {other.unit!r}")
        o = other.to(self.unit) if other.unit != self.unit else other
        v = None if self.value is None or o.value is None else self.value + o.value
        lo = None if self.low is None or o.low is None else self.low + o.low
        hi = None if self.high is None or o.high is None else self.high + o.high
        if v is None and (lo is None or hi is None):
            # fall back to mid-points when only one side carries a range
            v = self.mid + o.mid
        return Quantity(value=v, unit=self.unit, low=lo, high=hi, provenance=provenance or self.provenance)
