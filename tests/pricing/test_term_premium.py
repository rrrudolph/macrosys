"""Published term-premium series ids (spec §3.3 C03)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from macrosys.pricing.curves import ZeroCurve
from macrosys.pricing.errors import CurveError
from macrosys.pricing.term_premium import expected_short_rate_path, term_premium_from_series
from tests.pricing.conftest import obs


def test_missing_acm_series_is_named(store):
    snap = store.snapshot(datetime(2024, 1, 3, tzinfo=UTC))
    with pytest.raises(CurveError, match="NYFED:ACM:TP10"):
        term_premium_from_series(snap, 10, model="ACM")


def test_acm_premium_is_subtracted_from_the_matching_tenor(store):
    day = date(2024, 1, 2)
    store.append([obs("NYFED:ACM:TP10", day, 0.8)])
    snap = store.snapshot(datetime(2024, 1, 3, tzinfo=UTC))
    premium = term_premium_from_series(snap, 10, model="ACM")
    assert premium.provenance == "NYFED:ACM:TP10"
    assert premium.value == pytest.approx(0.8)
    zero = ZeroCurve.from_pairs([(2.0, 4.0), (10.0, 4.0)], compounding="continuous", source="FEDGSW")
    path = expected_short_rate_path(zero, premium)
    assert len(path) == 1
    assert path[0][0] == pytest.approx(40.0)
    assert path[0][1].value == pytest.approx(3.2)


def test_kim_wright_is_the_fred_ten_year(store):
    day = date(2024, 1, 2)
    store.append([obs("FRED:THREEFYTP10", day, 0.5)])
    snap = store.snapshot(datetime(2024, 1, 3, tzinfo=UTC))
    premium = term_premium_from_series(snap, 10, model="KW")
    assert premium.provenance == "FRED:THREEFYTP10"
    with pytest.raises(CurveError, match="THREEFYTP10|tenor 2"):
        term_premium_from_series(snap, 2, model="KW")
