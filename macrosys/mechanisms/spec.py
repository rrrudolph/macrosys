"""Pydantic record for one mechanism YAML file (spec §3.1).

The illustrative card in §3.1 writes horizons as ``0q`` / ``n/a`` and ``tests`` as file paths.
This schema uses numeric quarters (the same convention as ``macrosys.core.time.Horizon``) and a
``tests.example`` block, which is the fixture the counterfactual harness runs (spec §10.1 item 3).
"""

from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from macrosys.core.time import Horizon
from macrosys.core.units import assert_unit

MECHANISM_ID = re.compile(r"^[ABC]\d{2}_[a-z0-9_]+$")

ClassName = Literal["identity", "empirical", "pricing"]
UncertaintyType = Literal["parameter", "structural", "regime", "measurement"]
Confidence = Literal["M", "E", "H", "J"]


class Port(BaseModel):
    """One named input or output and its unit (spec §3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    unit: str
    by: list[str] | None = None
    desc: str | None = None

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        assert_unit(v)
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("port name is empty")
        return v


class ParameterSpec(BaseModel):
    """A named parameter. ``source`` is required; ``TODO_estimate`` forbids a magnitude (spec §3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    unit: str
    value: float | None = None
    low: float | None = None
    high: float | None = None
    source: str
    vintage: str | int | None = None
    stability: str | None = None

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        assert_unit(v)
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("parameter source is required")
        return v

    @model_validator(mode="after")
    def _magnitude(self) -> Self:
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError(f"{self.name}: low {self.low} > high {self.high}")
        if self.source == "TODO_estimate":
            if self.value is not None or self.low is not None or self.high is not None:
                raise ValueError(f"{self.name}: TODO_estimate requires null value, low, and high")
            return self
        if self.value is None and (self.low is None or self.high is None):
            raise ValueError(f"{self.name}: needs a value or a (low, high) range")
        if self.value is not None and self.low is not None and self.value < self.low:
            raise ValueError(f"{self.name}: value {self.value} < low {self.low}")
        if self.value is not None and self.high is not None and self.value > self.high:
            raise ValueError(f"{self.name}: value {self.value} > high {self.high}")
        return self


class HorizonSpec(BaseModel):
    """Impact, peak, and decay in quarters. ``decay`` is null when the effect does not die out (spec §3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    impact: float
    peak: float
    decay: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.peak < self.impact:
            raise ValueError("peak before impact")
        if self.decay is not None and self.decay < self.peak:
            raise ValueError("decay before peak")
        return self

    def to_core(self) -> Horizon:
        """Convert to the shared horizon type used by path composition (spec §3.4 rule 2)."""
        return Horizon(impact_q=self.impact, peak_q=self.peak, decay_q=self.decay)


class Uncertainty(BaseModel):
    """Kind of uncertainty so it can be propagated (spec §3.1, §8.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: UncertaintyType
    note: str

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("uncertainty note is empty")
        return v


class ExampleCase(BaseModel):
    """Numeric fixture: inputs, parameters, and the output the implementation must reproduce (spec §10.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inputs: dict[str, Any]
    params: dict[str, Any]
    expected: dict[str, Any]


class TestsBlock(BaseModel):
    """The example block consumed by the library tests (spec §10.1 items 3–4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    example: ExampleCase


class MechanismSpec(BaseModel):
    """One mechanism record, loaded from ``mechanisms/<id>.yaml`` (spec §3.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    id: str
    name: str
    class_: ClassName = Field(alias="class")
    scope: list[str]
    inputs: list[Port]
    outputs: list[Port]
    form: str
    parameters: list[ParameterSpec]
    horizon: HorizonSpec
    state_dependence: list[str]
    assumptions: list[str]
    competing: list[str]
    evidence: list[str] = Field(min_length=1)
    falsification: str = Field(min_length=1)
    uncertainty: Uncertainty
    confidence: Confidence
    references: list[str]
    tests: TestsBlock
    monotone: dict[str, Literal[1, -1]]

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not MECHANISM_ID.match(v):
            raise ValueError(f"mechanism id {v!r} does not match ^[ABC]\\d{{2}}_[a-z0-9_]+$")
        return v

    @field_validator("competing")
    @classmethod
    def _competing(cls, v: list[str]) -> list[str]:
        for cid in v:
            if not MECHANISM_ID.match(cid):
                raise ValueError(f"competing id {cid!r} is not a mechanism id")
        if len(set(v)) != len(v):
            raise ValueError("competing ids must be unique")
        return v

    @field_validator("name", "form", "falsification")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field is empty")
        return v

    @model_validator(mode="after")
    def _internal(self) -> Self:
        in_names = [p.name for p in self.inputs]
        out_names = [p.name for p in self.outputs]
        if len(in_names) != len(set(in_names)):
            raise ValueError(f"{self.id}: duplicate input names")
        if len(out_names) != len(set(out_names)):
            raise ValueError(f"{self.id}: duplicate output names")
        if not in_names or not out_names:
            raise ValueError(f"{self.id}: inputs and outputs are required")
        unknown = [n for n in self.monotone if n not in in_names]
        if unknown:
            raise ValueError(f"{self.id}: monotone keys {unknown} are not inputs")
        if self.id in self.competing:
            raise ValueError(f"{self.id}: a mechanism cannot compete with itself")
        param_names = [p.name for p in self.parameters]
        if len(param_names) != len(set(param_names)):
            raise ValueError(f"{self.id}: duplicate parameter names")
        return self

    @property
    def code(self) -> str:
        """Short id used by the implementation registry, e.g. ``B03`` from ``B03_bottleneck_pricing``."""
        return self.id.split("_", 1)[0]

    def input_unit(self, name: str) -> str:
        """Unit declared for input ``name`` (spec §3.4 rule 1)."""
        for port in self.inputs:
            if port.name == name:
                return port.unit
        raise KeyError(name)

    def output_unit(self, name: str) -> str:
        """Unit declared for output ``name`` (spec §3.4 rule 1)."""
        for port in self.outputs:
            if port.name == name:
                return port.unit
        raise KeyError(name)
