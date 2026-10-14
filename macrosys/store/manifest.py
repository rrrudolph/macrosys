"""Reproducibility manifest for a decision run (spec §4.4, §9.4).

The digest is sha256 over a canonical JSON document of: per-table row counts and, where the
table has ``ingested_at``, the maximum of that column in UTC; the sorted distinct sha256s of
raw payload files under ``raw_root``; and the library, parameter-set, and code-commit strings.
The digest itself is not part of the hashed document. The same inputs produce the same digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import duckdb
from pydantic import BaseModel, ConfigDict

from macrosys.store.schema import ensure_utc, table_names

_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class TableStat(BaseModel):
    """Row count and latest ingestion time for one table (spec §9.4 snapshot tuple)."""

    model_config = ConfigDict(frozen=True)

    rows: int
    max_ingested_at: str | None = None


class Manifest(BaseModel):
    """The run tuple a decision can be replayed from (spec §4.4, §9.4)."""

    model_config = ConfigDict(frozen=True)

    library_version: str
    parameter_set: str
    code_commit: str
    tables: dict[str, TableStat]
    raw_sha256: list[str]
    digest: str


def build_manifest(
    conn: duckdb.DuckDBPyConnection,
    raw_root: Path,
    library_version: str,
    parameter_set: str,
    code_commit: str,
) -> Manifest:
    """Hash the store, the raw tree, and the three version strings (spec §4.4, §9.4).

    ``raw_root`` is the ``FileRawStore`` root (the parent of ``raw/``). Payload files are
    recognised by a 64-hex filename; sidecar JSON is ignored. ``max_ingested_at`` is formatted
    in UTC with microsecond precision. Tables without ``ingested_at`` contribute a null
    timestamp and still contribute their row count.
    """
    ensure_utc(conn)
    stats = _table_stats(conn)
    hashes = _raw_hashes(Path(raw_root))
    payload = {
        "code_commit": code_commit,
        "library_version": library_version,
        "parameter_set": parameter_set,
        "raw_sha256": hashes,
        "tables": {
            name: {"max_ingested_at": stat.max_ingested_at, "rows": stat.rows}
            for name, stat in sorted(stats.items())
        },
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return Manifest(
        library_version=library_version,
        parameter_set=parameter_set,
        code_commit=code_commit,
        tables=stats,
        raw_sha256=hashes,
        digest=digest,
    )


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Write ``manifest`` as JSON. Parent directories are created. Does not recompute the digest."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")


def read_manifest(path: Path) -> Manifest:
    """Read a manifest written by ``write_manifest`` (spec §9.4)."""
    return Manifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _table_stats(conn: duckdb.DuckDBPyConnection) -> dict[str, TableStat]:
    ingested = {
        name
        for (name,) in conn.execute(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = 'main' AND column_name = 'ingested_at'
            """
        ).fetchall()
    }
    stats: dict[str, TableStat] = {}
    for name in table_names(conn):
        if not _IDENT.fullmatch(name):
            continue
        count = conn.execute(f"SELECT count(*) FROM {name}").fetchone()
        rows = 0 if count is None else int(count[0])
        max_ingested: str | None = None
        if name in ingested:
            stamped = conn.execute(
                f"""
                SELECT CASE
                    WHEN max(ingested_at) IS NULL THEN NULL
                    ELSE strftime(timezone('UTC', max(ingested_at)), '%Y-%m-%dT%H:%M:%S.%fZ')
                END
                FROM {name}
                """
            ).fetchone()
            max_ingested = None if stamped is None else stamped[0]
        stats[name] = TableStat(rows=rows, max_ingested_at=max_ingested)
    return stats


def _raw_hashes(raw_root: Path) -> list[str]:
    if not raw_root.exists():
        return []
    found: set[str] = set()
    for path in raw_root.rglob("*"):
        if path.is_file() and _SHA256.fullmatch(path.name):
            found.add(path.name)
    return sorted(found)
