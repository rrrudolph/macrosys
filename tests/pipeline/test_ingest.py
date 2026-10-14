from datetime import date

import duckdb

from macrosys.pipeline.ingest import ingest_prices, ingest_series, replay
from macrosys.store.actions import CorporateActionStore
from macrosys.store.observations import ObservationStore
from macrosys.store.prices import PriceStore
from macrosys.store.raw import FileRawStore
from macrosys.store.schema import create_schema
from tests.pipeline.fakes import NOW, BarSource, FakeSource


def test_ingest_inserts_and_quarantines_without_aborting(tmp_path):
    conn = duckdb.connect(":memory:")
    create_schema(conn)
    raw = FileRawStore(tmp_path / "raw")
    obs = ObservationStore(conn)
    report = ingest_series(FakeSource(), ["GOOD", "BAD"], raw, obs, now=NOW)
    by_id = {item.item_id: item for item in report.results}
    assert by_id["GOOD"].inserted == 1
    assert by_id["GOOD"].error is None
    assert by_id["BAD"].quarantined is True
    assert by_id["BAD"].inserted == 0
    assert by_id["GOOD"].elapsed_seconds >= 0
    assert conn.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 1
    conn.close()


def test_replay_is_idempotent(tmp_path):
    conn = duckdb.connect(":memory:")
    create_schema(conn)
    raw = FileRawStore(tmp_path / "raw")
    source = FakeSource()
    source.fetch("GOOD", None, raw)
    obs = ObservationStore(conn)
    first = replay(raw, source, obs)
    second = replay(raw, source, obs)
    assert first.inserted == 1
    assert second.inserted == 0
    assert conn.execute("SELECT count(*) FROM observation").fetchone()[0] == 1
    conn.close()


def test_ingest_prices_uses_bars_method(tmp_path):
    conn = duckdb.connect(":memory:")
    create_schema(conn)
    report = ingest_prices(
        BarSource(),
        ["SPY"],
        FileRawStore(tmp_path / "raw"),
        PriceStore(conn),
        CorporateActionStore(conn),
        start=date(2026, 10, 13),
        end=date(2026, 10, 14),
        now=NOW,
    )
    assert report.results[0].inserted == 1
    assert report.results[0].error is None
    conn.close()
