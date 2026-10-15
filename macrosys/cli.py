"""Command-line entry point. Subcommands are registered by packages as they land (spec §13)."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from macrosys.skeleton.demos import run_demo, shock_by_name
from macrosys.skeleton.run import run_once


def main(argv: list[str] | None = None) -> int:
    """Dispatch a macrosys subcommand (spec §13). Returns a process status code."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from macrosys import __version__

        print(__version__)
        return 0
    if args.cmd == "skeleton":
        return _skeleton(args)
    if args.cmd == "ledger":
        return _ledger(args)
    if args.cmd == "library":
        return _library(args)
    func = getattr(args, "func", None)
    if callable(func):
        return int(func(args))
    parser.print_help()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="macrosys", description="Global macro research and trading system.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("version", help="print version")

    skeleton = sub.add_parser("skeleton", help="walking-skeleton decision loop (spec §13 S0.1, S0.2)")
    sk = skeleton.add_subparsers(dest="skeleton_cmd")
    run_p = sk.add_parser("run", help="one shock through the stub decision loop")
    run_p.add_argument("--shock", required=True, choices=["demand", "fx", "transport"])
    run_p.add_argument("--as-of", required=True, dest="as_of", help="YYYY-MM-DD")
    run_p.add_argument("--out", required=True, help="directory for the report, manifest, and ledger")
    run_p.add_argument("--refresh", action="store_true", help="derive world state from recipes")
    run_p.add_argument(
        "--acknowledge-runbook-change",
        action="store_true",
        help="allow this run when docs/runbook.md no longer matches the recorded hash",
    )
    demo_p = sk.add_parser("demo", help="run the three transfer shocks and write an index")
    demo_p.add_argument("--out", required=True)

    ledger = sub.add_parser("ledger", help="forecast ledger")
    led = ledger.add_subparsers(dest="ledger_cmd")
    verify = led.add_parser("verify", help="recompute the hash chain (spec §10.2)")
    verify.add_argument("path")

    library = sub.add_parser("library", help="mechanism library")
    lib = library.add_subparsers(dest="library_cmd")
    lib.add_parser("list", help="print id, class, and confidence")
    lib.add_parser("validate", help="load the library and report the version hash")

    # Optional command groups register themselves (each package owns its own commands).
    for module in (
        "macrosys.pipeline.cli_commands",
        "macrosys.exec.cli_commands",
        "macrosys.cases.cli_commands",
        "macrosys.events.cli_commands",
    ):
        try:
            register = __import__(module, fromlist=["register_cli"]).register_cli
        except ImportError:
            continue
        register(sub)
    return parser


def _skeleton(args: argparse.Namespace) -> int:
    cmd = args.skeleton_cmd
    if cmd not in ("run", "demo"):
        print("usage: macrosys skeleton {run,demo} ...", file=sys.stderr)
        return 2
    out = Path(args.out)
    if cmd == "demo":
        results = run_demo(out)
        for result in results:
            print(f"{result.shock_id} {result.decision.action} {result.digest}")
        return 0
    try:
        as_of = date.fromisoformat(str(args.as_of))
    except ValueError:
        print("as-of must be YYYY-MM-DD", file=sys.stderr)
        return 2
    shock = shock_by_name(str(args.shock))
    result = run_once(
        shock,
        as_of,
        ledger_path=out / "ledger.jsonl",
        out_dir=out,
        refresh_world=bool(args.refresh),
        acknowledge_runbook_change=bool(args.acknowledge_runbook_change),
    )
    print(result.decision.action)
    print(result.digest)
    return 0


def _ledger(args: argparse.Namespace) -> int:
    if args.ledger_cmd != "verify":
        print("usage: macrosys ledger verify PATH", file=sys.stderr)
        return 2
    from macrosys.eval.ledger import ForecastLedger

    path = Path(args.path)
    if not path.is_file():
        print(f"ledger not found: {path}", file=sys.stderr)
        return 1
    ok = ForecastLedger(path).verify()
    print("ok" if ok else "failed")
    return 0 if ok else 1


def _library(args: argparse.Namespace) -> int:
    from macrosys.mechanisms import load_library

    cmd = args.library_cmd
    root = _mechanisms_dir()
    if cmd == "list":
        library = load_library(root)
        for mechanism_id in library.ids():
            spec = library.get(mechanism_id)
            print(f"{spec.id}\t{spec.class_}\t{spec.confidence}")
        return 0
    if cmd == "validate":
        try:
            library = load_library(root)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"ok {len(library)} {library.version}")
        return 0
    print("usage: macrosys library {list,validate}", file=sys.stderr)
    return 2


def _mechanisms_dir() -> Path:
    """Library directory next to this package, else ``mechanisms`` in the working directory."""
    bundled = Path(__file__).resolve().parents[1] / "mechanisms"
    if bundled.is_dir():
        return bundled
    return Path("mechanisms")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
