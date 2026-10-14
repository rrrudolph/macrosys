"""SQLite order manager: idempotent submit, poll, and unknown-state recovery (spec §11.7).

The ledger is the SQLite file (single writer, WAL, one transaction per public call). This process
does not read a thesis and does not choose a size. It stores validated ``OrderTicket`` values and
applies broker facts through ``OrderStateMachine``. Positions and cash are signed USD notional
derived from fills, the same unit as ``OrderTicket.quantity``, because that is what
``Reconciler`` compares. Share counts are ``notional / fill price`` and are not the book.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from macrosys.decide.portfolio import IllegalTransitionError, OrderState, OrderStateMachine, OrderTicket
from macrosys.exec.broker import BrokerAdapter, BrokerEvent, BrokerLookup, account_from_fills, as_utc

Clock = Callable[[], datetime]
_OPEN = (
    OrderState.SUBMITTED.value,
    OrderState.WORKING.value,
    OrderState.PARTIALLY_FILLED.value,
)
_PENDING = _OPEN + (OrderState.UNKNOWN.value,)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    run_id TEXT NOT NULL,
    thesis_ids TEXT NOT NULL,
    state TEXT NOT NULL,
    broker_order_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    filled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    at TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    class TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS applied_events (
    event_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(client_order_id);
"""


class HaltTrading(RuntimeError):  # noqa: N818 — ticket X5.1 names this HaltTrading, not HaltTradingError
    """Class-A halt: do not submit new orders until the incident is resolved (spec §11.7)."""

    def __init__(self, message: str, *, report: object | None = None) -> None:
        super().__init__(message)
        self.report = report


class SubmissionGate(Protocol):
    """Anything ``submit`` consults before sending an order. The kill switch is the implementation."""

    def is_engaged(self) -> bool: ...


class KillSwitchEngaged(HaltTrading):
    """Submission refused because the kill switch flag is set (spec §11.7)."""


