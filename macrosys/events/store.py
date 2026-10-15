"""Persisted surprise events (spec §4.7 three timestamps, §10.1 item 11).

The table is a derived view of the observation store and is rebuilt per (series, forecaster).
It exists so the weekly job and the thesis layer can read "the latest surprise on series X"
through one query, and so a study run is reproducible from a stored event set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pyarrow as pa

from macrosys.core.quantity import Quantity
from macrosys.events.schema import SurpriseEvent
from macrosys.store.schema import ensure_utc

_DDL = """
CREATE TABLE IF NOT EXISTS release_surprise (
    series_id TEXT NOT NULL,
    ref_period_start DATE NOT NULL,
    forecaster TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    event_date DATE NOT NULL,
    publication_quality TEXT NOT NULL,
    actual DOUBLE NOT NULL,
    previous DOUBLE NOT NULL,
    level_unit TEXT NOT NULL,
    actual_change DOUBLE NOT NULL,
    forecast_change DOUBLE NOT NULL,
    surprise DOUBLE NOT NULL,
    change_unit TEXT NOT NULL,
    surprise_sigma DOUBLE,
    n_history INTEGER NOT NULL,
    built_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (series_id, ref_period_start, forecaster)
)
"""


def create_events_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the ``release_surprise`` table if it does not exist."""
    ensure_utc(conn)
    conn.execute(_DDL)


class SurpriseStore:
    """Replace and read surprise events for one (series, forecaster). Reads work on a read-only connection."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def replace(self, series_id: str, forecaster: str, events: list[SurpriseEvent], built_at: datetime | None = None) -> int:
        """Delete the stored set for ``(series_id, forecaster)`` and insert ``events``. Returns the row count."""
        create_events_schema(self._conn)
        stamp = built_at if built_at is not None else datetime.now(UTC)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("built_at must be timezone-aware")
        for event in events:
            if event.series_id != series_id or event.forecaster != forecaster:
                raise ValueError("event does not belong to the (series_id, forecaster) being replaced")
        self._conn.execute("DELETE FROM release_surprise WHERE series_id = ? AND forecaster = ?", [series_id, forecaster])
        if not events:
            return 0
        table = pa.table(
            {
                "series_id": pa.array([e.series_id for e in events], type=pa.string()),
                "ref_period_start": pa.array([e.ref_period_start for e in events], type=pa.date32()),
                "forecaster": pa.array([e.forecaster for e in events], type=pa.string()),
                "published_at": pa.array([e.published_at for e in events], type=pa.timestamp("us", tz="UTC")),
                "event_date": pa.array([e.event_date for e in events], type=pa.date32()),
                "publication_quality": pa.array([e.publication_quality for e in events], type=pa.string()),
                "actual": pa.array([e.actual.value for e in events], type=pa.float64()),
                "previous": pa.array([e.previous.value for e in events], type=pa.float64()),
                "level_unit": pa.array([e.actual.unit for e in events], type=pa.string()),
                "actual_change": pa.array([e.actual_change.value for e in events], type=pa.float64()),
                "forecast_change": pa.array([e.forecast_change.value for e in events], type=pa.float64()),
                "surprise": pa.array([e.surprise.value for e in events], type=pa.float64()),
                "change_unit": pa.array([e.surprise.unit for e in events], type=pa.string()),
                "surprise_sigma": pa.array([e.surprise_sigma for e in events], type=pa.float64()),
                "n_history": pa.array([e.n_history for e in events], type=pa.int32()),
                "built_at": pa.array([stamp] * len(events), type=pa.timestamp("us", tz="UTC")),
            }
        )
        self._conn.register("surprise_incoming", table)
        try:
            self._conn.execute("INSERT INTO release_surprise SELECT * FROM surprise_incoming")
        finally:
            self._conn.unregister("surprise_incoming")
        return len(events)

    def read(self, series_id: str, forecaster: str) -> list[SurpriseEvent]:
        """Stored events for ``(series_id, forecaster)`` in release order; empty when the table does not exist."""
        ensure_utc(self._conn)
        if "release_surprise" not in {row[0] for row in self._conn.execute("SHOW TABLES").fetchall()}:
            return []
        frame = self._conn.execute(
            "SELECT * FROM release_surprise WHERE series_id = ? AND forecaster = ? ORDER BY published_at, ref_period_start",
            [series_id, forecaster],
        ).df()
        events: list[SurpriseEvent] = []
        for _, row in frame.iterrows():
            level_unit = str(row["level_unit"])
            change_unit = str(row["change_unit"])
            sigma = row["surprise_sigma"]
            events.append(
                SurpriseEvent(
                    series_id=str(row["series_id"]),
                    ref_period_start=pd.Timestamp(row["ref_period_start"]).date(),
                    published_at=pd.Timestamp(row["published_at"]).tz_convert("UTC").to_pydatetime(),
                    event_date=pd.Timestamp(row["event_date"]).date(),
                    publication_quality=str(row["publication_quality"]),
                    actual=Quantity(value=float(row["actual"]), unit=level_unit, provenance=series_id),
                    previous=Quantity(value=float(row["previous"]), unit=level_unit, provenance=series_id),
                    actual_change=Quantity(value=float(row["actual_change"]), unit=change_unit, provenance=series_id),
                    forecast_change=Quantity(value=float(row["forecast_change"]), unit=change_unit, provenance=forecaster),
                    surprise=Quantity(value=float(row["surprise"]), unit=change_unit, provenance=f"surprise:{forecaster}"),
                    surprise_sigma=None if pd.isna(sigma) else float(sigma),
                    forecaster=str(row["forecaster"]),
                    n_history=int(row["n_history"]),
                )
            )
        return events

    def latest(self, series_id: str, forecaster: str, as_of: datetime) -> SurpriseEvent | None:
        """The newest stored event with ``published_at <= as_of`` (spec §9.2); the B23 ``surprise`` input."""
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        events = [event for event in self.read(series_id, forecaster) if event.published_at <= as_of]
        return events[-1] if events else None
