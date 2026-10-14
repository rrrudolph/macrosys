"""DuckDB DDL for the canonical tables (spec §4.7) plus the §9.6 tier columns.

``create_schema`` is idempotent. Timestamps are ``TIMESTAMPTZ`` and the session zone is pinned to UTC
so point-in-time reads do not depend on the machine's local zone (spec §9.2). ``world_state.as_of`` is
``TIMESTAMPTZ`` rather than the ``DATE`` in the spec sketch: ``StateView.as_of`` is a datetime and a
later write the same calendar day must not hide an earlier one. ``world_state`` also stores ``low`` and
``high`` so a ``Quantity`` range survives the round trip (spec §4.7 typed objects).
"""

from __future__ import annotations

import re
from typing import get_args

import duckdb

from macrosys.core.contracts import EntityKey, QualityFlag, SeriesMeta, SourceTier

SCHEMA_VERSION = 2

_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _sql_in(values: tuple[str, ...]) -> str:
    if not values or not all(isinstance(v, str) and v for v in values):
        raise RuntimeError(f"cannot build a SQL IN list from {values!r}")
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


_QUALITY_SQL = _sql_in(get_args(QualityFlag))
_TIER_SQL = _sql_in(get_args(SourceTier))
_FREQ_SQL = _sql_in(tuple(get_args(SeriesMeta.model_fields["frequency"].annotation)))
_ENTITY_SQL = _sql_in(tuple(get_args(EntityKey.model_fields["entity_type"].annotation)))

