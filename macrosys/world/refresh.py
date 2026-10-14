"""Materialize world state from recipes at a point in time (spec §4.3, §9.2, §11.2).

``refresh_world_state(conn, as_of)`` is the ``weekly_job`` refresh hook: the pipeline calls it
with the connection and the decision instant, and ignores the report. Inputs are read through
``ObservationStore.snapshot(as_of)``. A missing, stale, or unacceptable-quality input is skipped
and reported; it is not written forward as if it were current.
"""

from __future__ import annotations

from datetime import datetime

import duckdb
from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.store.observations import ObservationStore
from macrosys.store.state import WorldStateStore
from macrosys.world.recipes import Recipe, load_recipes
from macrosys.world.transforms import TransformError, apply_transform, parse_transform, worst_quality


class SkippedRecipe(BaseModel):
    """A recipe that wrote nothing at this ``as_of`` (spec §4.3)."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str
    reason: str


class WrittenState(BaseModel):
    """One world-state row this refresh wrote (spec §4.7)."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str
    entity_type: str
    entity_id: str
    var: str
    value: float
    unit: str
    quality_flag: str
    derived_from: str


class RefreshReport(BaseModel):
    """Written rows and skipped recipes for one refresh (spec §4.3, §11.2)."""

    model_config = ConfigDict(frozen=True)

    as_of: datetime
    written: list[WrittenState] = Field(default_factory=list)
    skipped: list[SkippedRecipe] = Field(default_factory=list)


def refresh_world_state(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    recipes: list[Recipe] | None = None,
) -> RefreshReport:
    """Derive world state at ``as_of`` and write it (spec §4.3, §9.2, §11.2).

    ``recipes`` defaults to ``config/world_recipes.yaml``, so ``refresh_world_state(conn, as_of)``
    matches the ``weekly_job`` hook ``(conn, as_of)``. Every input is read with
    ``snapshot(as_of)``; a vintage published later is invisible. ``derived_from`` records the
    recipe id, the worst quality flag, and each input series id with the vintage that entered
    the transform. The world-state table has no quality column, so the flag is carried there
    and on this report.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    loaded = list(recipes) if recipes is not None else load_recipes()
    view = ObservationStore(conn).snapshot(as_of)
    store = WorldStateStore(conn)
    written: list[WrittenState] = []
    skipped: list[SkippedRecipe] = []
    for recipe in loaded:
        try:
            row = _one(recipe, view, as_of)
        except TransformError as exc:
            skipped.append(SkippedRecipe(recipe_id=recipe.id, reason=str(exc)))
            continue
        except Exception as exc:
            skipped.append(SkippedRecipe(recipe_id=recipe.id, reason=f"{type(exc).__name__}: {exc}"))
            continue
        entity = EntityKey(entity_type=recipe.entity.type, entity_id=recipe.entity.id)
        store.write(as_of, entity, recipe.var, row.quantity, row.derived_from)
        written.append(
            WrittenState(
                recipe_id=recipe.id,
                entity_type=recipe.entity.type,
                entity_id=recipe.entity.id,
                var=recipe.var,
                value=float(row.quantity.value),  # type: ignore[arg-type]
                unit=row.quantity.unit,
                quality_flag=row.quality_flag,
                derived_from=row.derived_from,
            )
        )
    return RefreshReport(as_of=as_of, written=written, skipped=skipped)


def weekly_refresh(conn: duckdb.DuckDBPyConnection, as_of: datetime) -> None:
    """``weekly_job`` hook with the declared ``None`` return (spec §9.4, §11.2).

    Loads the repository recipes and refreshes state. Callers that need the skip list use
    ``refresh_world_state`` instead. The pipeline ignores either return value.
    """
    refresh_world_state(conn, as_of)


class _Prepared(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    quantity: Quantity
    quality_flag: str
    derived_from: str


def _one(recipe: Recipe, view, as_of: datetime) -> _Prepared:
    parsed = parse_transform(recipe.transform, recipe.inputs)
    frames = {}
    for series_id in recipe.inputs:
        frame = view.get(series_id)
        if frame.empty:
            raise TransformError(f"missing input {series_id}")
        frames[series_id] = frame
        _staleness(recipe, series_id, frame, as_of)
        _quality_gate(recipe, series_id, frame)
    result = apply_transform(parsed, frames, recipe.unit)
    age = (as_of.date() - result.period_end).days
    if age > recipe.max_staleness_days:
        raise TransformError(f"stale result {recipe.id} age_days={age}")
    flag = worst_quality([result.quality_flag], imputed=result.imputed)
    if flag not in recipe.min_quality:
        raise TransformError(f"quality {recipe.id} flag={flag}")
    derived = _derived_from(recipe.id, flag, result.vintages)
    return _Prepared(quantity=result.quantity, quality_flag=flag, derived_from=derived)


def _staleness(recipe: Recipe, series_id: str, frame, as_of: datetime) -> None:
    usable = frame.dropna(subset=["value"])
    if usable.empty:
        raise TransformError(f"missing input {series_id}")
    ordered = usable.sort_values(["ref_period_end", "ref_period_start"], kind="mergesort")
    end = ordered.iloc[-1]["ref_period_end"]
    age = (as_of.date() - end).days
    if age > recipe.max_staleness_days:
        raise TransformError(f"stale input {series_id} age_days={age}")


def _quality_gate(recipe: Recipe, series_id: str, frame) -> None:
    usable = frame.dropna(subset=["value"])
    ordered = usable.sort_values(["ref_period_end", "ref_period_start"], kind="mergesort")
    flag = ordered.iloc[-1]["quality_flag"]
    text = "ok" if flag is None else str(flag)
    if text not in recipe.min_quality:
        raise TransformError(f"quality {series_id} flag={text}")


def _derived_from(recipe_id: str, quality: str, vintages: tuple[tuple[str, str], ...]) -> str:
    parts = [f"recipe:{recipe_id}", f"quality:{quality}"]
    parts.extend(f"{series_id}@{vintage}" for series_id, vintage in vintages)
    return " ".join(parts)
