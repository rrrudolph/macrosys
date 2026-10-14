"""Materialized world-state rows and a point-in-time ``StateView`` (spec §4.3, §4.4).

``write`` appends a ``(as_of, entity, variable)`` row. ``view(as_of)`` reads only rows whose
``as_of`` is at or before the requested instant and returns the latest one. A quantity's range
is stored beside the point value so mechanisms can read back what was written.
"""

from __future__ import annotations

from datetime import datetime

import duckdb

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.store.schema import ensure_utc


def _require_aware(ts: datetime, name: str) -> datetime:
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return ts


class SnapshotStateView:
    """World state as it stood at ``as_of`` (spec §4.3). Later rows are invisible."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, as_of: datetime) -> None:
        self._conn = conn
        self.as_of = as_of

    def get(self, entity: EntityKey, var: str) -> Quantity:
        """Return the latest quantity for ``(entity, var)`` with ``row.as_of <= self.as_of``.

        Raises ``KeyError`` when nothing visible matches. Provenance on the result is the
        stored ``derived_from`` string (spec §4.7 ``world_state``).
        """
        row = self._lookup(entity, var)
        if row is None:
            raise KeyError(f"{entity.entity_type}:{entity.entity_id}.{var}")
        value, low, high, unit, derived_from = row
        return Quantity(value=value, unit=unit, low=low, high=high, provenance=derived_from)

    def has(self, entity: EntityKey, var: str) -> bool:
        """True when ``get`` would return a quantity for this entity and variable (spec §4.3)."""
        return self._lookup(entity, var) is not None

    def _lookup(self, entity: EntityKey, var: str) -> tuple | None:
        ensure_utc(self._conn)
        return self._conn.execute(
            """
            SELECT value, low, high, unit, derived_from
            FROM world_state
            WHERE entity_type = ?
              AND entity_id = ?
              AND state_var = ?
              AND as_of <= ?
            ORDER BY as_of DESC
            LIMIT 1
            """,
            [entity.entity_type, entity.entity_id, var, self.as_of],
        ).fetchone()


class WorldStateStore:
    """Write and read materialized world-state rows (spec §4.3, §4.7 ``world_state``)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def write(self, as_of: datetime, entity: EntityKey, var: str, q: Quantity, derived_from: str) -> None:
        """Record ``q`` for ``entity`` / ``var`` at ``as_of`` (spec §4.3, §4.7).

        A second write with the same ``as_of`` and key replaces that row. A later ``as_of``
        adds history; ``view`` picks the latest row at or before the requested instant.
        ``as_of`` must be timezone-aware. ``derived_from`` names the series or computation.
        """
        as_of = _require_aware(as_of, "as_of")
        if not var:
            raise ValueError("state var must be non-empty")
        if not entity.entity_id:
            raise ValueError("entity_id must be non-empty")
        if not derived_from:
            raise ValueError("derived_from must be non-empty")
        ensure_utc(self._conn)
        self._conn.execute(
            """
            INSERT INTO world_state (
                as_of, entity_type, entity_id, state_var, value, low, high, unit, derived_from
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (as_of, entity_type, entity_id, state_var) DO UPDATE SET
                value = EXCLUDED.value,
                low = EXCLUDED.low,
                high = EXCLUDED.high,
                unit = EXCLUDED.unit,
                derived_from = EXCLUDED.derived_from
            """,
            [
                as_of,
                entity.entity_type,
                entity.entity_id,
                var,
                q.value,
                q.low,
                q.high,
                q.unit,
                derived_from,
            ],
        )

    def view(self, as_of: datetime) -> SnapshotStateView:
        """Point-in-time ``StateView`` of rows with ``as_of <=`` the requested instant (spec §4.3)."""
        return SnapshotStateView(self._conn, _require_aware(as_of, "as_of"))
