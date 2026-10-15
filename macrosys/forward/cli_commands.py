"""CLI for the prospective weekly decision (spec §11.2, §10.2 item 7, §10.3).

The CLI package owns ``macrosys.cli``. Call ``register_cli(subparsers)`` from there. This command
does not place an order. The owner runs:

    macrosys forward week --config config/pipeline.yaml --as-of YYYY-MM-DD --shocks config/open_shocks.yaml --triggers config/triggers.yaml --mandate config/mandate.yaml --ledger data/forward/ledger.jsonl --week-log data/forward/weeks.jsonl --out data/forward/<date>
    macrosys forward status --week-log data/forward/weeks.jsonl
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from macrosys.decide.approve import StoreApprover
from macrosys.decide.mandate import Mandate
from macrosys.exec.runbook import RunbookBlockedError
from macrosys.forward.week import DuplicateWeekError, forward_week, week_status_text
from macrosys.pipeline.jobs import load_config


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``forward week`` and ``forward status`` (spec §11.2, §10.3)."""
    forward = subparsers.add_parser("forward", help="prospective weekly decision; does not place an order")
    commands = forward.add_subparsers(dest="forward_cmd")

    week = commands.add_parser("week", help="refresh, gate, and freeze one week")
    week.add_argument("--config", type=Path, required=True)
    week.add_argument("--as-of", required=True, dest="as_of", help="YYYY-MM-DD")
    week.add_argument("--shocks", type=Path, required=True)
    week.add_argument(
        "--triggers",
        type=Path,
        default=None,
        help="trigger rules YAML; shocks they raise at as-of are decided next to the filed ones",
    )
    week.add_argument("--mandate", type=Path, required=True)
    week.add_argument(
        "--universe",
        type=Path,
        default=None,
        help="instrument master for the approver; defaults to the mandate's universe_file",
    )
    week.add_argument(
        "--no-approver",
        action="store_true",
        dest="no_approver",
        help="leave the exposure check pending (forecast only, no position can be approved)",
    )
    week.add_argument("--ledger", type=Path, required=True)
    week.add_argument("--week-log", type=Path, required=True, dest="week_log")
    week.add_argument("--out", type=Path, required=True)
    week.add_argument("--runbook", type=Path, default=None)
    week.add_argument(
        "--acknowledge-runbook-change",
        action="store_true",
        help="allow this week when docs/runbook.md no longer matches the recorded hash",
    )
    week.set_defaults(func=_cmd_week)

    status = commands.add_parser("status", help="count finished weeks on the dry-run clock")
    status.add_argument("--week-log", type=Path, required=True, dest="week_log")
    status.set_defaults(func=_cmd_status)


def _cmd_week(args: argparse.Namespace) -> int:
    try:
        as_of = date.fromisoformat(str(args.as_of))
    except ValueError:
        print("as-of must be YYYY-MM-DD", file=sys.stderr)
        return 2
    config = load_config(args.config)
    runbook = args.runbook if args.runbook is not None else Path("docs/runbook.md")
    universe = args.universe if args.universe is not None else _universe_from_mandate(args.mandate)
    approver = None if args.no_approver else StoreApprover(config, universe)
    try:
        result = forward_week(
            config.db_path,
            as_of,
            pipeline_config=args.config,
            shocks_path=args.shocks,
            mandate_path=args.mandate,
            ledger_path=args.ledger,
            week_log_path=args.week_log,
            out_dir=args.out,
            runbook_path=runbook,
            acknowledge_runbook_change=bool(args.acknowledge_runbook_change),
            triggers_path=args.triggers,
            approver=approver,
        )
    except DuplicateWeekError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except RunbookBlockedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    allowed = "yes" if result.paper_allowed else "no"
    print(
        f"{result.status} shocks={result.n_shocks} triggered={len(result.triggered)} "
        f"theses={result.n_theses} paper_allowed={allowed}"
    )
    print(result.report_path)
    if result.status == "error":
        return 1
    if result.status == "blocked":
        return 2
    return 0


def _universe_from_mandate(mandate_path: Path) -> Path:
    """The mandate's ``universe_file``, resolved against the repo root when relative."""
    universe = Path(Mandate.load(mandate_path).universe_file)
    if universe.is_absolute() or universe.is_file():
        return universe
    return Path(__file__).resolve().parents[2] / universe


def _cmd_status(args: argparse.Namespace) -> int:
    print(week_status_text(args.week_log), end="")
    return 0
