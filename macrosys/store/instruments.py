"""Instrument master table (spec §9.1). Rows come from ``macrosys.instruments.master.Instrument``."""

from __future__ import annotations

import duckdb
import pyarrow as pa

from macrosys.instruments.master import Instrument
from macrosys.store.schema import ensure_utc

_UPSERT = """
INSERT INTO instrument (
    instrument_id, ticker, isin, figi, type, structure, base_ccy, expense_ratio,
    benchmark, issuer, listing_tz, hedged_pair_of, role, underlying
)
SELECT
    instrument_id, ticker, isin, figi, type, structure, base_ccy, expense_ratio,
    benchmark, issuer, listing_tz, hedged_pair_of, role, underlying
FROM (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY instrument_id ORDER BY ord DESC) AS rn
    FROM instrument_incoming
) ranked
WHERE rn = 1
ON CONFLICT (instrument_id) DO UPDATE SET
    ticker = EXCLUDED.ticker,
    isin = EXCLUDED.isin,
    figi = EXCLUDED.figi,
    type = EXCLUDED.type,
    structure = EXCLUDED.structure,
    base_ccy = EXCLUDED.base_ccy,
    expense_ratio = EXCLUDED.expense_ratio,
    benchmark = EXCLUDED.benchmark,
    issuer = EXCLUDED.issuer,
    listing_tz = EXCLUDED.listing_tz,
    hedged_pair_of = EXCLUDED.hedged_pair_of,
    role = EXCLUDED.role,
    underlying = EXCLUDED.underlying
"""


def _strings(values: list[str | None]) -> pa.Array:
    return pa.array(values, type=pa.string())


class InstrumentStore:
    """Upsert the instrument master (spec §4.7 ``instrument``, §9.1)."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self._conn = conn
        ensure_utc(conn)

    def upsert(self, instruments: list[Instrument]) -> None:
        """Insert or replace instruments. The last row for an ``instrument_id`` in the batch wins."""
        if not instruments:
            return
        table = pa.table(
            {
                "instrument_id": _strings([row.instrument_id for row in instruments]),
                "ticker": _strings([row.ticker for row in instruments]),
                "isin": _strings([row.isin for row in instruments]),
                "figi": _strings([row.figi for row in instruments]),
                "type": _strings([row.type for row in instruments]),
                "structure": _strings([row.structure for row in instruments]),
                "base_ccy": _strings([row.base_ccy for row in instruments]),
                "expense_ratio": pa.array([row.expense_ratio for row in instruments], type=pa.float64()),
                "benchmark": _strings([row.benchmark for row in instruments]),
                "issuer": _strings([row.issuer for row in instruments]),
                "listing_tz": _strings([row.listing_tz for row in instruments]),
                "hedged_pair_of": _strings([row.hedged_pair_of for row in instruments]),
                "role": _strings([row.role for row in instruments]),
                "underlying": _strings([row.underlying for row in instruments]),
                "ord": pa.array(list(range(len(instruments))), type=pa.int64()),
            }
        )
        self._conn.register("instrument_incoming", table)
        try:
            self._conn.execute(_UPSERT)
        finally:
            self._conn.unregister("instrument_incoming")
