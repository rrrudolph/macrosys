from datetime import UTC, date, datetime

import pandas as pd
import pytest

from macrosys.core.contracts import PriceBar
from macrosys.instruments.actions import PRICE_DISCREPANCY_FLAG
from macrosys.instruments.align import aligned_returns
from macrosys.store.prices import PriceStore

WHEN = datetime(2026, 10, 14, 20, tzinfo=UTC)


def _bar(instrument: str, day: date, close: float, *, source: str, tier: str, flag: str = "ok") -> PriceBar:
    return PriceBar(
        instrument_id=instrument,
        trade_date=day,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        ccy="USD",
        source_id=source,
        tier=tier,  # type: ignore[arg-type]
        ingested_at=WHEN,
        quality_flag=flag,  # type: ignore[arg-type]
    )


def test_research_tier_requires_unofficial_flag(conn):
    store = PriceStore(conn)
    bad = _bar("CPER", date(2026, 10, 13), 30.0, source="TRADINGVIEW_UNOFFICIAL", tier="research", flag="ok")
    with pytest.raises(ValueError, match="unofficial"):
        store.append_bars([bad])
    assert conn.execute("SELECT count(*) FROM price_bar").fetchone()[0] == 0

    good = _bar(
        "CPER", date(2026, 10, 13), 30.0, source="TRADINGVIEW_UNOFFICIAL", tier="research", flag="unofficial"
    )
    assert store.append_bars([good, good]) == 1
    assert store.append_bars([good]) == 0


def test_cross_check_flags_discrepancy_date(conn):
    store = PriceStore(conn)
    day = date(2026, 10, 13)
    quiet = date(2026, 10, 14)
    store.append_bars(
        [
            _bar("SPY", day, 100.0, source="TIINGO", tier="record"),
            _bar("SPY", day, 110.0, source="MT5", tier="cross_check"),
            _bar("SPY", quiet, 100.0, source="TIINGO", tier="record"),
            _bar("SPY", quiet, 100.4, source="MT5", tier="cross_check"),
        ]
    )
    flagged = store.cross_check("SPY", date(2026, 10, 1), date(2026, 10, 31), tol=0.02)
    assert flagged == [day]
    row = conn.execute("SELECT kind, detail FROM data_flag").fetchone()
    assert row is not None
    assert row[0] == PRICE_DISCREPANCY_FLAG
    assert "110" in row[1]

    wide = store.read_closes(["SPY", "TLT"], date(2026, 10, 1), date(2026, 10, 31))
    assert list(wide.columns) == ["SPY", "TLT"]
    assert wide.loc[wide.index == _ts(day), "SPY"].iloc[0] == pytest.approx(100.0)
    frames = {name: wide[[name]].rename(columns={name: "close"}).dropna() for name in wide.columns if wide[name].notna().any()}
    panel = aligned_returns(frames)
    assert "SPY" in panel.prices.columns


def _ts(day: date) -> pd.Timestamp:
    return pd.Timestamp(day)
