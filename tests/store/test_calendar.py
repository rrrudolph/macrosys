from datetime import UTC, datetime

from macrosys.core.contracts import ReleaseEvent
from macrosys.store.calendar import ReleaseCalendarStore


def _event(day: int) -> ReleaseEvent:
    return ReleaseEvent(
        release_id="GDP",
        scheduled_at=datetime(2026, day, 1, 12, 30, tzinfo=UTC),
        source_id="FRED",
    )


def test_next_releases_are_strictly_after_and_limited(conn):
    store = ReleaseCalendarStore(conn)
    store.upsert([_event(1), _event(2), _event(3)])
    store.upsert([_event(2)])
    upcoming = store.next_releases(datetime(2026, 1, 1, 12, 30, tzinfo=UTC), 2)
    assert [event.scheduled_at.month for event in upcoming] == [2, 3]
    assert upcoming[0].source_id == "FRED"
    assert len(store.next_releases(datetime(2026, 2, 1, 12, 30, tzinfo=UTC), 1)) == 1
