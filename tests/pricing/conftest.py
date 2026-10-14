"""In-memory store for pricing tests. No network."""

from __future__ import annotations

from datetime import UTC, date, datetime

import duckdb
import pytest

from macrosys.core.contracts import Observation
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema


@pytest.fixture
def store():
    connection = duckdb.connect(":memory:")
    create_schema(connection)
    try:
        yield ObservationStore(connection)
    finally:
        connection.close()


def obs(series_id: str, day: date, value: float, unit: str = "pct") -> Observation:
    """One print published at 18:00 UTC on ``day``."""
    stamp = datetime(day.year, day.month, day.day, 18, 0, tzinfo=UTC)
    return Observation(
        series_id=series_id,
        ref_period_start=day,
        ref_period_end=day,
        value=value,
        unit=unit,
        published_at=stamp,
        ingested_at=stamp,
        vintage_id="v1",
        source_url=f"https://example.test/{series_id}",
        source_hash="a" * 64,
        quality_flag="ok",
    )
