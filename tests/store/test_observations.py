from datetime import UTC, date, datetime

import pandas as pd
import pytest

from macrosys.core.quantity import Quantity
from macrosys.store.observations import ObservationStore
from tests.store.factories import obs, series

SERIES = "FRED:GDPC1"
Q1 = (date(2026, 1, 1), date(2026, 3, 31))
Q2 = (date(2026, 4, 1), date(2026, 6, 30))
APR30 = datetime(2026, 4, 30, 12, 30, tzinfo=UTC)
JUL30 = datetime(2026, 7, 30, 12, 30, tzinfo=UTC)
AUG1 = datetime(2026, 8, 1, tzinfo=UTC)
AUG28 = datetime(2026, 8, 28, 12, 30, tzinfo=UTC)
SEP1 = datetime(2026, 9, 1, tzinfo=UTC)


def _load(conn) -> ObservationStore:
    store = ObservationStore(conn)
    store.upsert_series([series(SERIES, unit="index")])
    store.append(
        [
            obs(SERIES, *Q1, 90.0, APR30, "v1"),
            obs(SERIES, *Q2, 100.0, JUL30, "v1", ingested=datetime(2026, 12, 1, tzinfo=UTC)),
            obs(SERIES, *Q1, 95.0, AUG28, "v2"),
            obs(SERIES, *Q2, 110.0, AUG28, "v2"),
        ]
    )
    return store


def test_append_is_idempotent_and_counts_inserts(conn):
    store = ObservationStore(conn)
    first = obs(SERIES, *Q2, 100.0, JUL30, "v1")
    second = obs(SERIES, *Q2, 110.0, AUG28, "v2")
    revised = obs(SERIES, *Q2, 999.0, JUL30, "v1")
    third = obs(SERIES, *Q1, 90.0, APR30, "v1")

    assert store.append([first, second]) == 2
    assert store.append([first, second]) == 0
    assert store.append([revised]) == 0
    assert store.append([first, third, third]) == 1
    assert store.append([]) == 0

    stored = conn.execute(
        """
        SELECT value FROM observation
        WHERE series_id = ? AND ref_period_start = ? AND vintage_id = 'v1'
        """,
        [SERIES, Q2[0]],
    ).fetchone()
    assert stored is not None
    assert stored[0] == pytest.approx(100.0)
    assert int(conn.execute("SELECT count(*) FROM observation").fetchone()[0]) == 3


def test_revision_published_after_as_of_is_invisible(conn):
    store = _load(conn)

    early = store.snapshot(AUG1).get(SERIES, vintage="latest")
    assert list(early["vintage_id"]) == ["v1", "v1"]
    assert list(early["value"]) == pytest.approx([90.0, 100.0])

    # Ingested in December but published in July: visible at 1 Aug.
    july_row = early.loc[early["ref_period_start"] == Q2[0]].iloc[0]
    assert july_row["published_at"] == pd.Timestamp(JUL30)

    empty = store.snapshot(datetime(2026, 4, 1, tzinfo=UTC)).get(SERIES)
    assert list(empty.columns) == [
        "ref_period_start",
        "ref_period_end",
        "value",
        "unit",
        "published_at",
        "vintage_id",
        "quality_flag",
    ]
    assert empty.empty

    on_the_instant = store.snapshot(JUL30).get(SERIES, vintage="latest")
    assert list(on_the_instant["value"]) == pytest.approx([90.0, 100.0])


def test_first_versus_latest_vintage(conn):
    store = _load(conn)
    latest = store.snapshot(SEP1).get(SERIES, vintage="latest")
    first = store.snapshot(SEP1).get(SERIES, vintage="first")

    assert list(latest["vintage_id"]) == ["v2", "v2"]
    assert list(latest["value"]) == pytest.approx([95.0, 110.0])
    assert list(first["vintage_id"]) == ["v1", "v1"]
    assert list(first["value"]) == pytest.approx([90.0, 100.0])

    window = store.snapshot(SEP1).get(SERIES, start=Q2[0], end=Q2[0], vintage="latest")
    assert list(window["ref_period_start"]) == [Q2[0]]
    assert window.iloc[0]["value"] == pytest.approx(110.0)

    pit = store.snapshot(AUG1)
    assert pit.latest_value(SERIES) == Quantity(value=100.0, unit="index", provenance=SERIES)
    assert store.snapshot(SEP1).latest_value(SERIES) == Quantity(value=110.0, unit="index", provenance=SERIES)
    assert pit.latest_value("FRED:MISSING") is None


def test_quality_flag_round_trips(conn):
    store = ObservationStore(conn)
    store.append(
        [
            obs(SERIES, *Q2, 1.0, JUL30, "v1", quality_flag="unofficial"),
            obs(SERIES, *Q1, None, APR30, "v1", quality_flag="reconstructed_pubtime"),
        ]
    )
    frame = store.snapshot(SEP1).get(SERIES)
    flags = dict(zip(frame["ref_period_start"], frame["quality_flag"], strict=True))
    assert flags[Q2[0]] == "unofficial"
    assert flags[Q1[0]] == "reconstructed_pubtime"
    assert pd.isna(frame.loc[frame["ref_period_start"] == Q1[0], "value"]).all()
    assert store.snapshot(APR30).latest_value(SERIES) is None


def test_upsert_series_replaces_metadata(conn):
    store = ObservationStore(conn)
    store.upsert_series([series(SERIES, typical_lag_days=10, unit="index")])
    store.upsert_series([series(SERIES, typical_lag_days=45, unit="USD_bn")])
    row = conn.execute("SELECT source, typical_lag_days, unit, tier FROM series").fetchone()
    assert row == ("FRED", 45, "USD_bn", "record")
