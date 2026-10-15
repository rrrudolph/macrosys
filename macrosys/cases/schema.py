"""Historical case records (spec §10.1 item 2, §12).

A case is a frozen information set, a shock, and the structural outcome that followed: signs,
ordering, and which competing effect dominated. It does not store a measured magnitude. Fixture
observations are replay inputs. Synthetic rows are allowed only with ``quality_flag='estimated'``
and provenance ``case_fixture``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from macrosys.core.contracts import EntityKey, QualityFlag
from macrosys.core.units import assert_unit
from macrosys.decide.shock import Shock
from macrosys.mechanisms.compose import Wire
from macrosys.mechanisms.spec import MECHANISM_ID

Sign = Literal[-1, 0, 1]
Polarity = Literal["same", "opposite"]
EntityType = Literal[
    "economy",
    "industry",
    "commodity",
    "institution",
    "currency",
    "instrument",
    "portfolio",
]


def _aware(value: object, name: str) -> datetime:
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{name} is not an ISO datetime") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"{name} must be a datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _http_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"source_url {value!r} is not an http(s) URL")
    return value.strip()


class StructureEntity(BaseModel):
    """Entity on a case outcome row. YAML uses ``type`` and ``id`` (spec §4.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EntityType
    id: str = Field(min_length=1)

    def key(self) -> EntityKey:
        """The store key for this entity (spec §4.1)."""
        return EntityKey(entity_type=self.type, entity_id=self.id)


class StructurePoint(BaseModel):
    """Sign, horizon, and the competing effect that dominated, without a magnitude (spec §10.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: StructureEntity
    variable: str = Field(min_length=1)
    sign: Sign
    horizon_q: tuple[float, float]
    dominant_competing: str | None = None

    @model_validator(mode="after")
    def _horizon(self) -> Self:
        lo, hi = self.horizon_q
        if lo > hi:
            raise ValueError(f"horizon_q low {lo} > high {hi}")
        if lo < 0:
            raise ValueError("horizon_q cannot start before the shock")
        if self.dominant_competing is not None and not MECHANISM_ID.match(self.dominant_competing):
            raise ValueError(f"dominant_competing {self.dominant_competing!r} is not a mechanism id")
        return self


class FixtureLevel(BaseModel):
    """A companion input for one mechanism on a case replay (spec §10.1, §9.2).

    ``value`` is the fallback when no store is open. ``series_id`` names a series to read with
    ``snapshot(as_of)`` at the case cutoff. ``scale`` converts that reading into ``unit``
    (percent to share is ``0.01``). It is not an estimated magnitude.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    unit: str
    series_id: str | None = None
    scale: float = 1.0

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        assert_unit(value)
        return value

    @field_validator("scale")
    @classmethod
    def _scale(cls, value: float) -> float:
        if value == 0.0:
            raise ValueError("scale must be a non-zero unit conversion")
        return value


class ReplayStep(BaseModel):
    """How one hinted mechanism receives the case shock and its other inputs (spec §10.1 item 2).

    ``consumes`` holds optional wires ``{input, from_step, output}`` matching ``Wire`` (spec §3.4).
    An empty list connects nothing, and the runner does not infer a wire.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shock_input: str | None = None
    shock_value: float | None = None
    shock_unit: str | None = None
    inputs: dict[str, FixtureLevel] = Field(default_factory=dict)
    consumes: list[Wire] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shock(self) -> Self:
        if self.shock_value is not None and not self.shock_unit:
            raise ValueError("shock_unit is required when shock_value is set")
        if self.shock_unit is not None:
            assert_unit(self.shock_unit)
        return self


class ObservedPoint(StructurePoint):
    """What subsequently happened, with the public source of the sign (spec §10.1 item 2).

    ``mechanism_id`` and ``output`` name the propagated quantity this row is compared with.
    ``polarity`` is ``opposite`` when the mechanism reports a loss and the row reports the
    level that fell.
    """

    source_url: str
    source_note: str = Field(min_length=1)
    mechanism_id: str | None = None
    output: str | None = None
    polarity: Polarity = "same"

    @field_validator("source_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _http_url(value)

    @model_validator(mode="after")
    def _link(self) -> Self:
        if self.mechanism_id is not None and not MECHANISM_ID.match(self.mechanism_id):
            raise ValueError(f"mechanism_id {self.mechanism_id!r} is not a mechanism id")
        if self.polarity == "opposite" and not self.output:
            raise ValueError("polarity opposite requires output")
        return self


class StateRow(BaseModel):
    """A labelled placeholder world-state row for replay (spec §10.1). Not a published reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: StructureEntity
    var: str = Field(min_length=1)
    value: float
    unit: str
    quality_flag: QualityFlag = "estimated"
    provenance: str = "case_fixture"

    @model_validator(mode="after")
    def _labelled(self) -> Self:
        if self.provenance != "case_fixture" or self.quality_flag != "estimated":
            raise ValueError("case state rows are fixtures: quality_flag estimated and provenance case_fixture")
        return self


class Case(BaseModel):
    """One frozen historical case (spec §10.1 item 2, §12).

    ``compose_path`` runs ``mechanism_path_hint`` as one wired propagation (spec §3.4). The default
    is false, and that is how the historical files are scored: each hinted mechanism on its own path.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    shock: Shock
    information_cutoff: datetime
    held_out_group: str = Field(min_length=1)
    expected_structure: list[StructurePoint] = Field(min_length=1)
    observed_structure: list[ObservedPoint] = Field(min_length=1)
    mechanism_path_hint: list[str] | None = None
    compose_path: bool = False
    fixture_observations: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    state_rows: list[StateRow] = Field(default_factory=list)
    replay: dict[str, ReplayStep] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _cutoffs(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "information_cutoff" in data:
            data["information_cutoff"] = _aware(data["information_cutoff"], "information_cutoff")
        shock = data.get("shock")
        if isinstance(shock, dict) and "information_cutoff" in shock:
            shock = dict(shock)
            shock["information_cutoff"] = _aware(shock["information_cutoff"], "shock.information_cutoff")
            data["shock"] = shock
        return data

    @model_validator(mode="after")
    def _fixture_rules(self) -> Self:
        if self.shock.information_cutoff != self.information_cutoff:
            raise ValueError("shock.information_cutoff must equal the case information_cutoff")
        if self.shock.size.provenance != "case_fixture":
            raise ValueError("shock size is a replay sign-carrier and must have provenance case_fixture")
        if "case_fixture" not in self.notes:
            raise ValueError("notes must say the shock size and observation file are case_fixture, not measurements")
        if self.mechanism_path_hint is not None:
            for mechanism_id in self.mechanism_path_hint:
                if not MECHANISM_ID.match(mechanism_id):
                    raise ValueError(f"mechanism_path_hint {mechanism_id!r} is not a mechanism id")
        for mechanism_id in self.replay:
            if not MECHANISM_ID.match(mechanism_id):
                raise ValueError(f"replay key {mechanism_id!r} is not a mechanism id")
        return self


def load_case(path: str | Path) -> Case:
    """Load one case YAML file (spec §10.1 item 2)."""
    file_path = Path(path)
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{file_path} must be a mapping")
    case = Case.model_validate(loaded)
    fixture = Path(case.fixture_observations)
    if not fixture.is_absolute():
        fixture = file_path.parent / fixture
    if not fixture.is_file():
        raise ValueError(f"{file_path} fixture does not exist: {fixture}")
    return case


def load_cases(directory: str | Path) -> list[Case]:
    """Load every ``*.yaml`` case in ``directory``, in filename order (spec §10.1 item 2)."""
    root = Path(directory)
    files = sorted(root.glob("*.yaml"), key=lambda item: item.name)
    if not files:
        raise ValueError(f"no case yaml files in {root}")
    return [load_case(path) for path in files]


def default_cases_dir() -> Path:
    """Repository ``cases/`` directory (spec §10.1 item 2)."""
    return Path(__file__).resolve().parents[2] / "cases"
