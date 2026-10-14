"""Corporate-action table (spec §9.1). Splits and distributions are inputs to total return, not observations."""

from __future__ import annotations

from datetime import date

import duckdb
import pyarrow as pa

from macrosys.core.contracts import CorporateAction
from macrosys.store.schema import ensure_utc

_INSERT = """
INSERT INTO corporate_action (instrument_id, ex_date, type, ratio, amount, ccy, source)
SELECT instrument_id, ex_date, type, ratio, amount, ccy, source
FROM (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY instrument_id, ex_date, type
               ORDER BY ord
           ) AS rn
    FROM action_incoming
) ranked
WHERE rn = 1
ON CONFLICT (instrument_id, ex_date, type) DO NOTHING
"""


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


class CorporateActionStore:
    """Append splits and distributions (spec §9.1 ``corporate_action``)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def append(self, rows: list[CorporateAction]) -> int:
        """Insert actions, ignoring duplicates of ``(instrument_id, ex_date, type)`` (spec §9.4).

        The first row in a batch wins. The spec ``source`` column is ``source`` or ``source_id``
        when the row carries one, otherwise an empty string. Returns rows actually inserted.
        """
        if not rows:
            return 0
        sources = [_source(row) for row in rows]
        table = pa.table(
            {
                "instrument_id": _strings([row.instrument_id for row in rows]),
                "ex_date": pa.array([row.ex_date for row in rows], type=pa.date32()),
                "type": _strings([row.type for row in rows]),
                "ratio": pa.array([row.ratio for row in rows], type=pa.float64()),
                "amount": pa.array([row.amount for row in rows], type=pa.float64()),
                "ccy": _strings([row.ccy for row in rows]),
                "source": _strings(sources),
                "ord": pa.array(list(range(len(rows))), type=pa.int64()),
            }
        )
        self._conn.register("action_incoming", table)
        try:
            inserted = self._conn.execute(_INSERT).fetchone()
        finally:
            self._conn.unregister("action_incoming")
        if inserted is None:
            raise RuntimeError("corporate-action insert did not report a row count")
        return int(inserted[0])

    def for_instrument(self, instrument_id: str, start: date | None = None, end: date | None = None) -> list[CorporateAction]:
        """Return actions for one instrument, optionally bounded by ex-date, oldest first (spec §9.1)."""
        frame = self._conn.execute(
            """
            SELECT instrument_id, ex_date, type, ratio, amount, ccy
            FROM corporate_action
            WHERE instrument_id = ?
              AND (? IS NULL OR ex_date >= ?)
              AND (? IS NULL OR ex_date <= ?)
            ORDER BY ex_date, type
            """,
            [instrument_id, start, start, end, end],
        ).fetchall()
        actions: list[CorporateAction] = []
        for instrument, ex_date, kind, ratio, amount, ccy in frame:
            actions.append(
                CorporateAction(
                    instrument_id=instrument,
                    ex_date=ex_date,
                    type=kind,
                    ratio=ratio,
                    amount=amount,
                    ccy=ccy or None,
                )
            )
        return actions


def _source(row: CorporateAction) -> str:
    extra = getattr(row, "source", None) or getattr(row, "source_id", None)
    return str(extra) if extra else ""
