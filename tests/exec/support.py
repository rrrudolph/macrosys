"""Shared fixtures for paper-execution tests. No network."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from macrosys.decide.portfolio import OrderTicket
from macrosys.exec.broker import BrokerEvent, BrokerLookup


class ManualClock:
    """Injectable UTC clock. Tests move ``moment``; nothing here reads the wall clock."""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


def ticket(
    *,
    instrument_id: str = "CPER",
    side: str = "buy",
    quantity: float = 1_000.0,
    client_order_id: str | None = None,
    run_id: str = "run-1",
) -> OrderTicket:
    return OrderTicket(
        instrument_id=instrument_id,
        side=side,  # type: ignore[arg-type]
        quantity=quantity,
        order_type="MOC",
        limit=None,
        client_order_id=client_order_id or ("a" * 24),
        run_id=run_id,
        thesis_ids=["T-1"],
    )


def closes(instrument_id: str, rows: dict[date, float | None]):
    """``read_closes``-shaped callable. NaN in ``rows`` is a missing print."""

    def read(instrument_ids: list[str], start: date, end: date) -> pd.DataFrame:
        column = instrument_ids[0] if instrument_ids else instrument_id
        days = [day for day in sorted(rows) if start <= day <= end]
        index = pd.DatetimeIndex(pd.to_datetime(days), name="trade_date")
        return pd.DataFrame({column: [rows[day] for day in days]}, index=index)

    return read


class FakeBroker:
    """In-memory broker. ``timeout`` makes ``place`` raise after remembering the order."""

    def __init__(self) -> None:
        self.place_calls = 0
        self.lookups = 0
        self.cancelled: list[str] = []
        self.timeout = False
        self.lookup_state = "working"
        self.lookup_filled_qty = 0.0
        self.lookup_price: float | None = None
        self.positions_view: dict[str, float] = {}
        self.cash_view: dict[str, float] = {}
        self.scripted_events: list[BrokerEvent] = []
        self._ids: dict[str, str] = {}
        self._accepted: set[str] = set()

    def place(self, order: OrderTicket) -> str:
        self.place_calls += 1
        broker_order_id = "B-" + order.client_order_id
        self._ids[order.client_order_id] = broker_order_id
        self._accepted.add(order.client_order_id)
        if self.timeout:
            raise TimeoutError("broker timeout")
        return broker_order_id

    def cancel(self, broker_order_id: str) -> None:
        self.cancelled.append(broker_order_id)
        client_order_id = next(cid for cid, bid in self._ids.items() if bid == broker_order_id)
        self.scripted_events.append(
            BrokerEvent(
                event_id=f"cancel-{broker_order_id}",
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                kind="cancel",
                at=datetime(2026, 1, 15, 18, 0, tzinfo=UTC),
            )
        )

    def events(self, since: datetime | None = None) -> list[BrokerEvent]:
        if since is None:
            return list(self.scripted_events)
        return [event for event in self.scripted_events if event.at >= since]

    def lookup(self, client_order_id: str) -> BrokerLookup:
        self.lookups += 1
        if client_order_id not in self._accepted:
            raise KeyError(client_order_id)
        return BrokerLookup(
            client_order_id=client_order_id,
            state=self.lookup_state,  # type: ignore[arg-type]
            broker_order_id=self._ids.get(client_order_id),
            filled_qty=self.lookup_filled_qty,
            avg_price=self.lookup_price,
        )

    def positions(self) -> dict[str, float]:
        return dict(self.positions_view)

    def cash(self) -> dict[str, float]:
        return dict(self.cash_view)
