"""SPY price uses the C05 growth read, from earnings yield or the trailing cash yield (spec §3.3 C05, §7.3)."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from macrosys.core.contracts import CorporateAction, EntityKey, Observation, PriceBar
from macrosys.pricing import StoreImpliedPaths, priced_in
from macrosys.pricing._params import band
from macrosys.store.actions import CorporateActionStore
from macrosys.store.prices import PriceStore
from tests.pricing.conftest import obs

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pricing"
_DAY = date(2026, 3, 2)
_AS_OF = datetime(2026, 3, 2, 23, 0, tzinfo=UTC)
_SPY = EntityKey(entity_type="instrument", entity_id="SPY")


def _load(store) -> None:
    bars = []
    with (_FIXTURES / "spy_closes.csv").open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            day = date.fromisoformat(record["trade_date"])
            close = float(record["close"])
            bars.append(
                PriceBar(
                    instrument_id="TIINGO:SPY",
                    trade_date=day,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1.0,
                    ccy="USD",
                    source_id="TIINGO",
                    tier="record",
                    ingested_at=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
                    quality_flag="ok",
                )
            )
    PriceStore(store.conn).append_bars(bars)
    actions = []
    with (_FIXTURES / "spy_dividends.csv").open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            actions.append(
                CorporateAction(
                    instrument_id="TIINGO:SPY",
                    ex_date=date.fromisoformat(record["ex_date"]),
                    type=record["type"],  # type: ignore[arg-type]
                    amount=float(record["amount"]),
                    ccy="USD",
                )
            )
    CorporateActionStore(store.conn).append(actions)


def test_spy_price_is_the_trailing_yield_gordon_range_and_ignores_later_prints(store):
    _load(store)
    # Published after the as-of date, so it must not replace the trailing yield.
    store.append(
        [
            Observation(
                series_id="PRICING:SPY:EARNINGS_YIELD",
                ref_period_start=_DAY,
                ref_period_end=_DAY,
                value=99.0,
                unit="pct",
                published_at=datetime(2026, 3, 3, 18, 0, tzinfo=UTC),
                ingested_at=datetime(2026, 3, 3, 18, 0, tzinfo=UTC),
                vintage_id="v-future",
                source_url="https://example.test/spy-ey",
                source_hash="c" * 64,
                quality_flag="ok",
            )
        ]
    )
    got = priced_in(_SPY, "price", (0.0, 4.0), StoreImpliedPaths(store.snapshot(_AS_OF)), _DAY)
    required = band("c05_required_return_pct")
    # Four 0.50 dividends over a close of 100 is a 2% trailing yield.
    # The boundary dividend, the next-day dividend, the next-day close, and the
    # capital-gain distribution are excluded.
    assert got.implied_change.unit == "pct"
    assert got.implied_change.value is None
    assert got.implied_change.low == pytest.approx(float(required.low) - 2.0)  # type: ignore[arg-type]
    assert got.implied_change.high == pytest.approx(float(required.high) - 2.0)  # type: ignore[arg-type]
    assert got.confidence == "low"
    assert "C05" in got.sources
    assert "TIINGO:SPY" in got.sources
    assert "PRICING:SPY:EARNINGS_YIELD" not in got.sources
    assert "trailing" in got.method_notes.lower()
    assert "TODO_estimate" in (got.implied_change.provenance or "")
    assert "trailing_dividend" in (got.implied_change.provenance or "")


def test_a_visible_earnings_yield_replaces_the_trailing_dividend(store):
    _load(store)
    store.append([obs("PRICING:SPY:EARNINGS_YIELD", _DAY, 8.0)])
    got = priced_in(_SPY, "price", (0.0, 4.0), StoreImpliedPaths(store.snapshot(_AS_OF)), _DAY)
    assert got.confidence == "medium"
    assert "PRICING:SPY:EARNINGS_YIELD" in got.sources
    assert "trailing_dividend" not in (got.implied_change.provenance or "")


def test_a_later_split_restates_earlier_cash_into_current_shares(store):
    _load(store)
    CorporateActionStore(store.conn).append(
        [CorporateAction(instrument_id="TIINGO:SPY", ex_date=date(2025, 9, 1), type="split", ratio=2.0)]
    )
    got = priced_in(_SPY, "price", (0.0, 4.0), StoreImpliedPaths(store.snapshot(_AS_OF)), _DAY)
    required = band("c05_required_return_pct")
    # June cash is halved. September, December, and March stay 0.50. Sum 1.75 on a close of 100.
    assert got.implied_change.low == pytest.approx(float(required.low) - 1.75)  # type: ignore[arg-type]
    assert got.implied_change.high == pytest.approx(float(required.high) - 1.75)  # type: ignore[arg-type]
