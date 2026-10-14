"""Stub world for the walking skeleton (spec §4.3, §9.2, §13 S0.1).

The numbers are labelled ``stub:S0.1``. They are not forecasts and not a vintage of any provider.
Observations carry the three timestamps and ``quality_flag='estimated'``. The same rows are
materialized into ``world_state`` so a point-in-time ``snapshot`` can read them.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from typing import Protocol

import duckdb
from pydantic import BaseModel, ConfigDict

from macrosys.core.contracts import EntityKey, Observation, SeriesMeta
from macrosys.core.quantity import Quantity
from macrosys.store.observations import ObservationStore
from macrosys.store.state import WorldStateStore

STUB_PROVENANCE = "stub:S0.1"

# The industry B03's applicability gate reads. Routing binds the same key.
UTIL_ENTITY = EntityKey(entity_type="industry", entity_id="USA:C26")


class Clock(Protocol):
    """Injectable source of ``ingested_at``. The default is the decision ``as_of``, not the wall clock."""

    def __call__(self) -> datetime: ...


class StubReading(BaseModel):
    """One stub series and the world-state row written from it (spec §4.3, §9.1)."""

    model_config = ConfigDict(frozen=True)

    series_id: str
    title: str
    entity: EntityKey
    state_var: str
    value: float
    unit: str
    geography: str | None = None


def stub_readings() -> tuple[StubReading, ...]:
    """The five stub series the skeleton world always contains (spec §4.3, §13 S0.1).

    Utilization, inventory days, a cost share, a policy rate, and an FX rate. Round numbers,
    provenance ``stub:S0.1``.
    """
    return (
        StubReading(
            series_id="STUB:UTILIZATION",
            title="Capacity utilization (stub)",
            entity=UTIL_ENTITY,
            state_var="capacity_utilization",
            value=0.80,
            unit="share",
            geography="USA",
        ),
        StubReading(
            series_id="STUB:INVENTORY_DAYS",
            title="Inventory days of cover (stub)",
            entity=EntityKey(entity_type="commodity", entity_id="Cu"),
            state_var="inventory_days",
            value=45.0,
            unit="d",
        ),
        StubReading(
            series_id="STUB:COST_SHARE",
            title="Material cost share (stub)",
            entity=UTIL_ENTITY,
            state_var="cost_share",
            value=0.18,
            unit="share",
            geography="USA",
        ),
        StubReading(
            series_id="STUB:POLICY_RATE",
            title="Policy rate (stub)",
            entity=EntityKey(entity_type="institution", entity_id="FED"),
            state_var="policy_rate",
            value=4.5,
            unit="pct",
            geography="USA",
        ),
        StubReading(
            series_id="STUB:FX_RATE",
            title="FX rate, local per USD (stub)",
            entity=EntityKey(entity_type="currency", entity_id="BRL"),
            state_var="fx_usd",
            value=5.40,
            unit="ratio",
            geography="BRA",
        ),
    )


def build_stub_world(
    conn: duckdb.DuckDBPyConnection,
    as_of: datetime,
    *,
    clock: Clock | None = None,
) -> tuple[StubReading, ...]:
    """Write the stub observations and world-state rows visible at ``as_of`` (spec §4.3, §9.2).

    Publication is one day before ``as_of`` so the rows are inside ``snapshot(as_of)``.
    ``ingested_at`` comes from ``clock`` when one is passed, otherwise from ``as_of`` itself.
    Both stamps are timezone-aware. Nothing in here reads the system clock.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    ingested_at = as_of if clock is None else clock()
    if ingested_at.tzinfo is None or ingested_at.utcoffset() is None:
        raise ValueError("clock() must return a timezone-aware datetime")
    published_at = as_of - timedelta(days=1)
    ref_end = published_at.date()
    ref_start = ref_end - timedelta(days=89)
    readings = stub_readings()
    store = ObservationStore(conn)
    store.upsert_series([_meta(row) for row in readings])
    store.append(
        [
            _observation(row, ref_start, ref_end, published_at, ingested_at)
            for row in readings
        ]
    )
    state = WorldStateStore(conn)
    for row in readings:
        state.write(
            as_of,
            row.entity,
            row.state_var,
            Quantity(value=row.value, unit=row.unit, provenance=row.series_id),
            row.series_id,
        )
    return readings


def _meta(row: StubReading) -> SeriesMeta:
    return SeriesMeta(
        series_id=row.series_id,
        source_id="stub",
        native_id=row.series_id.split(":", 1)[1],
        title=row.title,
        unit=row.unit,
        frequency="Q",
        geography=row.geography,
        concept=row.state_var,
        tier="research",
    )


def _observation(
    row: StubReading,
    ref_start: date,
    ref_end: date,
    published_at: datetime,
    ingested_at: datetime,
) -> Observation:
    payload = f"{STUB_PROVENANCE}|{row.series_id}|{row.value}|{row.unit}".encode()
    return Observation(
        series_id=row.series_id,
        ref_period_start=ref_start,
        ref_period_end=ref_end,
        value=row.value,
        unit=row.unit,
        published_at=published_at,
        ingested_at=ingested_at,
        vintage_id=STUB_PROVENANCE,
        source_url=STUB_PROVENANCE,
        source_hash=hashlib.sha256(payload).hexdigest(),
        quality_flag="estimated",
    )
