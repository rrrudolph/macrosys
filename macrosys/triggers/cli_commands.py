"""CLI for data-filed shocks (owner decision 2026-10-15).

The CLI package owns ``macrosys.cli``. Call ``register_cli(subparsers)`` from there. Neither command
writes to the store or files a shock for the weekly job; ``scan`` prints what would fire today and
``replay`` writes the point-in-time history of fires to a JSONL file.

    macrosys triggers scan --config config/pipeline.yaml --triggers config/triggers.yaml --as-of YYYY-MM-DD
    macrosys triggers replay --config config/pipeline.yaml --triggers config/triggers.yaml --from YYYY-MM-DD --to YYYY-MM-DD --out data/triggers/replay.jsonl
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path

import duckdb

from macrosys.pipeline.jobs import load_config
from macrosys.triggers.rules import load_triggers
from macrosys.triggers.scan import StoreReader, replay, scan, write_replay

# Same end-of-day cutoff as ``macrosys.skeleton.run.as_of_datetime``.
_CUTOFF = time(23, 59, 59)


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``triggers scan`` and ``triggers replay``."""
    triggers = subparsers.add_parser("triggers", help="data-filed shocks: scan today or replay history")
    commands = triggers.add_subparsers(dest="triggers_cmd")

    scan_p = commands.add_parser("scan", help="print the rules that fire at a date")
    _common(scan_p)
    scan_p.add_argument("--as-of", required=True, dest="as_of", help="YYYY-MM-DD")
    scan_p.set_defaults(func=_cmd_scan)

    replay_p = commands.add_parser("replay", help="scan weekly across a date range and write JSONL")
    _common(replay_p)
    replay_p.add_argument("--from", required=True, dest="start", help="YYYY-MM-DD")
    replay_p.add_argument("--to", required=True, dest="end", help="YYYY-MM-DD")
    replay_p.add_argument("--out", type=Path, required=True)
    replay_p.add_argument("--step-days", type=int, default=7, dest="step_days")
    replay_p.set_defaults(func=_cmd_replay)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True, help="pipeline config with db_path and prices")
    parser.add_argument("--triggers", type=Path, required=True, help="trigger rules YAML")


def _cmd_scan(args: argparse.Namespace) -> int:
    day = _day(args.as_of)
    if day is None:
        return 2
    config = load_config(args.config)
    rules = load_triggers(args.triggers)
    conn = duckdb.connect(str(config.db_path), read_only=True)
    try:
        fired = scan(
            StoreReader(conn, config.price_instrument_id), rules, datetime.combine(day, _CUTOFF, tzinfo=UTC)
        )
    finally:
        conn.close()
    if not fired:
        print(f"no trigger fires at {day.isoformat()} ({len(rules.rules)} rules checked)")
        return 0
    for item in fired:
        print(item.sentence())
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    start, end = _day(args.start), _day(args.end)
    if start is None or end is None:
        return 2
    config = load_config(args.config)
    rules = load_triggers(args.triggers)
    conn = duckdb.connect(str(config.db_path), read_only=True)
    try:
        runs = replay(
            StoreReader(conn, config.price_instrument_id), rules, start, end, step_days=args.step_days
        )
        counts = write_replay(args.out, runs)
    finally:
        conn.close()
    total = sum(counts.values())
    print(f"{total} fires from {start.isoformat()} to {end.isoformat()} written to {args.out}")
    for rule in rules.rules:
        print(f"{rule.id}\t{counts.get(rule.id, 0)}")
    return 0


def _day(text: str) -> date | None:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        print("dates must be YYYY-MM-DD", file=sys.stderr)
        return None
