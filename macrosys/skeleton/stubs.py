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

# B03's gate and the usa_capacity_utilization recipe both use this key (spec §4.3).
UTIL_ENTITY = EntityKey(entity_type="economy", entity_id="USA")

# Months of CME:HG:YYYY-MM settlements so a 0–2 quarter window sits on the curve.
_COPPER_MONTHS = 8
# 85 percent of capacity. The recipe divides by 100 and writes a 0.85 share.
_TCU_PCT = 85.0


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
    _seed_priced_in(store, ref_start, ref_end, published_at, ingested_at, as_of.date())
    _seed_utilization_input(store, ref_start, ref_end, published_at, ingested_at)
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


def _seed_priced_in(
    store: ObservationStore,
    ref_start: date,
    ref_end: date,
    published_at: datetime,
    ingested_at: datetime,
    as_of: date,
) -> None:
    """Copper futures the C07 resolver reads for entity Cu (spec §7.3, series_map.yaml).

    The quote's reference period is before ``as_of``. The contract month is only in the
    series id. Prints are estimated stubs, provenance ``stub:S0.1``.
    """
    months = _month_starts(as_of, _COPPER_MONTHS)
    rows = []
    meta = []
    for index, month in enumerate(months):
        series_id = f"CME:HG:{month:%Y-%m}"
        value = 100.0 + index
        meta.append(
            SeriesMeta(
                series_id=series_id,
                source_id="stub",
                native_id=series_id,
                title=f"COMEX copper {month:%Y-%m} (stub)",
                unit="USD",
                frequency="M",
                concept="futures_price",
                tier="research",
            )
        )
        rows.append(
            _observation_raw(
                series_id, value, "USD", ref_start, ref_end, published_at, ingested_at, "estimated"
            )
        )
    store.upsert_series(meta)
    store.append(rows)


def _seed_utilization_input(
    store: ObservationStore,
    ref_start: date,
    ref_end: date,
    published_at: datetime,
    ingested_at: datetime,
) -> None:
    """FRED:TCU so the capacity recipe can run (spec §4.3).

    The recipe accepts ``ok`` and ``reconstructed_pubtime`` only, so this one print is not
    ``estimated``. The value is still a stub. Provenance is ``stub:S0.1``.
    """
    series_id = "FRED:TCU"
    store.upsert_series(
        [
            SeriesMeta(
                series_id=series_id,
                source_id="stub",
                native_id="TCU",
                title="Capacity utilization percent (stub)",
                unit="pct",
                frequency="M",
                geography="USA",
                concept="capacity_utilization",
                tier="research",
            )
        ]
    )
    store.append(
        [
            _observation_raw(
                series_id,
                _TCU_PCT,
                "pct",
                ref_start,
                ref_end,
                published_at,
                ingested_at,
                "reconstructed_pubtime",
            )
        ]
    )


def _month_starts(origin: date, count: int) -> list[date]:
    """Month-start dates from the month that contains ``origin``. Matches the pricing resolver."""
    year, month = origin.year, origin.month
    out: list[date] = []
    for _ in range(count):
        out.append(date(year, month, 1))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return out


def _observation(
    row: StubReading,
    ref_start: date,
    ref_end: date,
    published_at: datetime,
    ingested_at: datetime,
) -> Observation:
    return _observation_raw(
        row.series_id,
        row.value,
        row.unit,
        ref_start,
        ref_end,
        published_at,
        ingested_at,
        "estimated",
    )


def _observation_raw(
    series_id: str,
    value: float,
    unit: str,
    ref_start: date,
    ref_end: date,
    published_at: datetime,
    ingested_at: datetime,
    quality_flag: str,
) -> Observation:
    payload = f"{STUB_PROVENANCE}|{series_id}|{value}|{unit}|{quality_flag}".encode()
    return Observation(
        series_id=series_id,
        ref_period_start=ref_start,
        ref_period_end=ref_end,
        value=value,
        unit=unit,
        published_at=published_at,
        ingested_at=ingested_at,
        vintage_id=STUB_PROVENANCE,
        source_url=STUB_PROVENANCE,
        source_hash=hashlib.sha256(payload).hexdigest(),
        quality_flag=quality_flag,  # type: ignore[arg-type]
    )
