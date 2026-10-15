"""CLI for local-projection coefficients (spec §8.4).

    macrosys estimate local --db data/store/macrosys.duckdb --write-cards
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from macrosys.estimate.local import (
    default_config_path,
    default_output_path,
    install_supported,
    load_config,
    run_estimates,
    write_report,
)


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``estimate local`` (spec §8.4)."""
    estimate = subparsers.add_parser("estimate", help="estimate mechanism coefficients from the store")
    commands = estimate.add_subparsers(dest="estimate_cmd")
    local = commands.add_parser("local", help="local projections; install only stable intervals")
    local.add_argument("--db", type=Path, required=True)
    local.add_argument("--config", type=Path, default=None)
    local.add_argument("--out", type=Path, default=None)
    local.add_argument("--as-of", default=None, help="ISO timestamp; a naive value is read as UTC")
    local.add_argument(
        "--write-cards",
        action="store_true",
        help="replace TODO_estimate on cards whose interval passed",
    )
    local.add_argument("--mechanisms", type=Path, default=None)
    local.set_defaults(func=_cmd_local)


def _cmd_local(args: argparse.Namespace) -> int:
    config_path = args.config if args.config is not None else default_config_path()
    out = args.out if args.out is not None else default_output_path()
    as_of = _aware(args.as_of) if args.as_of else datetime.now(UTC)
    config = load_config(config_path)
    conn = duckdb.connect(str(args.db), read_only=True)
    try:
        report = run_estimates(conn, config, as_of)
    finally:
        conn.close()
    write_report(out, report)
    installed = [item for item in report.results if item.status == "installed"]
    rejected = [item for item in report.results if item.status == "rejected"]
    print(f"installed {len(installed)} rejected {len(rejected)} unavailable {len(report.results) - len(installed) - len(rejected)}")
    for item in report.results:
        if item.status == "unavailable":
            continue
        span = "" if item.low is None else f" [{item.low}, {item.high}]"
        print(f"{item.status} {item.mechanism}.{item.parameter}{span} {item.reason}")
    if args.write_cards and installed:
        root = args.mechanisms if args.mechanisms is not None else Path("mechanisms")
        changed = install_supported(report, root)
        print("cards " + ", ".join(changed))
    return 0


def _aware(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
