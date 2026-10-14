from datetime import UTC, date, datetime

import duckdb

from macrosys.store.manifest import build_manifest, read_manifest, write_manifest
from macrosys.store.observations import ObservationStore
from macrosys.store.raw import FileRawStore
from macrosys.store.schema import create_schema
from tests.store.factories import obs

PUBLISHED = datetime(2026, 7, 30, 12, 30, tzinfo=UTC)


def _one_row():
    return obs(
        "FRED:GDPC1",
        date(2026, 4, 1),
        date(2026, 6, 30),
        100.0,
        PUBLISHED,
        "v1",
        ingested=PUBLISHED,
    )


def test_manifest_digest_is_deterministic_and_changes_when_a_row_is_added(conn, tmp_path):
    raw = tmp_path / "raw-a"
    first = build_manifest(conn, raw, "lib-1", "params-1", "abc123")
    second = build_manifest(conn, raw, "lib-1", "params-1", "abc123")
    assert first.digest == second.digest
    assert len(first.digest) == 64

    other = duckdb.connect(":memory:")
    create_schema(other)
    try:
        same_empty = build_manifest(other, raw, "lib-1", "params-1", "abc123")
    finally:
        other.close()
    assert same_empty.digest == first.digest

    store = ObservationStore(conn)
    assert store.append([_one_row()]) == 1
    changed = build_manifest(conn, raw, "lib-1", "params-1", "abc123")
    assert changed.digest != first.digest
    assert changed.tables["observation"].rows == 1
    assert changed.tables["observation"].max_ingested_at == "2026-07-30T12:30:00.000000Z"

    # A second database with the same observation (and a different schema_version clock) matches.
    replay = duckdb.connect(":memory:")
    create_schema(replay)
    try:
        ObservationStore(replay).append([_one_row()])
        replayed = build_manifest(replay, raw, "lib-1", "params-1", "abc123")
    finally:
        replay.close()
    assert replayed.digest == changed.digest

    payload = FileRawStore(raw).put(
        "FRED",
        {"api_key": "SECRET"},
        b"payload",
        status=200,
        content_type="application/json",
        fetched_at=PUBLISHED,
    )
    with_raw = build_manifest(conn, raw, "lib-1", "params-1", "abc123")
    assert with_raw.raw_sha256 == [payload.sha256]
    assert with_raw.digest != changed.digest
    assert "SECRET" not in payload.model_dump_json()

    path = tmp_path / "out" / "manifest.json"
    write_manifest(path, with_raw)
    assert read_manifest(path) == with_raw
