"""Release calendar (spec §9.1). Upcoming releases are the rows scheduled strictly after ``after``."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import duckdb
import pyarrow as pa

from macrosys.core.contracts import ReleaseEvent
from macrosys.store.schema import ensure_utc

_UPSERT = """
INSERT INTO release_calendar (release_id, scheduled_at, actual_at, source)
SELECT release_id, scheduled_at, actual_at, source
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY release_id, scheduled_at
               ORDER BY ord DESC
           ) AS rn
    FROM release_incoming
) ranked
WHERE rn = 1
ON CONFLICT (release_id, scheduled_at) DO UPDATE SET
    actual_at = EXCLUDED.actual_at,
    source = EXCLUDED.source
"""


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


class ReleaseCalendarStore:
    """Upsert release events and list the next ones (spec §9.1 ``release_calendar``)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def upsert(self, events: list[ReleaseEvent]) -> None:
        """Insert or replace events keyed by ``(release_id, scheduled_at)`` (spec §9.1).

        ``ReleaseEvent.source_id`` is stored in the spec ``source`` column. A later upsert of the
        same key replaces ``actual_at``.
        """
        if not events:
            return
        for event in events:
            _require_aware(event.scheduled_at, "scheduled_at")
            if event.actual_at is not None:
                _require_aware(event.actual_at, "actual_at")
        table = pa.table(
            {
                "release_id": _strings([event.release_id for event in events]),
                "scheduled_at": pa.array(
                    [event.scheduled_at for event in events], type=pa.timestamp("us", tz="UTC")
                ),
                "actual_at": pa.array(
                    [event.actual_at for event in events], type=pa.timestamp("us", tz="UTC")
                ),
                "source": _strings([event.source_id for event in events]),
                "ord": pa.array(list(range(len(events))), type=pa.int64()),
            }
        )
        ensure_utc(self._conn)
        self._conn.register("release_incoming", table)
        try:
            self._conn.execute(_UPSERT)
        finally:
            self._conn.unregister("release_incoming")

    def next_releases(self, after: datetime | date, n: int) -> list[ReleaseEvent]:
        """The next ``n`` releases with ``scheduled_at`` strictly after ``after`` (spec §9.1).

        A date is treated as midnight UTC. ``n`` must be positive. Returns events in schedule order.
        """
        if n < 1:
            raise ValueError(f"n must be >= 1, got {n}")
        if isinstance(after, datetime):
            cutoff = _require_aware(after, "after")
        else:
            cutoff = datetime.combine(after, time.min, tzinfo=UTC)
        ensure_utc(self._conn)
        # Avoid fetchall of TIMESTAMPTZ (that path imports pytz). Format instants as UTC text.
        rows = self._conn.execute(
            """
            SELECT
                release_id,
                strftime(timezone('UTC', scheduled_at), '%Y-%m-%dT%H:%M:%S.%fZ'),
                CASE
                    WHEN actual_at IS NULL THEN NULL
                    ELSE strftime(timezone('UTC', actual_at), '%Y-%m-%dT%H:%M:%S.%fZ')
                END,
                source
            FROM release_calendar
            WHERE scheduled_at > ?
            ORDER BY scheduled_at
            LIMIT ?
            """,
            [cutoff, n],
        ).fetchall()
        events: list[ReleaseEvent] = []
        for release_id, scheduled, actual, source in rows:
            events.append(
                ReleaseEvent(
                    release_id=release_id,
                    scheduled_at=_parse(scheduled),
                    actual_at=None if actual is None else _parse(actual),
                    source_id=source,
                )
            )
        return events


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
