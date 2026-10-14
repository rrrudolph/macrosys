"""Tickets, the order state machine, and reconciliation (spec §11.7)."""

from __future__ import annotations

import hashlib

import pytest

from macrosys.decide.mandate import load_example_mandate
from macrosys.decide.portfolio import (
    IllegalTransitionError,
    LimitCheckMissingError,
    OrderState,
    OrderStateMachine,
    PortfolioConstructor,
    Reconciler,
    client_order_id,
)
from macrosys.decide.thesis import ThesisStatus
from tests.decide.support import make_thesis


def _approved(**overrides: object):
    thesis = make_thesis(status=ThesisStatus.approved, **overrides)
    return thesis


def test_tickets_are_deterministic_and_idempotency_key_is_stable() -> None:
    mandate = load_example_mandate()
    thesis = _approved()
    other = _approved(
        thesis_id="T-bonds",
        expression={
            "instrument": "IEF",
            "direction": "long",
            "exposure_vector_check": "passed",
            "structure_notes": "treasury ETF",
            "cost_estimate_bp": 2.0,
        },
    )
    constructor = PortfolioConstructor("run-2026-10-14", min_notional=1.0)
    sizes = {thesis.thesis_id: 0.10, other.thesis_id: 0.04}
    current = {"CPER": 0.02}
    first = constructor.propose([other, thesis], sizes, current, mandate, nav=100_000.0)
    second = constructor.propose([thesis, other], sizes, current, mandate, nav=100_000.0)
    assert [ticket.model_dump() for ticket in first] == [ticket.model_dump() for ticket in second]
    assert [ticket.instrument_id for ticket in first] == ["CPER", "IEF"]
    cper = first[0]
    assert cper.side == "buy"
    assert cper.quantity == pytest.approx(8_000.0)  # (0.10 - 0.02) * 100_000
    raw = f"run-2026-10-14|CPER|buy|{cper.quantity:.2f}"
    assert cper.client_order_id == hashlib.sha256(raw.encode()).hexdigest()[:24]
    assert cper.client_order_id == client_order_id("run-2026-10-14", "CPER", "buy", cper.quantity)
    assert cper.thesis_ids == ["T-2026-0412"]


def test_min_notional_skips_and_position_limit_clips() -> None:
    mandate = load_example_mandate()
    thesis = _approved()
    quiet = PortfolioConstructor("run-1", min_notional=1_000.0)
    assert quiet.propose([thesis], {thesis.thesis_id: 0.001}, {}, mandate, nav=10_000.0) == []

    capped = mandate.model_copy(update={"risk": mandate.risk.model_copy(update={"position_limit_pct": 5.0})})
    tickets = PortfolioConstructor("run-1").propose([thesis], {thesis.thesis_id: 0.20}, {}, capped, nav=100_000.0)
    assert tickets[0].quantity == pytest.approx(5_000.0)

    def position_ok(weights: dict[str, float]) -> bool:
        return all(weight <= 0.03 + 1e-12 for weight in weights.values())

    limited = PortfolioConstructor("run-2", position_ok=position_ok)
    held = limited.propose([thesis], {thesis.thesis_id: 0.10}, {}, mandate, nav=100_000.0)
    assert held[0].quantity / 100_000.0 <= 0.03 + 1e-9
    assert held[0].quantity > 2_000.0


def test_unwired_factor_limit_refuses_to_trade() -> None:
    mandate = load_example_mandate().model_copy(
        update={"risk": load_example_mandate().risk.model_copy(update={"factor_limit_pct": 25.0})}
    )
    thesis = _approved()
    with pytest.raises(LimitCheckMissingError):
        PortfolioConstructor("run-1").propose([thesis], {thesis.thesis_id: 0.05}, {}, mandate, nav=100_000.0)


def test_sells_down_toward_a_smaller_target() -> None:
    mandate = load_example_mandate()
    thesis = _approved()
    tickets = PortfolioConstructor("run-1").propose(
        [thesis], {thesis.thesis_id: 0.04}, {"CPER": 0.10}, mandate, nav=100_000.0
    )
    assert len(tickets) == 1
    assert tickets[0].side == "sell"
    assert tickets[0].quantity == pytest.approx(6_000.0)


def test_state_machine_rejects_illegal_transitions() -> None:
    machine = OrderStateMachine()
    with pytest.raises(IllegalTransitionError):
        machine.transition(OrderState.FILLED)
    machine.transition(OrderState.SUBMITTED)
    machine.transition(OrderState.UNKNOWN)
    machine.resolve(OrderState.WORKING)
    machine.transition(OrderState.PARTIALLY_FILLED)
    machine.transition(OrderState.WORKING)
    machine.transition(OrderState.FILLED)
    with pytest.raises(IllegalTransitionError):
        machine.transition(OrderState.CANCELLED)
    with pytest.raises(IllegalTransitionError):
        OrderStateMachine(OrderState.SUBMITTED).resolve(OrderState.FILLED)


def test_reconciler_flags_an_unexplained_break() -> None:
    broken = Reconciler.compare(
        {"CPER": 10.0},
        {"CPER": 12.0},
        {"USD": 1_000.0},
        {"USD": 1_000.0},
        tol=0.5,
    )
    assert broken.class_a is True
    assert broken.ok is False
    assert broken.breaks[0].key == "CPER"
    assert broken.breaks[0].kind == "position"

    cash = Reconciler.compare({}, {}, {"USD": 100.0}, {"USD": 90.0}, tol=1.0)
    assert cash.class_a is True
    assert cash.breaks[0].kind == "cash"

    matched = Reconciler.compare({"CPER": 10.0}, {"CPER": 10.2}, {"USD": 5.0}, {"USD": 5.0}, tol=0.5)
    assert matched.ok is True
    assert matched.class_a is False
    assert matched.breaks == []
