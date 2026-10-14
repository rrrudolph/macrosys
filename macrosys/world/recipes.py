"""Declarative world-state recipes (spec §4.3, §4.7).

A recipe names an entity, a state variable, the observation series it reads, and one transform
from a closed set. Parameters of the transform live in this file, not in code. A series id whose
prefix is not a connector in ``macrosys.sources`` is ``awaiting_source``: the recipe is still
valid, and refresh skips it until those observations exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from macrosys.core.contracts import QualityFlag
from macrosys.core.units import assert_unit
from macrosys.world.transforms import OUTPUT_UNITS, parse_transform

# Prefixes the observation connectors actually emit. FRED accepts any native id, so ``FRED:TCU``
# counts as a source that exists even when it is not listed in the example pipeline.
_SOURCE_PREFIXES = frozenset(
    {
        "FRED",
        "BLS",
        "ECB",
        "IMF",
        "OECD",
        "COMTRADE",
        "CFTC",
        "SEC",
        "TREASURY",
        "FEDGSW",
        "PORTWATCH",
        "TIINGO",
        "BEA",
        "CENSUS",
        "EIA",
        "NYFED",
        "WORLDBANK",
        "CBOE",
    }
)

_QUALITY: frozenset[str] = frozenset(
    {"ok", "estimated", "parser_changed", "imputed", "unofficial", "reconstructed_pubtime"}
)

EntityType = Literal[
    "economy",
    "industry",
    "commodity",
    "institution",
    "currency",
    "instrument",
    "portfolio",
]


class RecipeEntity(BaseModel):
    """Entity the recipe writes (spec §4.1). YAML uses ``type`` and ``id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EntityType
    id: str = Field(min_length=1)


class Recipe(BaseModel):
    """One world-state derivation (spec §4.3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    status: Literal["ready", "awaiting_source"]
    entity: RecipeEntity
    var: str = Field(min_length=1)
    unit: str
    inputs: list[str] = Field(min_length=1)
    transform: str
    min_quality: list[QualityFlag] = Field(min_length=1)
    max_staleness_days: int = Field(ge=0)
    note: str | None = None

    @field_validator("unit")
    @classmethod
    def _unit(cls, value: str) -> str:
        assert_unit(value)
        return value

    @field_validator("min_quality")
    @classmethod
    def _quality(cls, value: list[str]) -> list[str]:
        unknown = [flag for flag in value if flag not in _QUALITY]
        if unknown:
            raise ValueError(f"unknown quality_flag {unknown}")
        return value

    @model_validator(mode="after")
    def _transform_and_source(self) -> Recipe:
        parsed = parse_transform(self.transform, self.inputs)
        expected = OUTPUT_UNITS.get(parsed.op)
        if expected is not None and self.unit != expected:
            raise ValueError(f"{self.id}: transform {parsed.op} writes {expected}, recipe unit is {self.unit}")
        missing = [series for series in self.inputs if _prefix(series) not in _SOURCE_PREFIXES]
        if self.status == "ready" and missing:
            raise ValueError(f"{self.id}: status ready but no connector emits {missing}")
        if self.status == "awaiting_source" and not missing:
            raise ValueError(f"{self.id}: status awaiting_source but every input has a connector prefix")
        return self


def _prefix(series_id: str) -> str:
    return series_id.split(":", 1)[0]


def default_recipes_path() -> Path:
    """Path of ``config/world_recipes.yaml`` next to this repository (spec §4.3)."""
    return Path(__file__).resolve().parents[2] / "config" / "world_recipes.yaml"


def load_recipes(path: str | Path | None = None) -> list[Recipe]:
    """Load and validate world-state recipes (spec §4.3).

    The file is a mapping with a ``recipes`` list. Duplicate ids are an error. Each transform
    string is parsed against the closed set, and ``status`` has to agree with whether a connector
    can emit the input series ids.
    """
    file_path = Path(path) if path is not None else default_recipes_path()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("recipes"), list):
        raise ValueError(f"{file_path} must be a mapping with a recipes list")
    recipes = [Recipe.model_validate(item) for item in loaded["recipes"]]
    ids = [recipe.id for recipe in recipes]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate recipe id")
    return recipes
