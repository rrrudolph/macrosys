"""Quarantine rows for payloads a parser refused (spec §9.4)."""

from __future__ import annotations

from datetime import datetime

import duckdb

from macrosys.store.schema import ensure_utc


def quarantine_payload(
    conn: duckdb.DuckDBPyConnection,
    *,
    sha256: str,
    source_id: str,
    reason: str,
    series_id: str | None,
    at: datetime,
) -> None:
    """Record a payload that must not be appended until the parser is approved (spec §9.4).

    Idempotent on ``sha256``: a second quarantine of the same bytes keeps the first reason.
    """
    ensure_utc(conn)
    conn.execute(
        """
        INSERT INTO quarantine (sha256, source_id, series_id, reason, quarantined_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (sha256) DO NOTHING
        """,
        [sha256, source_id, series_id, reason, at],
    )


def clear_quarantine(conn: duckdb.DuckDBPyConnection, sha256: str) -> None:
    """Drop a quarantine row after a replay parses the payload (spec §9.1, §9.4)."""
    conn.execute("DELETE FROM quarantine WHERE sha256 = ?", [sha256])