# Spec §4.7, with tier / quality_flag from spec §9.6 and macrosys.core.contracts.
_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observation (
        series_id TEXT NOT NULL,
        ref_period_start DATE NOT NULL,
        ref_period_end DATE NOT NULL,
        value DOUBLE,
        unit TEXT NOT NULL,
        published_at TIMESTAMPTZ NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,
        vintage_id TEXT NOT NULL,
        source_url TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        quality_flag TEXT NOT NULL DEFAULT 'ok',
        PRIMARY KEY (series_id, ref_period_start, vintage_id),
        CHECK (quality_flag IN ("""
    + _QUALITY_SQL
    + """))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS series (
        series_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        native_id TEXT NOT NULL,
        title TEXT NOT NULL,
        unit TEXT NOT NULL,
        frequency TEXT NOT NULL,
        seasonal_adj TEXT,
        geography TEXT,
        concept TEXT,
        release_id TEXT,
        typical_lag_days INTEGER,
        revision_policy TEXT,
        license TEXT,
        tier TEXT NOT NULL DEFAULT 'record',
        CHECK (frequency IN ("""
    + _FREQ_SQL
    + """)),
        CHECK (tier IN ("""
    + _TIER_SQL
    + """))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS release_calendar (
        release_id TEXT NOT NULL,
        scheduled_at TIMESTAMPTZ NOT NULL,
        actual_at TIMESTAMPTZ,
        source TEXT NOT NULL,
        PRIMARY KEY (release_id, scheduled_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instrument (
        instrument_id TEXT PRIMARY KEY,
        ticker TEXT,
        isin TEXT,
        figi TEXT,
        type TEXT,
        structure TEXT,
        base_ccy TEXT,
        expense_ratio DOUBLE,
        benchmark TEXT,
        issuer TEXT,
        listing_tz TEXT,
        hedged_pair_of TEXT,
        role TEXT,
        underlying TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS holding_snapshot (
        instrument_id TEXT NOT NULL,
        as_of DATE NOT NULL,
        published_at TIMESTAMPTZ NOT NULL,
        source TEXT NOT NULL,
        holding_key TEXT NOT NULL,
        weight DOUBLE,
        country TEXT,
        sector TEXT,
        ccy TEXT,
        asset_class TEXT,
        name TEXT,
        market_value DOUBLE,
        ccy_mv TEXT,
        PRIMARY KEY (instrument_id, as_of, holding_key, source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_bar (
        instrument_id TEXT NOT NULL,
        trade_date DATE NOT NULL,
        open DOUBLE,
        high DOUBLE,
        low DOUBLE,
        close DOUBLE NOT NULL,
        adj_close DOUBLE,
        volume DOUBLE,
        nav DOUBLE,
        ccy TEXT NOT NULL,
        source TEXT NOT NULL,
        ingested_at TIMESTAMPTZ NOT NULL,
        tier TEXT NOT NULL,
        quality_flag TEXT NOT NULL DEFAULT 'ok',
        session_note TEXT,
        PRIMARY KEY (instrument_id, trade_date, source),
        CHECK (tier IN ("""
    + _TIER_SQL
    + """)),
        CHECK (quality_flag IN ("""
    + _QUALITY_SQL
    + """)),
        CHECK (tier <> 'research' OR quality_flag = 'unofficial')
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corporate_action (
        instrument_id TEXT NOT NULL,
        ex_date DATE NOT NULL,
        type TEXT NOT NULL,
        ratio DOUBLE,
        amount DOUBLE,
        ccy TEXT,
        source TEXT NOT NULL,
        PRIMARY KEY (instrument_id, ex_date, type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS io_coefficient (
        vintage TEXT NOT NULL,
        year INTEGER NOT NULL,
        from_econ TEXT NOT NULL,
        from_ind TEXT NOT NULL,
        to_econ TEXT NOT NULL,
        to_ind TEXT NOT NULL,
        a DOUBLE NOT NULL,
        PRIMARY KEY (vintage, year, from_econ, from_ind, to_econ, to_ind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document (
        doc_id TEXT PRIMARY KEY,
        source TEXT,
        url TEXT,
        published_at TIMESTAMPTZ,
        retrieved_at TIMESTAMPTZ,
        content_hash TEXT NOT NULL,
        retained_text_policy TEXT,
        extraction_version TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS claim (
        claim_id TEXT PRIMARY KEY,
        doc_id TEXT REFERENCES document(doc_id),
        span TEXT,
        claimant TEXT,
        entity_type TEXT,
        entity_id TEXT,
        variable TEXT,
        value DOUBLE,
        unit TEXT,
        stage TEXT,
        effective_date DATE,
        claim_made_at TIMESTAMPTZ,
        extraction_version TEXT,
        confidence TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS world_state (
        as_of TIMESTAMPTZ NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        state_var TEXT NOT NULL,
        value DOUBLE,
        low DOUBLE,
        high DOUBLE,
        unit TEXT NOT NULL,
        derived_from TEXT NOT NULL,
        PRIMARY KEY (as_of, entity_type, entity_id, state_var),
        CHECK (entity_type IN ("""
    + _ENTITY_SQL
    + """))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_flag (
        as_of DATE NOT NULL,
        kind TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        detail TEXT NOT NULL,
        PRIMARY KEY (as_of, kind, instrument_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quarantine (
        sha256 TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        series_id TEXT,
        reason TEXT NOT NULL,
        quarantined_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_run (
        job_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        ended_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL,
        manifest_digest TEXT,
        detail TEXT,
        CHECK (kind IN ('daily', 'weekly')),
        CHECK (status IN ('ok', 'blocked', 'error'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_observation_series_published
        ON observation (series_id, published_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_world_state_lookup
        ON world_state (entity_type, entity_id, state_var, as_of)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_price_bar_tier
        ON price_bar (instrument_id, tier, trade_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_holding_published
        ON holding_snapshot (instrument_id, published_at)
    """,
)

# Columns added after the first schema. CREATE TABLE IF NOT EXISTS does not alter an existing table.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("price_bar", "session_note", "TEXT"),
    ("holding_snapshot", "name", "TEXT"),
    ("holding_snapshot", "market_value", "DOUBLE"),
    ("holding_snapshot", "ccy_mv", "TEXT"),
    ("instrument", "hedged_pair_of", "TEXT"),
    ("instrument", "role", "TEXT"),
    ("instrument", "underlying", "TEXT"),
)


def ensure_utc(conn: duckdb.DuckDBPyConnection) -> None:
    """Pin the DuckDB session time zone to UTC (spec §9.2).

    TIMESTAMPTZ values are stored as instants, but DuckDB renders them in the session zone.
    UTC makes snapshot frames and manifest timestamps independent of the host zone.
    """
    conn.execute("SET TimeZone='UTC'")


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create every spec §4.7 table and ``schema_version`` if they are not already there.

    Safe to call more than once: existing tables are left in place and the version row is
    inserted only when missing. Also pins the connection time zone to UTC.
    """
    ensure_utc(conn)
    for statement in _STATEMENTS:
        conn.execute(statement)
    for table, column, typedef in _ADDED_COLUMNS:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {typedef}")
    conn.execute(
        """
        INSERT INTO schema_version (version, applied_at)
        SELECT ?, now()
        WHERE NOT EXISTS (SELECT 1 FROM schema_version WHERE version = ?)
        """,
        [SCHEMA_VERSION, SCHEMA_VERSION],
    )


def table_names(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Return base-table names in this connection, sorted, excluding anything that is not an identifier."""
    rows = conn.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    ).fetchall()
    return [name for (name,) in rows if _IDENT.fullmatch(name)]
