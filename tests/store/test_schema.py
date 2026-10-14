from macrosys.store.schema import SCHEMA_VERSION, create_schema

_TABLES = {
    "schema_version",
    "observation",
    "series",
    "release_calendar",
    "instrument",
    "holding_snapshot",
    "price_bar",
    "corporate_action",
    "io_coefficient",
    "document",
    "claim",
    "world_state",
}


def test_create_schema_is_idempotent_and_has_tier_columns(conn):
    create_schema(conn)
    create_schema(conn)
    names = {
        row[0]
        for row in conn.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'main' AND table_type = 'BASE TABLE'
            """
        ).fetchall()
    }
    assert _TABLES <= names
    versions = conn.execute("SELECT version, count(*) FROM schema_version GROUP BY version").fetchall()
    assert versions == [(SCHEMA_VERSION, 1)]

    price_cols = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = 'price_bar'
            """
        ).fetchall()
    }
    assert {"tier", "quality_flag", "adj_close", "source", "ingested_at"} <= price_cols

    obs_cols = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'main' AND table_name = 'observation'
            """
        ).fetchall()
    }
    assert "quality_flag" in obs_cols

    conn.execute(
        """
        INSERT INTO price_bar (
            instrument_id, trade_date, close, ccy, source, ingested_at, tier, quality_flag
        ) VALUES (
            'CPER', DATE '2026-10-13', 30.0, 'USD', 'tradingview_unofficial',
            TIMESTAMPTZ '2026-10-14T20:00:00Z', 'research', 'unofficial'
        )
        """
    )
    stored = conn.execute("SELECT tier, quality_flag FROM price_bar").fetchone()
    assert stored == ("research", "unofficial")
