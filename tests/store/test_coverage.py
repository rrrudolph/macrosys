from datetime import UTC, date, datetime

from macrosys.store.coverage import coverage_report
from macrosys.store.observations import ObservationStore
from tests.store.factories import obs, series

AS_OF = datetime(2026, 10, 14, tzinfo=UTC)
FRESH = "FRED:FRESH"
STALE = "FRED:STALE"
MISSING = "FRED:MISSING"


def test_coverage_blocks_on_stale_or_missing_and_passes_on_fresh(conn):
    store = ObservationStore(conn)
    store.upsert_series(
        [
            series(FRESH, typical_lag_days=30),
            series(STALE, typical_lag_days=30),
            series(MISSING, typical_lag_days=5),
        ]
    )
    # Cutoff for lag 30 + tolerance 10 is 2026-09-04. September is fresh; June is stale.
    # A September period published after as_of must not make the stale series look fresh.
    store.append(
        [
            obs(FRESH, date(2026, 9, 1), date(2026, 9, 30), 1.0, datetime(2026, 10, 10, tzinfo=UTC), "v1"),
            obs(STALE, date(2026, 4, 1), date(2026, 6, 30), 1.0, datetime(2026, 7, 30, tzinfo=UTC), "v1"),
            obs(STALE, date(2026, 7, 1), date(2026, 9, 30), 2.0, datetime(2026, 10, 20, tzinfo=UTC), "v2"),
        ]
    )

    report = coverage_report(conn, AS_OF, [(FRESH, 10), (STALE, 10), (MISSING, 10)])
    by_id = {row.series_id: row for row in report.rows}
    assert by_id[FRESH].status == "ok"
    assert by_id[FRESH].latest_ref_period_end == date(2026, 9, 30)
    assert by_id[STALE].status == "stale"
    assert by_id[STALE].latest_ref_period_end == date(2026, 6, 30)
    assert by_id[MISSING].status == "missing"
    assert by_id[MISSING].latest_ref_period_end is None
    assert report.blocking is True

    fresh_only = coverage_report(conn, AS_OF, [(FRESH, 10)])
    assert fresh_only.blocking is False
    assert fresh_only.rows[0].status == "ok"
