"""CLI for the point-in-time walk-forward (spec §10.2).

The CLI package owns ``macrosys.cli``. Call ``register_cli(subparsers)`` from there. The store is
opened read-only. The approver is ``macrosys.decide.approve.StoreApprover`` on the mandate's
universe file; ``--no-approver`` approves nothing, so a passing gate still holds the resting book.

    macrosys eval walkforward --config config/pipeline.yaml --mandate config/mandate.yaml --replay data/triggers/replay.jsonl --from 2010-01-07 --to 2026-10-15 --out data/eval/walkforward --cost-bp 5
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb

from macrosys.decide.approve import StoreApprover
from macrosys.decide.mandate import Mandate
from macrosys.eval.walkforward import WalkForwardReport, run
from macrosys.pipeline.jobs import load_config


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``eval walkforward`` (spec §10.2)."""
    evaluation = subparsers.add_parser("eval", help="walk-forward evaluation against dumb baselines")
    commands = evaluation.add_subparsers(dest="eval_cmd")
    walk = commands.add_parser("walkforward", help="score the weekly book on replayed trigger shocks")
    walk.add_argument("--config", type=Path, required=True)
    walk.add_argument("--mandate", type=Path, required=True)
    walk.add_argument("--replay", type=Path, required=True)
    walk.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD, first decision date")
    walk.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD, last date included")
    walk.add_argument("--out", type=Path, required=True)
    walk.add_argument("--cost-bp", type=float, default=5.0, dest="cost_bp")
    walk.add_argument("--shocks", type=Path, default=None, help="optional hand-filed shocks YAML")
    walk.add_argument(
        "--universe",
        type=Path,
        default=None,
        help="instrument master for the approver; defaults to the mandate's universe_file",
    )
    walk.add_argument(
        "--no-approver",
        action="store_true",
        dest="no_approver",
        help="approve nothing: a passing gate still holds the resting book",
    )
    walk.set_defaults(func=_cmd_walkforward)


def _cmd_walkforward(args: argparse.Namespace) -> int:
    try:
        start = date.fromisoformat(str(args.start))
        end = date.fromisoformat(str(args.end))
    except ValueError:
        print("from and to must be YYYY-MM-DD", file=sys.stderr)
        return 2
    config = load_config(args.config)
    mandate = Mandate.load(args.mandate)
    if args.no_approver:
        approver = None
    else:
        universe = args.universe if args.universe is not None else _resolve(mandate.universe_file)
        approver = StoreApprover(config, universe)
    conn = duckdb.connect(str(config.db_path), read_only=True)
    try:
        result = run(
            conn,
            mandate,
            config,
            args.replay,
            start,
            end,
            approver=approver,
            shocks_path=args.shocks,
            cost_bp=float(args.cost_bp),
            out_dir=args.out,
        )
    finally:
        conn.close()
    _print_report(result.report)
    print(args.out / "walkforward.json")
    return 0


def _resolve(universe_file: str) -> Path:
    """A relative ``universe_file`` on the mandate is read against the repo root."""
    path = Path(universe_file)
    if path.is_absolute() or path.is_file():
        return path
    return Path(__file__).resolve().parents[2] / path


def _print_report(report: WalkForwardReport) -> None:
    gates = ", ".join(f"{name}={count}" for name, count in report.failing_gates.items()) or "none"
    print(
        f"weeks={report.n_weeks} blocked={report.n_blocked} shocks={report.n_shocks} "
        f"theses={report.n_theses} trades={report.n_trades} sized={report.n_sized}"
    )
    print(f"failing gates: {gates}")
    print(f"{'book':<14} {'ann%':>8} {'vol%':>8} {'sharpe':>8} {'maxDD%':>8} {'turn%':>8} {'effN':>8}")
    for name, score in report.books.items():
        print(
            f"{name:<14} {_pct(score.ann_return.value):>8} {_pct(score.vol.value):>8} "
            f"{_num(score.sharpe.value if score.sharpe is not None else None):>8} "
            f"{_pct(score.max_drawdown.value):>8} {_pct(score.turnover.value):>8} "
            f"{_num(score.effective_n):>8}"
        )


def _pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:.2f}"


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"
