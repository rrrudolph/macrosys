"""Broker boundary for paper execution (spec §11.7, §8.3 guardrail c).

A live adapter (IBKR is the default assumption in spec §15) must not be implemented in this
process. Broker credentials and trade authority live in a separate process that accepts only
validated ``OrderTicket`` values. This module is the interface plus a paper broker: it fills
market-on-close tickets from a close series and has no credentials and no say in size or thesis.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from macrosys.decide.portfolio import OrderTicket

# [J] A paper MOC fill is available at 21:00 UTC on the trade date, after the US cash close.
# This is not an exchange calendar; it only makes "the next close" deterministic in tests.
MOC_PRINT_HOUR_UTC = 21

Clock = Callable[[], datetime]
BrokerEventKind = Literal["working", "partial", "fill", "reject", "cancel"]
LookupState = Literal["filled", "working", "rejected", "cancelled"]


class PriceSource(Protocol):
    """``PriceStore.read_closes`` shape: wide frame, index ``trade_date``, one column per instrument.

    A missing print is NaN, never zero (spec §9.6). The paper broker treats NaN as "no price" and
    an empty frame as "not published yet".
    """

    def __call__(self, instrument_ids: list[str], start: date, end: date) -> pd.DataFrame: ...


class BrokerEvent(BaseModel):
    """One broker-side fact the OMS applies through ``OrderStateMachine`` (spec §11.7)."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1)
    broker_order_id: str = Field(min_length=1)
    client_order_id: str = Field(min_length=1)
    kind: BrokerEventKind
    at: datetime
    qty: float | None = None
    price: float | None = None
    note: str | None = None


class BrokerLookup(BaseModel):
    """What the broker currently believes about one client order id (spec §11.7 Unknown resolution).

    ``filled`` / ``working`` / ``rejected`` are the three outcomes the spec names. ``cancelled`` is
    included so a cancel is not reported as a reject. ``filled_qty`` is USD notional already filled;
    a working order with a positive ``filled_qty`` is a partial fill.
    """

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    state: LookupState
    broker_order_id: str | None = None
    filled_qty: float = 0.0
    avg_price: float | None = None
    filled_at: datetime | None = None


class BrokerAdapter(Protocol):
    """Process boundary the OMS talks to (spec §8.5 ``OrderManager``, §11.7, §8.3 guardrail c).

    Implementations must be idempotent on ``client_order_id``: ``place`` of an id the broker
    already accepted returns the same broker order id and does not open a second order. A live
    adapter (IBKR) is a separate process with its own credentials. It accepts validated
    ``OrderTicket`` values and nothing else — no thesis text, no sizing, no broker password in
    this process.
    """

    def place(self, ticket: OrderTicket) -> str:
        """Accept ``ticket`` and return the broker order id. Raise ``TimeoutError`` if the ack is lost."""

    def cancel(self, broker_order_id: str) -> None:
        """Cancel a working order. The next ``events`` call must show a cancel if it took effect."""

    def events(self, since: datetime | None) -> list[BrokerEvent]:
        """Events at or after ``since`` (all of them when ``since`` is None), oldest first."""

    def lookup(self, client_order_id: str) -> BrokerLookup:
        """Return the broker's view of this client order id. Raise ``KeyError`` if it is unknown."""

    def positions(self) -> Mapping[str, float]:
        """Signed filled notional in USD by instrument. Missing instruments are flat."""

    def cash(self) -> Mapping[str, float]:
        """Cash by currency from the same fills. USD only in the paper broker; buys reduce cash."""


