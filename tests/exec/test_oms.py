"""Idempotent submit, unknown recovery, partial fills, and illegal transitions (spec §11.7)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from macrosys.decide.portfolio import OrderState
from macrosys.exec.broker import BrokerEvent, PaperBroker
from macrosys.exec.oms import HaltTrading, OrderManager
from macrosys.exec.reconcile import daily_reconciliation
from tests.exec.support import FakeBroker, ManualClock, closes, ticket


def test_duplicate_submit_calls_the_broker_once(tmp_path) -> None:
    broker = FakeBroker()
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    order = ticket()
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        first = oms.submit(order)
        second = oms.submit(order)
        assert first is OrderState.SUBMITTED
        assert second is OrderState.SUBMITTED
        assert broker.place_calls == 1

        broker.scripted_events.append(
            BrokerEvent(
                event_id="fill-1",
                broker_order_id="B-" + order.client_order_id,
                client_order_id=order.client_order_id,
                kind="fill",
                qty=order.quantity,
                price=50.0,
                at=datetime(2026, 3, 2, 21, 0, tzinfo=UTC),
            )
        )
        oms.poll()
        assert oms.submit(order) is OrderState.FILLED
        assert broker.place_calls == 1


def test_broker_timeout_is_unknown_until_lookup(tmp_path) -> None:
    broker = FakeBroker()
    broker.timeout = True
    broker.lookup_state = "working"
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    order = ticket()
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        with pytest.raises(HaltTrading):
            oms.submit(order)
        assert oms.state(order.client_order_id) is OrderState.UNKNOWN
        assert any(item.failure_class == "A" and item.kind == "unknown_state" for item in oms.incidents())
        events = oms.resolve_unknown(30)
        assert broker.lookups == 1
        assert oms.state(order.client_order_id) is OrderState.WORKING
        assert any(event.to_state is OrderState.WORKING for event in events)
        assert oms.halted() is False


def test_submitted_past_timeout_is_resolved_from_lookup(tmp_path) -> None:
    broker = FakeBroker()
    broker.lookup_state = "rejected"
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    order = ticket()
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        assert oms.submit(order) is OrderState.SUBMITTED
        clock.moment = datetime(2026, 3, 2, 15, 2, tzinfo=UTC)
        events = oms.resolve_unknown(30)
        assert broker.lookups == 1
        assert any(event.to_state is OrderState.UNKNOWN for event in events)
        assert oms.state(order.client_order_id) is OrderState.REJECTED
        assert oms.halted() is False


def test_partial_fill_then_completion_records_two_fills(tmp_path) -> None:
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    prices = closes("CPER", {date(2026, 3, 2): 100.0, date(2026, 3, 3): 100.0})
    broker = PaperBroker(prices, slippage_bp=10, partial_fill_fraction=0.25, clock=clock)
    order = ticket(quantity=1_000.0)
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        assert oms.submit(order) is OrderState.SUBMITTED
        assert oms.poll() == []
        assert oms.state(order.client_order_id) is OrderState.SUBMITTED

        clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=UTC)
        oms.poll()
        first = oms.fills(order.client_order_id)
        assert oms.state(order.client_order_id) is OrderState.PARTIALLY_FILLED
        assert len(first) == 1
        assert first[0].qty == pytest.approx(250.0)
        assert first[0].price == pytest.approx(100.1)

        clock.moment = datetime(2026, 3, 3, 21, 0, tzinfo=UTC)
        oms.poll()
        both = oms.fills(order.client_order_id)
        assert oms.state(order.client_order_id) is OrderState.FILLED
        assert [item.qty for item in both] == pytest.approx([250.0, 750.0])
        assert oms.positions()["CPER"] == pytest.approx(1_000.0)
        assert oms.cash()["USD"] == pytest.approx(-1_000.0)
        assert daily_reconciliation(oms, broker, 0.01).ok is True
        assert broker.place(order) == "paper-" + order.client_order_id


def test_missing_close_rejects(tmp_path) -> None:
    clock = ManualClock(datetime(2026, 3, 2, 21, 0, tzinfo=UTC))
    broker = PaperBroker(closes("CPER", {date(2026, 3, 2): None}), slippage_bp=0, clock=clock)
    order = ticket()
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        oms.submit(order)
        oms.poll()
        assert oms.state(order.client_order_id) is OrderState.REJECTED
        assert oms.fills(order.client_order_id) == []


def test_illegal_transition_halts_with_a_class_a_incident(tmp_path) -> None:
    broker = FakeBroker()
    clock = ManualClock(datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    order = ticket()
    broker_order_id = "B-" + order.client_order_id
    with OrderManager(tmp_path / "oms.sqlite", broker, clock) as oms:
        oms.submit(order)
        broker.scripted_events.extend(
            [
                BrokerEvent(
                    event_id="ack",
                    broker_order_id=broker_order_id,
                    client_order_id=order.client_order_id,
                    kind="working",
                    at=datetime(2026, 3, 2, 15, 1, tzinfo=UTC),
                ),
                BrokerEvent(
                    event_id="nope",
                    broker_order_id=broker_order_id,
                    client_order_id=order.client_order_id,
                    kind="reject",
                    at=datetime(2026, 3, 2, 15, 2, tzinfo=UTC),
                ),
            ]
        )
        with pytest.raises(HaltTrading):
            oms.poll()
        assert oms.state(order.client_order_id) is OrderState.WORKING
        incident = next(item for item in oms.incidents() if item.kind == "illegal_transition")
        assert incident.failure_class == "A"
        assert incident.resolved_at is None
        assert oms.halted() is True
