"""Daily reconciliation: pending fills are class B; a cash break halts (spec §11.7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from macrosys.exec.oms import HaltTrading, OrderManager
from macrosys.exec.reconcile import daily_reconciliation
from tests.exec.support import FakeBroker, ManualClock, ticket


def test_injected_cash_break_halts_and_writes_an_incident(tmp_path) -> None:
    broker = FakeBroker()
    broker.cash_view = {"USD": 50.0}
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        with pytest.raises(HaltTrading):
            daily_reconciliation(oms, broker, 0.01)
        incident = next(item for item in oms.incidents() if item.kind == "reconciliation_break")
        assert incident.failure_class == "A"
        assert "USD" in incident.detail
        assert oms.halted() is True


def test_pending_fill_is_a_class_b_note(tmp_path) -> None:
    broker = FakeBroker()
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    order = ticket(quantity=1_000.0)
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        oms.submit(order)
        broker.positions_view = {"CPER": 1_000.0}
        broker.cash_view = {"USD": -1_000.0}
        report = daily_reconciliation(oms, broker, 0.01)
        assert report.ok is True
        assert report.class_a is False
        notes = [item for item in oms.incidents() if item.failure_class == "B"]
        assert {item.kind for item in notes} == {"pending_fill"}
        assert oms.halted() is False
