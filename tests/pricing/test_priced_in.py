"""Priced-in resolver over an in-memory store (spec §7.3, §7.7)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.pricing import NoPricedInError, StoreImpliedPaths, priced_in
from macrosys.pricing.priced_in import PricedIn
from tests.pricing.conftest import obs

_DAY = date(2026, 3, 2)
_AS_OF = datetime(2026, 3, 2, 23, 0, tzinfo=UTC)


def _policy_store(store):
    store.append(
        [
            obs("CME:FF:2026-03", _DAY, 95.75, unit="index"),
            obs("CME:FF:2026-06", _DAY, 95.60, unit="index"),
            obs("PRICING:CPER:EARNINGS_YIELD", _DAY, 8.0, unit="pct"),
        ]
    )
    return store.snapshot(_AS_OF)


def test_policy_rate_resolves_through_c09(store):
    snap = _policy_store(store)
    paths = StoreImpliedPaths(snap)
    path = paths.policy_path("USD", _DAY)
    start, end = path[0][0], path[-1][0]
    got = priced_in(
        EntityKey(entity_type="currency", entity_id="USD"),
        "policy_rate",
        (start, end),
        paths,
        _DAY,
    )
    # 100 - 95.75 = 4.25, 100 - 95.60 = 4.40. Change = 0.15 percentage points.
    assert got.implied_change.unit == "pp"
    assert got.implied_change.value == pytest.approx(0.15)
    assert got.implied_change.low is not None and got.implied_change.high is not None
    assert got.implied_change.low < got.implied_change.value < got.implied_change.high
    assert got.confidence == "medium"
    assert "C09" in got.sources
    assert "CME:FF:2026-03" in got.sources
    assert "TODO_estimate" in (got.implied_change.provenance or "")


def test_industry_falls_back_with_low_confidence_and_a_wider_range(store):
    snap = _policy_store(store)
    paths = StoreImpliedPaths(snap)
    direct = paths.implied_growth("CPER", _DAY)
    got = priced_in(
        EntityKey(entity_type="industry", entity_id="USA:C26"),
        "output",
        (0.0, 4.0),
        paths,
        _DAY,
    )
    assert got.confidence == "low"
    assert "C05" in got.sources
    assert "CPER" in got.sources
    assert direct.low is not None and direct.high is not None
    assert got.implied_change.low is not None and got.implied_change.high is not None
    direct_width = direct.high - direct.low
    widened = got.implied_change.high - got.implied_change.low
    assert widened == pytest.approx(direct_width * 2.0)
    assert "TODO_estimate" in (got.implied_change.provenance or "")


def test_unmapped_entity_names_what_is_missing(store):
    snap = _policy_store(store)
    paths = StoreImpliedPaths(snap)
    with pytest.raises(NoPricedInError, match="UNMAPPED") as raised:
        priced_in(
            EntityKey(entity_type="industry", entity_id="UNMAPPED"),
            "output",
            (0.0, 4.0),
            paths,
            _DAY,
        )
    assert "output" in str(raised.value)
    assert "nearest instrument" in str(raised.value)


def test_to_gap_is_interval_subtraction():
    # expected [1, 3] minus priced [0, 1] → [0, 3], point 1.5.
    row = PricedIn(
        implied_change=Quantity(value=0.5, unit="pp", low=0.0, high=1.0, provenance="priced"),
        sources=["C09"],
        method_notes="interval check",
        confidence="low",
    )
    gap = row.to_gap(Quantity(value=2.0, unit="pp", low=1.0, high=3.0, provenance="expected"))
    assert gap.unit == "pp"
    assert gap.value == pytest.approx(1.5)
    assert gap.low == pytest.approx(0.0)
    assert gap.high == pytest.approx(3.0)
