from datetime import UTC, date, datetime

import pytest

from macrosys.core.contracts import HoldingRow
from macrosys.store.holdings import HoldingStore

JUNE = date(2026, 6, 30)
AUGUST = date(2026, 8, 31)
PUBLISHED_EARLY = datetime(2026, 7, 15, 12, tzinfo=UTC)
PUBLISHED_LATE = datetime(2026, 9, 20, 12, tzinfo=UTC)


def _line(as_of: date, published: datetime, key: str, weight: float) -> HoldingRow:
    return HoldingRow(
        instrument_id="SPY",
        as_of=as_of,
        published_at=published,
        source="NPORT",
        holding_key=key,
        name=key,
        weight=weight,
        country="USA",
        sector=None,
        ccy="USD",
        asset_class="equity",
        market_value=None,
        ccy_mv=None,
    )


def test_holdings_snapshot_honours_published_at(conn):
    store = HoldingStore(conn)
    store.append(
        [
            _line(JUNE, PUBLISHED_EARLY, "AAPL", 0.07),
            _line(JUNE, PUBLISHED_EARLY, "MSFT", 0.06),
            _line(AUGUST, PUBLISHED_LATE, "AAPL", 0.08),
        ]
    )
    early = store.latest_snapshot("SPY", datetime(2026, 8, 1, tzinfo=UTC))
    assert sorted(row.holding_key for row in early) == ["AAPL", "MSFT"]
    assert early[0].as_of == JUNE

    late = store.latest_snapshot("SPY", datetime(2026, 10, 1, tzinfo=UTC))
    assert [row.holding_key for row in late] == ["AAPL"]
    assert late[0].as_of == AUGUST
    assert late[0].weight == pytest.approx(0.08)

    assert store.latest_snapshot("SPY", datetime(2026, 7, 1, tzinfo=UTC)) == []
