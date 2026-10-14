from datetime import UTC, datetime

import pytest

from macrosys.core.contracts import EntityKey, StateView
from macrosys.core.quantity import Quantity
from macrosys.store.state import WorldStateStore

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 6, 1, tzinfo=UTC)
USA = EntityKey(entity_type="economy", entity_id="USA")


def test_snapshot_state_view_honours_as_of_and_raises_on_unknown(conn):
    store = WorldStateStore(conn)
    store.write(T1, USA, "policy_rate", Quantity(value=4.5, unit="pct", provenance="ignored"), "FRED:FEDFUNDS")
    store.write(T2, USA, "policy_rate", Quantity(value=5.0, unit="pct"), "FRED:FEDFUNDS")
    store.write(
        T1,
        USA,
        "gap",
        Quantity(value=None, unit="pct", low=1.0, high=3.0),
        "estimate",
    )

    early = store.view(T1)
    assert isinstance(early, StateView)
    assert early.as_of == T1
    assert early.has(USA, "policy_rate")
    got = early.get(USA, "policy_rate")
    assert got.value == pytest.approx(4.5)
    assert got.unit == "pct"
    assert got.provenance == "FRED:FEDFUNDS"

    mid = store.view(datetime(2026, 3, 1, tzinfo=UTC))
    assert mid.get(USA, "policy_rate").value == pytest.approx(4.5)
    assert store.view(T2).get(USA, "policy_rate").value == pytest.approx(5.0)

    gap = early.get(USA, "gap")
    assert gap.value is None and gap.low == pytest.approx(1.0) and gap.high == pytest.approx(3.0)

    before = store.view(datetime(2025, 1, 1, tzinfo=UTC))
    assert before.has(USA, "policy_rate") is False
    with pytest.raises(KeyError):
        before.get(USA, "policy_rate")
    with pytest.raises(KeyError):
        early.get(USA, "not_a_var")
    assert early.has(USA, "not_a_var") is False