class OrderEvent(BaseModel):
    """One ledger transition written to ``events`` (spec §11.7)."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    from_state: OrderState | None
    to_state: OrderState
    at: datetime
    note: str | None = None


class Fill(BaseModel):
    """One row of ``fills``. ``qty`` is filled USD notional; ``price`` is the execution price."""

    model_config = ConfigDict(frozen=True)

    fill_id: str
    client_order_id: str
    qty: float
    price: float
    filled_at: datetime


class Incident(BaseModel):
    """One failure-class row. ``failure_class`` is A, B, or C (spec §11.7)."""

    model_config = ConfigDict(frozen=True)

    id: int
    failure_class: str
    kind: str
    detail: str
    at: datetime
    resolved_at: datetime | None


class ManagedOrder(BaseModel):
    """One persisted order plus the notional filled so far."""

    model_config = ConfigDict(frozen=True)

    client_order_id: str
    instrument_id: str
    side: Literal["buy", "sell"]
    quantity: float
    order_type: str
    state: OrderState
    broker_order_id: str | None
    filled_qty: float
    run_id: str


class OrderManager:
    """Persisted OMS (spec §11.7). ``clock`` supplies every timestamp; pass a callable the tests can move.

    ``submit`` is idempotent on ``client_order_id``. Any id already in the ledger is returned as
    stored and is not sent to the broker again, whether it is still working or already filled,
    rejected, or cancelled — the id is the primary key, and a second insert would be a duplicate
    order. ``poll`` applies broker events through ``OrderStateMachine``. An illegal transition is
    stored as a class-A incident and halts. Orders left in ``Submitted`` longer than ``timeout_s``
    become ``Unknown``; ``resolve_unknown`` then asks the broker by client order id.
    """

    def __init__(
        self,
        db_path: Path,
        broker: BrokerAdapter,
        clock: Clock | None = None,
        *,
        kill_switch: SubmissionGate | None = None,
    ) -> None:
        self.broker = broker
        self._clock = clock or (lambda: datetime.now(UTC))
        self._kill_switch = kill_switch
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Checkpoint the WAL and close the file so Windows can delete the database."""
        if self._conn is None:
            return
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._conn.close()
        self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> OrderManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def submit(self, ticket: OrderTicket) -> OrderState:
        """Submit ``ticket`` once. A known client order id returns the stored state and does not call the broker.

        A ``TimeoutError`` from ``place`` is a lost ack: the order is stored as ``Unknown``, a class-A
        incident is written, and ``HaltTrading`` is raised. ``resolve_unknown`` is how that order is
        recovered (spec §11.7).
        """
        self._guard()
        timed_out = False
        with self._tx():
            existing = self._fetch(ticket.client_order_id)
            if existing is not None:
                return OrderState(existing["state"])
            now = self._now()
            self._insert_new(ticket, now)
            try:
                broker_order_id = self.broker.place(ticket)
            except TimeoutError:
                self._move(ticket.client_order_id, OrderState.SUBMITTED, now, "submit sent; ack timed out")
                self._move(ticket.client_order_id, OrderState.UNKNOWN, now, "broker timeout")
                self._insert_incident(
                    "A",
                    "unknown_state",
                    json.dumps({"client_order_id": ticket.client_order_id, "note": "broker timeout"}),
                    at=now,
                )
                timed_out = True
            else:
                self._move(
                    ticket.client_order_id,
                    OrderState.SUBMITTED,
                    now,
                    "broker accepted",
                    broker_order_id=broker_order_id,
                )
        if timed_out:
            raise HaltTrading(f"broker timeout; {ticket.client_order_id} is Unknown")
        return OrderState.SUBMITTED

    def poll(self) -> list[OrderEvent]:
        """Apply broker events since the cursor. An illegal transition halts with a class-A incident."""
        with self._tx():
            applied, halt = self._apply_available()
        if halt is not None:
            raise halt
        return applied

    def resolve_unknown(self, timeout_s: float) -> list[OrderEvent]:
        """Age ``Submitted`` orders past ``timeout_s`` into ``Unknown``, then adopt the broker lookup.

        Lookup outcomes are filled, working (a partial stays ``PartiallyFilled``), rejected, or
        cancelled (spec §11.7). An order that stays ``Unknown`` because the broker has no record
        leaves the class-A incident open and raises ``HaltTrading``.
        """
        if timeout_s < 0:
            raise ValueError("timeout_s cannot be negative")
        halt: HaltTrading | None = None
        with self._tx():
            applied: list[OrderEvent] = []
            now = self._now()
            for row in self._rows_in(OrderState.SUBMITTED.value):
                age = (now - _parse(row["updated_at"])).total_seconds()
                if age >= timeout_s:
                    applied.append(self._move(row["client_order_id"], OrderState.UNKNOWN, now, "submit timeout"))
                    self._insert_incident(
                        "A",
                        "unknown_state",
                        json.dumps({"client_order_id": row["client_order_id"], "note": "submit timeout"}),
                        at=now,
                    )
            for row in self._rows_in(OrderState.UNKNOWN.value):
                resolved = self._resolve_one(row, now)
                if resolved is not None:
                    applied.append(resolved)
            still = self._rows_in(OrderState.UNKNOWN.value)
            if still:
                names = ", ".join(item["client_order_id"] for item in still)
                halt = HaltTrading(f"still Unknown after lookup: {names}")
        if halt is not None:
            raise halt
        return applied

    def positions(self) -> dict[str, float]:
        """Signed filled USD notional by instrument, from the fills table (spec §11.7)."""
        positions, _cash = account_from_fills(self._fill_rows())
        return positions

    def cash(self) -> dict[str, float]:
        """USD cash flow of the fills table. Buys reduce cash. This is not a funded account balance."""
        _positions, cash = account_from_fills(self._fill_rows())
        return cash

    def cancel_all(self) -> list[OrderEvent]:
        """Ask the broker to cancel every Submitted, Working, or PartiallyFilled order, then apply the events."""
        halt: HaltTrading | None = None
        with self._tx():
            for row in self._open_rows():
                broker_order_id = row["broker_order_id"]
                if broker_order_id:
                    self.broker.cancel(broker_order_id)
            applied, halt = self._apply_available()
        if halt is not None:
            raise halt
        return applied

    def state(self, client_order_id: str) -> OrderState:
        """Stored state of one order. Raises ``KeyError`` when the id was never submitted."""
        row = self._fetch(client_order_id)
        if row is None:
            raise KeyError(client_order_id)
        return OrderState(row["state"])

    def fills(self, client_order_id: str) -> list[Fill]:
        """Fills for one order, oldest first."""
        rows = self._conn.execute(
            """
            SELECT fill_id, client_order_id, qty, price, filled_at
            FROM fills WHERE client_order_id = ? ORDER BY filled_at, fill_id
            """,
            (client_order_id,),
        ).fetchall()
        return [
            Fill(
                fill_id=row["fill_id"],
                client_order_id=row["client_order_id"],
                qty=float(row["qty"]),
                price=float(row["price"]),
                filled_at=_parse(row["filled_at"]),
            )
            for row in rows
        ]

    def incidents(self) -> list[Incident]:
        """Every incident, oldest first."""
        rows = self._conn.execute(
            'SELECT id, "class" AS failure_class, kind, detail, at, resolved_at FROM incidents ORDER BY id'
        ).fetchall()
        return [
            Incident(
                id=int(row["id"]),
                failure_class=row["failure_class"],
                kind=row["kind"],
                detail=row["detail"],
                at=_parse(row["at"]),
                resolved_at=_parse(row["resolved_at"]) if row["resolved_at"] else None,
            )
            for row in rows
        ]

    def orders(self) -> list[ManagedOrder]:
        """Every order with its filled notional."""
        return [self._managed(row) for row in self._all_rows()]

    def open_orders(self) -> list[ManagedOrder]:
        """Orders whose remaining notional can still trade: Submitted, Working, PartiallyFilled, Unknown."""
        return [order for order in self.orders() if order.state.value in _PENDING]

    def halted(self) -> bool:
        """True when a class-A incident is still unresolved (spec §11.7 halt)."""
        row = self._conn.execute(
            "SELECT 1 FROM incidents WHERE class = 'A' AND resolved_at IS NULL LIMIT 1"
        ).fetchone()
        return row is not None

    def record_incident(self, failure_class: str, kind: str, detail: str, *, resolved: bool = False) -> int:
        """Append one incident. ``failure_class`` is ``A``, ``B``, or ``C`` (spec §11.7)."""
        if failure_class not in ("A", "B", "C"):
            raise ValueError("failure class must be A, B, or C")
        now = self._now()
        with self._tx():
            return self._insert_incident(
                failure_class, kind, detail, at=now, resolved_at=now if resolved else None
            )

    def resolve_incidents(self, kind: str, *, note: str) -> None:
        """Close unresolved class-A incidents of ``kind`` and store ``note`` as the acknowledgement."""
        if not note or not note.strip():
            raise ValueError("acknowledgement required")
        now = self._now()
        with self._tx():
            self._conn.execute(
                "UPDATE incidents SET resolved_at = ? WHERE class = 'A' AND kind = ? AND resolved_at IS NULL",
                (_iso(now), kind),
            )
            self._insert_incident("A", f"{kind}_ack", note.strip(), at=now, resolved_at=now)

    def _guard(self) -> None:
        if self._kill_switch is not None and self._kill_switch.is_engaged():
            raise KillSwitchEngaged("kill switch is engaged; submission is disabled")
        if self.halted():
            raise HaltTrading("unresolved class-A incident; submission is halted")

    def _resolve_one(self, row: sqlite3.Row, now: datetime) -> OrderEvent | None:
        client_order_id = row["client_order_id"]
        try:
            view = self.broker.lookup(client_order_id)
        except KeyError:
            return None
        target = _lookup_state(view, float(row["quantity"]))
        if target in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            if view.filled_qty <= 0:
                return None
            if not self._book_lookup_fill(row, view, now):
                return None
        broker_order_id = view.broker_order_id or row["broker_order_id"]
        event = self._move(
            client_order_id,
            target,
            view.filled_at or now,
            f"lookup {view.state}",
            broker_order_id=broker_order_id,
        )
        self._resolve_unknown_incident(client_order_id, now)
        return event

    def _book_lookup_fill(self, row: sqlite3.Row, view: BrokerLookup, now: datetime) -> bool:
        already = self._filled_qty(row["client_order_id"])
        delta = view.filled_qty - already
        if delta <= 0.001:
            return True
        if view.avg_price is None or view.avg_price <= 0:
            self._insert_incident(
                "A",
                "unknown_state",
                json.dumps(
                    {
                        "client_order_id": row["client_order_id"],
                        "note": "lookup reported a fill without a price",
                    }
                ),
                at=now,
            )
            return False
        if already + delta > float(row["quantity"]) + 0.01:
            self._insert_incident(
                "A",
                "overfill",
                json.dumps(
                    {
                        "client_order_id": row["client_order_id"],
                        "note": f"lookup filled {view.filled_qty} of {row['quantity']}",
                    }
                ),
                at=now,
            )
            return False
        fill_id = f"lookup-{row['client_order_id']}-{_cents_key(already + delta)}"
        self._insert_fill(fill_id, row["client_order_id"], delta, view.avg_price, view.filled_at or now)
        return True

    def _resolve_unknown_incident(self, client_order_id: str, now: datetime) -> None:
        rows = self._conn.execute(
            "SELECT id, detail FROM incidents WHERE class = 'A' AND kind = 'unknown_state' AND resolved_at IS NULL"
        ).fetchall()
        for incident in rows:
            try:
                payload = json.loads(incident["detail"])
            except json.JSONDecodeError:
                payload = {}
            if payload.get("client_order_id") == client_order_id:
                self._conn.execute(
                    "UPDATE incidents SET resolved_at = ? WHERE id = ?",
                    (_iso(now), incident["id"]),
                )

    def _apply_available(self) -> tuple[list[OrderEvent], HaltTrading | None]:
        since = self._cursor()
        applied: list[OrderEvent] = []
        halt: HaltTrading | None = None
        for event in self.broker.events(since):
            outcome = self._apply_one(event)
            if isinstance(outcome, HaltTrading):
                halt = outcome
                break
            if outcome is not None:
                applied.append(outcome)
        return applied, halt

    def _apply_one(self, event: BrokerEvent) -> OrderEvent | HaltTrading | None:
        if self._applied(event.event_id):
            self._set_cursor(event.at)
            return None
        row = self._fetch(event.client_order_id)
        self._mark_applied(event)
        self._set_cursor(event.at)
        if row is None:
            self._insert_incident(
                "A",
                "unknown_broker_event",
                json.dumps({"client_order_id": event.client_order_id, "event_id": event.event_id}),
                at=event.at,
            )
            return HaltTrading(f"broker event {event.event_id} for an order the ledger does not have")
        try:
            target, fill = _event_plan(event, row, self._filled_qty(event.client_order_id))
        except ValueError as exc:
            self._insert_incident(
                "A",
                "bad_fill",
                json.dumps({"client_order_id": event.client_order_id, "note": str(exc)}),
                at=event.at,
            )
            return HaltTrading(str(exc))
        current = OrderState(row["state"])
        try:
            _check_transition(current, target)
        except IllegalTransitionError:
            note = f"class-A illegal transition: {current.value} -> {target.value}"
            self._insert_event(event.client_order_id, current, target, event.at, note)
            self._insert_incident(
                "A",
                "illegal_transition",
                json.dumps({"client_order_id": event.client_order_id, "note": note, "event_id": event.event_id}),
                at=event.at,
            )
            return HaltTrading(note)
        if fill is not None:
            self._insert_fill(event.event_id, event.client_order_id, fill[0], fill[1], event.at)
        note = event.note or event.kind
        if target is OrderState.CANCELLED:
            remaining = float(row["quantity"]) - self._filled_qty(event.client_order_id)
            note = f"remaining {remaining:.2f} USD"
        return self._move(
            event.client_order_id,
            target,
            event.at,
            note,
            broker_order_id=event.broker_order_id,
        )

    def _move(
        self,
        client_order_id: str,
        to: OrderState,
        at: datetime,
        note: str,
        *,
        broker_order_id: str | None = None,
    ) -> OrderEvent:
        row = self._must(client_order_id)
        current = OrderState(row["state"])
        _check_transition(current, to)
        self._conn.execute(
            """
            UPDATE orders
            SET state = ?, updated_at = ?, broker_order_id = COALESCE(?, broker_order_id)
            WHERE client_order_id = ?
            """,
            (to.value, _iso(at), broker_order_id, client_order_id),
        )
        self._insert_event(client_order_id, current, to, at, note)
        return OrderEvent(
            client_order_id=client_order_id, from_state=current, to_state=to, at=at, note=note
        )

    def _insert_new(self, ticket: OrderTicket, now: datetime) -> None:
        stamp = _iso(now)
        self._conn.execute(
            """
            INSERT INTO orders (
                client_order_id, instrument_id, side, quantity, order_type, limit_price,
                run_id, thesis_ids, state, broker_order_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                ticket.client_order_id,
                ticket.instrument_id,
                ticket.side,
                ticket.quantity,
                ticket.order_type,
                ticket.limit,
                ticket.run_id,
                json.dumps(list(ticket.thesis_ids)),
                OrderState.CREATED.value,
                stamp,
                stamp,
            ),
        )

    def _insert_fill(self, fill_id: str, client_order_id: str, qty: float, price: float, at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO fills (fill_id, client_order_id, qty, price, filled_at) VALUES (?, ?, ?, ?, ?)",
            (fill_id, client_order_id, qty, price, _iso(at)),
        )

    def _insert_event(
        self,
        client_order_id: str,
        from_state: OrderState | None,
        to_state: OrderState,
        at: datetime,
        note: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (client_order_id, from_state, to_state, at, note) VALUES (?, ?, ?, ?, ?)",
            (client_order_id, None if from_state is None else from_state.value, to_state.value, _iso(at), note),
        )

    def _insert_incident(
        self,
        failure_class: str,
        kind: str,
        detail: str,
        *,
        at: datetime,
        resolved_at: datetime | None = None,
    ) -> int:
        cursor = self._conn.execute(
            'INSERT INTO incidents ("class", kind, detail, at, resolved_at) VALUES (?, ?, ?, ?, ?)',
            (failure_class, kind, detail, _iso(at), _iso(resolved_at) if resolved_at else None),
        )
        return int(cursor.lastrowid)

    def _mark_applied(self, event: BrokerEvent) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO applied_events (event_id, client_order_id, applied_at) VALUES (?, ?, ?)",
            (event.event_id, event.client_order_id, _iso(event.at)),
        )

    def _applied(self, event_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM applied_events WHERE event_id = ?", (event_id,)).fetchone()
        return row is not None

    def _cursor(self) -> datetime | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'events_since'").fetchone()
        if row is None:
            return None
        return _parse(row["value"])

    def _set_cursor(self, at: datetime) -> None:
        current = self._cursor()
        if current is not None and at < current:
            return
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('events_since', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_iso(at),),
        )

    def _filled_qty(self, client_order_id: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(qty), 0) AS filled FROM fills WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return float(row["filled"])

    def _fill_rows(self) -> list[tuple[str, str, float]]:
        rows = self._conn.execute(
            """
            SELECT o.instrument_id, o.side, f.qty
            FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id
            """
        ).fetchall()
        return [(row["instrument_id"], row["side"], float(row["qty"])) for row in rows]

    def _fetch(self, client_order_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()

    def _must(self, client_order_id: str) -> sqlite3.Row:
        row = self._fetch(client_order_id)
        if row is None:
            raise KeyError(client_order_id)
        return row

    def _rows_in(self, state: str) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM orders WHERE state = ? ORDER BY client_order_id", (state,)
            ).fetchall()
        )

    def _open_rows(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in _OPEN)
        return list(
            self._conn.execute(
                f"SELECT * FROM orders WHERE state IN ({marks}) ORDER BY client_order_id", _OPEN
            ).fetchall()
        )

    def _all_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM orders ORDER BY client_order_id").fetchall())

    def _managed(self, row: sqlite3.Row) -> ManagedOrder:
        return ManagedOrder(
            client_order_id=row["client_order_id"],
            instrument_id=row["instrument_id"],
            side=row["side"],
            quantity=float(row["quantity"]),
            order_type=row["order_type"],
            state=OrderState(row["state"]),
            broker_order_id=row["broker_order_id"],
            filled_qty=self._filled_qty(row["client_order_id"]),
            run_id=row["run_id"],
        )

    def _now(self) -> datetime:
        return as_utc(self._clock())

    @contextmanager
    def _tx(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")


def _check_transition(current: OrderState, to: OrderState) -> None:
    machine = OrderStateMachine(current)
    if current is OrderState.UNKNOWN:
        machine.resolve(to)
    else:
        machine.transition(to)


def _lookup_state(view: BrokerLookup, quantity: float) -> OrderState:
    if view.state == "rejected":
        return OrderState.REJECTED
    if view.state == "cancelled":
        return OrderState.CANCELLED
    if view.state == "filled" or view.filled_qty >= quantity - 0.001:
        return OrderState.FILLED
    if view.filled_qty > 0:
        return OrderState.PARTIALLY_FILLED
    return OrderState.WORKING


def _event_plan(
    event: BrokerEvent, row: sqlite3.Row, filled: float
) -> tuple[OrderState, tuple[float, float] | None]:
    if event.kind == "reject":
        return OrderState.REJECTED, None
    if event.kind == "cancel":
        return OrderState.CANCELLED, None
    if event.kind == "working":
        return OrderState.WORKING, None
    if event.kind not in ("partial", "fill"):
        raise ValueError(f"unknown broker event kind {event.kind}")
    if event.qty is None or event.qty <= 0 or event.price is None or event.price <= 0:
        raise ValueError(f"fill {event.event_id} needs a positive qty and price")
    quantity = float(row["quantity"])
    if filled + event.qty > quantity + 0.01:
        raise ValueError(f"fill {event.event_id} would exceed order quantity {quantity}")
    if filled + event.qty >= quantity - 0.001:
        return OrderState.FILLED, (event.qty, event.price)
    return OrderState.PARTIALLY_FILLED, (event.qty, event.price)


def _cents_key(amount: float) -> str:
    return f"{amount:.2f}"


def _iso(moment: datetime) -> str:
    return as_utc(moment).isoformat()


def _parse(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(value))


def pending_cover(orders: list[ManagedOrder]) -> tuple[Mapping[str, float], float]:
    """Unsigned remaining notional by instrument, and the same total as USD cash cover (spec §11.7).

    A daily break no larger than this cover is a pending fill (class B). Anything beyond it is
    unexplained (class A).
    """
    by_instrument: dict[str, float] = {}
    cash = 0.0
    for order in orders:
        remaining = order.quantity - order.filled_qty
        if remaining <= 0.001:
            continue
        by_instrument[order.instrument_id] = by_instrument.get(order.instrument_id, 0.0) + remaining
        cash += remaining
    return by_instrument, cash