def as_utc(moment: datetime) -> datetime:
    """Require a timezone-aware timestamp and return it in UTC."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware UTC")
    return moment.astimezone(UTC)


def account_from_fills(rows: Iterable[tuple[str, str, float]]) -> tuple[dict[str, float], dict[str, float]]:
    """Signed USD notional and USD cash from ``(instrument_id, side, qty)`` fills (spec §11.7).

    ``qty`` is the filled notional, the same unit as ``OrderTicket.quantity``. A buy adds to the
    instrument and reduces USD cash by the same amount. Near-zero residuals are dropped so a flat
    book compares equal to an empty one.
    """
    positions: dict[str, float] = {}
    cash = 0.0
    for instrument_id, side, qty in rows:
        sign = 1.0 if side == "buy" else -1.0
        positions[instrument_id] = positions.get(instrument_id, 0.0) + sign * qty
        cash -= sign * qty
    positions = {key: value for key, value in positions.items() if abs(value) > 1e-9}
    cash_map = {} if abs(cash) <= 1e-9 else {"USD": cash}
    return positions, cash_map


def slip_price(close: float, side: str, slippage_bp: float) -> float:
    """Execution price: buys pay the close plus slippage, sells receive the close minus slippage.

    ``slippage_bp`` is basis points of the close (10 bp on a close of 100 is 0.10). The notional
    of the fill stays the order notional; the slipped price is the execution record (spec §11.7,
    live-small slippage check in §10.3).
    """
    sign = 1.0 if side == "buy" else -1.0
    return close * (1.0 + sign * slippage_bp / 10_000.0)


def _cents(amount: float) -> float:
    quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(quantized)


def _close_instant(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, MOC_PRINT_HOUR_UTC, 0, tzinfo=UTC)


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _closes(frame: pd.DataFrame, instrument_id: str) -> list[tuple[date, float | None]]:
    if frame is None or frame.empty or instrument_id not in getattr(frame, "columns", ()):
        return []
    found: dict[date, float | None] = {}
    for idx, raw in frame[instrument_id].items():
        if raw is None or pd.isna(raw):
            found[_as_date(idx)] = None
        else:
            found[_as_date(idx)] = float(raw)
    return [(day, found[day]) for day in sorted(found)]


@dataclass
class _PaperOrder:
    ticket: OrderTicket
    broker_order_id: str
    placed_at: datetime
    status: str = "open"
    filled_qty: float = 0.0
    last_fill_price: float | None = None
    last_fill_date: date | None = None
    events: list[BrokerEvent] = field(default_factory=list)


class PaperBroker:
    """Fill MOC tickets at the next published close (spec §11.7 paper adapter).

    ``price_source`` matches ``PriceStore.read_closes`` (instrument ids, start, end) and may omit
    the ``tier`` argument. The next close is the first trade date whose 21:00 UTC print is at or
    after the place time and at or before the clock. ``partial_fill_fraction`` below 1 fills that
    fraction of the order notional on the first close and the remainder on the following close.
    A NaN close rejects the order when ``reject_if_no_price`` is set; an empty frame means the
    close is not published yet and the order keeps working. ``place`` is idempotent on
    ``client_order_id``. Pass ``state_path`` so a later process (the CLI) sees the same working
    orders. There is no IBKR adapter here: live credentials belong in another process
    (spec §8.3(c)).
    """

    def __init__(
        self,
        price_source: PriceSource,
        slippage_bp: float,
        partial_fill_fraction: float = 1.0,
        reject_if_no_price: bool = True,
        *,
        clock: Clock | None = None,
        state_path: Path | None = None,
    ) -> None:
        if slippage_bp < 0:
            raise ValueError("slippage_bp cannot be negative")
        if not 0 < partial_fill_fraction <= 1:
            raise ValueError("partial_fill_fraction must be in (0, 1]")
        self._prices = price_source
        self.slippage_bp = slippage_bp
        self.partial_fill_fraction = partial_fill_fraction
        self.reject_if_no_price = reject_if_no_price
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state_path = Path(state_path) if state_path is not None else None
        self._orders: dict[str, _PaperOrder] = {}
        self._load()

    def place(self, ticket: OrderTicket) -> str:
        """Accept one MOC ticket. The same client order id returns the original broker order id."""
        if ticket.order_type != "MOC":
            raise ValueError("paper broker fills MOC orders only")
        existing = self._orders.get(ticket.client_order_id)
        if existing is not None:
            same = (
                existing.ticket.instrument_id == ticket.instrument_id
                and existing.ticket.side == ticket.side
                and existing.ticket.quantity == ticket.quantity
            )
            if not same:
                raise ValueError("client_order_id already used for a different order")
            return existing.broker_order_id
        placed_at = as_utc(self._clock())
        order = _PaperOrder(
            ticket=ticket,
            broker_order_id=f"paper-{ticket.client_order_id}",
            placed_at=placed_at,
        )
        self._orders[ticket.client_order_id] = order
        self._save()
        return order.broker_order_id

    def cancel(self, broker_order_id: str) -> None:
        """Cancel the remainder. A filled, rejected, or already cancelled order is left as it is."""
        order = self._by_broker_id(broker_order_id)
        if order is None or order.status in ("filled", "rejected", "cancelled"):
            return
        self._emit(
            order,
            kind="cancel",
            at=as_utc(self._clock()),
            event_id=f"{order.ticket.client_order_id}:cancel",
            note="cancelled",
        )
        order.status = "cancelled"
        self._save()

    def events(self, since: datetime | None = None) -> list[BrokerEvent]:
        """Advance fills to the clock, then return events at or after ``since``."""
        now = as_utc(self._clock())
        for order in self._orders.values():
            self._advance(order, now)
        self._save()
        chosen: list[BrokerEvent] = []
        for order in self._orders.values():
            for event in order.events:
                if since is None or event.at >= since:
                    chosen.append(event)
        chosen.sort(key=lambda event: (event.at, event.event_id))
        return chosen

    def lookup(self, client_order_id: str) -> BrokerLookup:
        """Return filled, working (including partial), rejected, or cancelled. Unknown ids raise."""
        order = self._orders.get(client_order_id)
        if order is None:
            raise KeyError(client_order_id)
        state: LookupState
        if order.status == "filled":
            state = "filled"
        elif order.status == "rejected":
            state = "rejected"
        elif order.status == "cancelled":
            state = "cancelled"
        else:
            state = "working"
        filled_at = _close_instant(order.last_fill_date) if order.last_fill_date is not None else None
        return BrokerLookup(
            client_order_id=client_order_id,
            state=state,
            broker_order_id=order.broker_order_id,
            filled_qty=order.filled_qty,
            avg_price=order.last_fill_price,
            filled_at=filled_at,
        )

    def positions(self) -> Mapping[str, float]:
        """Signed filled USD notional, from this broker's own fills (spec §11.7)."""
        positions, _cash = account_from_fills(self._fill_rows())
        return positions

    def cash(self) -> Mapping[str, float]:
        """USD cash flow of this broker's fills. Buys reduce cash (spec §11.7)."""
        _positions, cash = account_from_fills(self._fill_rows())
        return cash

    def _fill_rows(self) -> list[tuple[str, str, float]]:
        rows: list[tuple[str, str, float]] = []
        for order in self._orders.values():
            for event in order.events:
                if event.kind in ("partial", "fill") and event.qty is not None:
                    rows.append((order.ticket.instrument_id, order.ticket.side, event.qty))
        return rows

    def _advance(self, order: _PaperOrder, now: datetime) -> None:
        if order.status in ("filled", "rejected", "cancelled"):
            return
        start = order.placed_at.date()
        if now.date() < start:
            return
        series = _closes(self._prices([order.ticket.instrument_id], start, now.date()), order.ticket.instrument_id)
        for day, price in series:
            if order.last_fill_date is not None and day <= order.last_fill_date:
                continue
            if _close_instant(day) < order.placed_at or now < _close_instant(day):
                continue
            if price is None:
                if self.reject_if_no_price:
                    self._reject_missing(order, day)
                return
            self._fill(order, day, price)
            if order.status in ("filled", "rejected", "cancelled"):
                return

    def _fill(self, order: _PaperOrder, day: date, close: float) -> None:
        remaining = _cents(order.ticket.quantity - order.filled_qty)
        if remaining <= 0:
            order.status = "filled"
            return
        if order.filled_qty <= 0 and self.partial_fill_fraction < 1:
            qty = min(remaining, _cents(order.ticket.quantity * self.partial_fill_fraction))
        else:
            qty = remaining
        if qty <= 0:
            qty = remaining
        kind: BrokerEventKind = "fill" if qty >= remaining - 0.001 else "partial"
        price = slip_price(close, order.ticket.side, self.slippage_bp)
        self._emit(
            order,
            kind=kind,
            at=_close_instant(day),
            event_id=f"{order.ticket.client_order_id}:fill:{day.isoformat()}",
            qty=qty,
            price=price,
        )
        order.filled_qty = _cents(order.filled_qty + qty)
        order.last_fill_price = price
        order.last_fill_date = day
        order.status = "filled" if kind == "fill" else "partial"

    def _reject_missing(self, order: _PaperOrder, day: date) -> None:
        if order.status == "partial":
            self._emit(
                order,
                kind="cancel",
                at=_close_instant(day),
                event_id=f"{order.ticket.client_order_id}:cancel",
                note="no price for the remaining quantity",
            )
            order.status = "cancelled"
            return
        self._emit(
            order,
            kind="reject",
            at=_close_instant(day),
            event_id=f"{order.ticket.client_order_id}:reject:{day.isoformat()}",
            note="no close",
        )
        order.status = "rejected"

    def _emit(
        self,
        order: _PaperOrder,
        *,
        kind: BrokerEventKind,
        at: datetime,
        event_id: str,
        qty: float | None = None,
        price: float | None = None,
        note: str | None = None,
    ) -> None:
        if any(event.event_id == event_id for event in order.events):
            return
        order.events.append(
            BrokerEvent(
                event_id=event_id,
                broker_order_id=order.broker_order_id,
                client_order_id=order.ticket.client_order_id,
                kind=kind,
                at=at,
                qty=qty,
                price=price,
                note=note,
            )
        )

    def _by_broker_id(self, broker_order_id: str) -> _PaperOrder | None:
        for order in self._orders.values():
            if order.broker_order_id == broker_order_id:
                return order
        return None

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        if payload.get("slippage_bp") != self.slippage_bp:
            raise ValueError("paper broker state was recorded with a different slippage_bp")
        if payload.get("partial_fill_fraction") != self.partial_fill_fraction:
            raise ValueError("paper broker state was recorded with a different partial_fill_fraction")
        if payload.get("reject_if_no_price") != self.reject_if_no_price:
            raise ValueError("paper broker state was recorded with a different reject_if_no_price")
        for raw in payload.get("orders", []):
            ticket = OrderTicket.model_validate(raw["ticket"])
            order = _PaperOrder(
                ticket=ticket,
                broker_order_id=raw["broker_order_id"],
                placed_at=as_utc(datetime.fromisoformat(raw["placed_at"])),
                status=raw["status"],
                filled_qty=float(raw["filled_qty"]),
                last_fill_price=raw.get("last_fill_price"),
                last_fill_date=date.fromisoformat(raw["last_fill_date"]) if raw.get("last_fill_date") else None,
                events=[BrokerEvent.model_validate(event) for event in raw.get("events", [])],
            )
            self._orders[ticket.client_order_id] = order

    def _save(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "slippage_bp": self.slippage_bp,
            "partial_fill_fraction": self.partial_fill_fraction,
            "reject_if_no_price": self.reject_if_no_price,
            "orders": [
                {
                    "ticket": order.ticket.model_dump(mode="json"),
                    "broker_order_id": order.broker_order_id,
                    "placed_at": order.placed_at.isoformat(),
                    "status": order.status,
                    "filled_qty": order.filled_qty,
                    "last_fill_price": order.last_fill_price,
                    "last_fill_date": order.last_fill_date.isoformat() if order.last_fill_date else None,
                    "events": [event.model_dump(mode="json") for event in order.events],
                }
                for order in self._orders.values()
            ],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.parent / (self._state_path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._state_path)
