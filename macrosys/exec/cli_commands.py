"""CLI for paper execution (spec §11.7).

The CLI package owns ``macrosys.cli``. Call ``register_cli(subparsers)`` from there. Each
subcommand sets ``args.func``; dispatch with ``args.func(args)``, which returns a process exit
code. Exit 2 means a class-A halt, the kill switch, or a runbook block. This process only
submits tickets it is given. It does not read a thesis or choose a size.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from macrosys.decide.portfolio import OrderTicket
from macrosys.exec.broker import PaperBroker
from macrosys.exec.killswitch import KillSwitch
from macrosys.exec.oms import HaltTrading, KillSwitchEngaged, OrderManager
from macrosys.exec.reconcile import daily_reconciliation
from macrosys.exec.runbook import RunbookBlockedError, check_runbook


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``exec`` with submit, poll, reconcile, kill, unkill, status, and runbook-check (spec §11.7)."""
    parser = subparsers.add_parser("exec", help="paper execution: submit, poll, reconcile, kill switch")
    commands = parser.add_subparsers(dest="exec_command", required=True)

    submit = commands.add_parser("submit", help="submit OrderTickets from a JSON file")
    _db_args(submit)
    submit.add_argument("--tickets", type=Path, required=True)
    submit.set_defaults(func=_cmd_submit)

    poll = commands.add_parser("poll", help="apply paper-broker fills and cancels")
    _db_args(poll)
    poll.set_defaults(func=_cmd_poll)

    reconcile = commands.add_parser("reconcile", help="compare the ledger with the paper broker")
    _db_args(reconcile)
    reconcile.add_argument("--tol", type=float, default=0.01, help="absolute USD tolerance")
    reconcile.set_defaults(func=_cmd_reconcile)

    kill = commands.add_parser("kill", help="cancel working orders and block new submits")
    _db_args(kill)
    kill.add_argument("--reason", required=True)
    kill.set_defaults(func=_cmd_kill)

    unkill = commands.add_parser("unkill", help="clear the kill switch after an acknowledgement")
    _db_args(unkill)
    unkill.add_argument("--ack", required=True)
    unkill.set_defaults(func=_cmd_unkill)

    status = commands.add_parser("status", help="print orders, positions, cash, and open incidents")
    _db_args(status)
    status.set_defaults(func=_cmd_status)

    runbook = commands.add_parser("runbook-check", help="block when the runbook hash changed without review")
    runbook.add_argument("--runbook", type=Path, required=True)
    runbook.add_argument("--hash-file", type=Path, required=True)
    runbook.add_argument(
        "--acknowledge-runbook-change",
        action="store_true",
        help="allow this run after a reviewed edit; does not rewrite the hash file",
    )
    runbook.set_defaults(func=_cmd_runbook)


def _db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, required=True, help="SQLite OMS database")
    parser.add_argument("--kill-flag", type=Path, required=True, help="kill-switch flag file")
    parser.add_argument("--broker-state", type=Path, help="paper-broker state file; default is next to --db")
    parser.add_argument("--prices-db", type=Path, help="DuckDB price store; omit to leave MOC orders working")
    parser.add_argument("--slippage-bp", type=float, default=0.0)


def _cmd_submit(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.tickets).read_text(encoding="utf-8"))
    tickets = payload if isinstance(payload, list) else [payload]
    with _session(args) as oms:
        for item in tickets:
            ticket = OrderTicket.model_validate(item)
            try:
                state = oms.submit(ticket)
            except KillSwitchEngaged as exc:
                print(f"killed {exc}")
                return 2
            except HaltTrading as exc:
                print(f"halted {exc}")
                return 2
            print(f"{ticket.client_order_id} {state.value}")
    return 0


def _cmd_poll(args: argparse.Namespace) -> int:
    with _session(args) as oms:
        try:
            events = oms.poll()
        except HaltTrading as exc:
            print(f"halted {exc}")
            return 2
    for event in events:
        print(f"{event.client_order_id} {event.from_state} -> {event.to_state}")
    print(f"events={len(events)}")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    with _session(args) as oms:
        try:
            report = daily_reconciliation(oms, oms.broker, args.tol)
        except HaltTrading as exc:
            print(f"halted {exc}")
            return 2
        notes = [item for item in oms.incidents() if item.failure_class == "B"]
    for note in notes:
        print(f"class-B {note.kind} {note.detail}")
    print("ok" if report.ok else "break")
    return 0 if report.ok else 2


def _cmd_kill(args: argparse.Namespace) -> int:
    switch = KillSwitch(args.kill_flag)
    with _session(args) as oms:
        try:
            switch.engage(args.reason, oms)
        except HaltTrading as exc:
            print(f"halted {exc}")
            return 2
        cancelled = [order for order in oms.orders() if order.state.value == "Cancelled"]
    print(f"engaged cancelled={len(cancelled)}")
    return 0


def _cmd_unkill(args: argparse.Namespace) -> int:
    switch = KillSwitch(args.kill_flag)
    with _session(args) as oms:
        try:
            switch.disengage(args.ack, oms)
        except (ValueError, RuntimeError) as exc:
            print(str(exc))
            return 1
    print("disengaged")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    switch = KillSwitch(args.kill_flag)
    with _session(args) as oms:
        print("kill_switch engaged" if switch.is_engaged() else "kill_switch clear")
        print("halted yes" if oms.halted() else "halted no")
        for order in oms.orders():
            print(
                f"{order.instrument_id} {order.side} {order.quantity:.2f} {order.state.value} "
                f"filled {order.filled_qty:.2f} {order.client_order_id}"
            )
        positions = oms.positions()
        cash = oms.cash()
        print("positions " + (" ".join(f"{key} {value:.2f}" for key, value in sorted(positions.items())) or "flat"))
        print("cash " + (" ".join(f"{key} {value:.2f}" for key, value in sorted(cash.items())) or "flat"))
        open_incidents = [item for item in oms.incidents() if item.resolved_at is None]
        print(f"open_incidents={len(open_incidents)}")
        for item in open_incidents:
            print(f"class-{item.failure_class} {item.kind} {item.detail}")
    return 0


def _cmd_runbook(args: argparse.Namespace) -> int:
    try:
        digest = check_runbook(
            args.runbook, args.hash_file, acknowledge=args.acknowledge_runbook_change
        )
    except RunbookBlockedError as exc:
        print(f"blocked {exc}")
        return 2
    print(digest)
    return 0


@contextmanager
def _session(args: argparse.Namespace) -> Iterator[OrderManager]:
    """Open the OMS and the paper broker, and close the database on the way out."""
    state_path = args.broker_state or Path(args.db).with_suffix(".paper.json")
    broker = PaperBroker(
        _price_source(args.prices_db),
        args.slippage_bp,
        clock=lambda: datetime.now(UTC),
        state_path=state_path,
    )
    oms = OrderManager(args.db, broker, clock=lambda: datetime.now(UTC), kill_switch=KillSwitch(args.kill_flag))
    try:
        yield oms
    finally:
        oms.close()


def _price_source(prices_db: Path | None):
    """Return a ``read_closes``-shaped callable. With no database, no close is published yet."""

    def read(instrument_ids: list[str], start: date, end: date) -> pd.DataFrame:
        if prices_db is None:
            return pd.DataFrame(index=pd.DatetimeIndex([], name="trade_date"))
        import duckdb

        from macrosys.store.prices import PriceStore

        conn = duckdb.connect(str(prices_db), read_only=True)
        try:
            return PriceStore(conn).read_closes(instrument_ids, start, end)
        finally:
            conn.close()

    return read
