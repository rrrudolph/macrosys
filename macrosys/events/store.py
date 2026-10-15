"""Persisted event-study tables (spec §4.7 three timestamps, §10.1 item 11, §9.8).

``release_surprise`` is a derived view of the observation store and is rebuilt per (series,
forecaster). It exists so the weekly job and the thesis layer can read "the latest surprise on
series X" through one query, and so a study run is reproducible from a stored event set.

``calendar_print`` holds the survey-consensus calendar rows (forecast, actual, previous) loaded from
a local file by ``events consensus-load``. Every row is ``quality_flag='unofficial'`` (research tier,
spec §9.6): the values are bare numbers whose printed unit the source dropped, and the consensus has
no publication time of its own — ``scheduled_at`` is the release clock, and the forecast is taken to
have been fixed before it. Nothing in this table enters a decision path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pyarrow as pa

from macrosys.core.quantity import Quantity
from macrosys.events.schema import ConsensusPrint, SurpriseEvent
from macrosys.store.schema import ensure_utc

_CALENDAR_DDL = """
CREATE TABLE IF NOT EXISTS calendar_print (
    source TEXT NOT NULL,
    currency TEXT NOT NULL,
    event TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    forecast DOUBLE,
    actual DOUBLE,
    previous DOUBLE,
    ingested_at TIMESTAMPTZ NOT NULL,
    source_hash TEXT NOT NULL,
    quality_flag TEXT NOT NULL,
    PRIMARY KEY (source, event, scheduled_at),
    CHECK (quality_flag = 'unofficial')
)
"""

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
    """Create the ``release_surprise`` and ``calendar_print`` tables if they do not exist."""
    ensure_utc(conn)
    conn.execute(_DDL)
    conn.execute(_CALENDAR_DDL)


def _has_table(conn: duckdb.DuckDBPyConnection, name: str) -> bool:
    return name in {row[0] for row in conn.execute("SHOW TABLES").fetchall()}


class ConsensusStore:
    """Replace and read calendar prints for one source (spec §9.8). Reads work on a read-only connection."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def replace(self, source: str, prints: list[ConsensusPrint]) -> int:
        """Delete the stored rows for ``source`` and insert ``prints``. Returns the row count."""
        create_events_schema(self._conn)
        for item in prints:
            if item.source != source:
                raise ValueError("print does not belong to the source being replaced")
        self._conn.execute("DELETE FROM calendar_print WHERE source = ?", [source])
        if not prints:
            return 0
        table = pa.table(
            {
                "source": pa.array([p.source for p in prints], type=pa.string()),
                "currency": pa.array([p.currency for p in prints], type=pa.string()),
                "event": pa.array([p.event for p in prints], type=pa.string()),
                "scheduled_at": pa.array([p.scheduled_at for p in prints], type=pa.timestamp("us", tz="UTC")),
                "forecast": pa.array([p.forecast for p in prints], type=pa.float64()),
                "actual": pa.array([p.actual for p in prints], type=pa.float64()),
                "previous": pa.array([p.previous for p in prints], type=pa.float64()),
                "ingested_at": pa.array([p.ingested_at for p in prints], type=pa.timestamp("us", tz="UTC")),
                "source_hash": pa.array([p.source_hash for p in prints], type=pa.string()),
                "quality_flag": pa.array([p.quality_flag for p in prints], type=pa.string()),
            }
        )
        self._conn.register("calendar_incoming", table)
        try:
            self._conn.execute("INSERT INTO calendar_print SELECT * FROM calendar_incoming")
        finally:
            self._conn.unregister("calendar_incoming")
        return len(prints)

    def read(self, source: str, events: set[str] | None = None, as_of: datetime | None = None) -> list[ConsensusPrint]:
        """Stored prints for ``source``, optionally only the named events and only those scheduled at or before ``as_of``."""
        ensure_utc(self._conn)
        if not _has_table(self._conn, "calendar_print"):
            return []
        sql = "SELECT * FROM calendar_print WHERE source = ? AND (? IS NULL OR scheduled_at <= ?)"
        params: list[object] = [source, as_of, as_of]
        if events is not None:
            if not events:
                return []
            sql += " AND event IN (" + ", ".join("?" for _ in events) + ")"
            params.extend(sorted(events))
        frame = self._conn.execute(sql + " ORDER BY scheduled_at, event", params).df()
        out: list[ConsensusPrint] = []
        for _, row in frame.iterrows():
            out.append(
                ConsensusPrint(
                    source=str(row["source"]),
                    currency=str(row["currency"]),
                    event=str(row["event"]),
                    scheduled_at=pd.Timestamp(row["scheduled_at"]).tz_convert("UTC").to_pydatetime(),
                    forecast=_float_or_none(row["forecast"]),
                    actual=_float_or_none(row["actual"]),
                    previous=_float_or_none(row["previous"]),
                    ingested_at=pd.Timestamp(row["ingested_at"]).tz_convert("UTC").to_pydatetime(),
                    source_hash=str(row["source_hash"]),
                )
            )
        return out


def _float_or_none(value: object) -> float | None:
    return None if value is None or pd.isna(value) else float(value)


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
        if not _has_table(self._conn, "release_surprise"):
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
