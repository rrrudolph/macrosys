"""Credit-spread implied change and the named gaps beside it (spec §3.3 C06, §7.3)."""

from __future__ import annotations

import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from macrosys.core.contracts import EntityKey, Observation
from macrosys.pricing import NoPricedInError, StoreImpliedPaths, priced_in
from macrosys.pricing.spreads import implied_spread_change
from tests.pricing.conftest import obs

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pricing" / "hy_oas.csv"
_DAY = date(2025, 1, 19)
_AS_OF = datetime(2025, 1, 19, 23, 0, tzinfo=UTC)
_USA = EntityKey(entity_type="economy", entity_id="USA")


def _load(store) -> None:
    rows = []
    with _FIXTURE.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            rows.append(obs("FRED:BAMLH0A0HYM2", date.fromisoformat(record["ref_date"]), float(record["value"])))
    store.append(rows)


def test_hy_oas_fixture_is_zero_with_the_tenth_to_ninetieth_band(store):
    _load(store)
    # A later vintage of the first print would move the -100 bp change to -500 bp.
    store.append(
        [
            Observation(
                series_id="FRED:BAMLH0A0HYM2",
                ref_period_start=date(2024, 1, 1),
                ref_period_end=date(2024, 1, 1),
                value=9.0,
                unit="pct",
                published_at=datetime(2025, 1, 20, 18, 0, tzinfo=UTC),
                ingested_at=datetime(2025, 1, 20, 18, 0, tzinfo=UTC),
                vintage_id="v-future",
                source_url="https://example.test/future",
                source_hash="b" * 64,
                quality_flag="ok",
            )
        ]
    )
    got = priced_in(_USA, "spread", (0.0, 4.0), StoreImpliedPaths(store.snapshot(_AS_OF)), _DAY)
    # Ten changes from -100 bp to -10 bp and ten from 0 bp to 90 bp.
    # Linear 10th percentile is -81 bp; linear 90th is 71 bp. The point is 0.
    assert got.implied_change.unit == "bp"
    assert got.implied_change.value == pytest.approx(0.0)
    assert got.implied_change.low == pytest.approx(-81.0)
    assert got.implied_change.high == pytest.approx(71.0)
    assert got.confidence == "medium"
    assert got.sources[0] == "C06"
    assert "FRED:BAMLH0A0HYM2" in got.sources
    assert "zero" in got.method_notes.lower()
    assert "extended_to_zero" not in (got.implied_change.provenance or "")


def test_short_oas_history_names_the_series_and_the_minimum(store):
    store.append(
        [
            obs("FRED:BAMLH0A0HYM2", date(2024, 1, 1), 4.0),
            obs("FRED:BAMLH0A0HYM2", date(2025, 1, 1), 5.0),
        ]
    )
    with pytest.raises(NoPricedInError) as raised:
        priced_in(
            _USA,
            "spread",
            (0.0, 4.0),
            StoreImpliedPaths(store.snapshot(datetime(2025, 1, 1, 23, 0, tzinfo=UTC))),
            date(2025, 1, 1),
        )
    message = str(raised.value)
    assert "FRED:BAMLH0A0HYM2" in message
    assert "20" in message


def test_loaded_oas_does_not_stand_in_for_bank_credit_growth(store):
    _load(store)
    with pytest.raises(NoPricedInError) as raised:
        priced_in(_USA, "credit", (0.0, 4.0), StoreImpliedPaths(store.snapshot(_AS_OF)), _DAY)
    message = str(raised.value)
    assert "TODO_estimate" in message
    assert "FRED:BAMLH0A0HYM2" in message
    assert "FRED:BAMLC0A0CM" in message
    assert "FRED:BUSLOANS" in message


def test_one_sided_history_is_extended_to_keep_the_zero_point():
    history: list[tuple[date, float]] = []
    for index in range(20):
        start = date(2024, 1, 1) + timedelta(days=index)
        history.append((start, 4.0))
        history.append((start + timedelta(days=365), 4.0 + (index + 1) * 0.1))
    got = implied_spread_change(
        history,
        window_days=365,
        level_unit="pct",
        low_percentile=10,
        high_percentile=90,
        min_changes=20,
        tolerance_days=7,
        series_id="FRED:BAMLH0A0HYM2",
    )
    # Changes are +10 bp through +200 bp. The 10th percentile is 29 bp, so the
    # reported low is extended to 0. The 90th percentile is 181 bp.
    assert got.value == pytest.approx(0.0)
    assert got.low == pytest.approx(0.0)
    assert got.high == pytest.approx(181.0)
    assert got.provenance is not None and "extended_to_zero" in got.provenance
