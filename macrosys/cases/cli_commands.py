"""CLI for the historical case score (spec §10.1 item 2, §10.3).

``cases score`` replays the library with the operating sign ranges and prints sign agreement
against the observed outcomes. The weekly decision job does not use those ranges. A high sign
score here is a direction check. It does not start paper trading.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from macrosys.cases.operating import load_operating_parameters, missing_operating_parameters
from macrosys.cases.runner import CaseSuite
from macrosys.cases.schema import default_cases_dir, load_cases
from macrosys.decide.mandate import Mandate
from macrosys.mechanisms.library import load_library


def register_cli(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add ``cases score`` (spec §10.1 item 2)."""
    cases = subparsers.add_parser("cases", help="historical case replay")
    commands = cases.add_subparsers(dest="cases_cmd")
    score = commands.add_parser("score", help="replay the case library and print sign agreement")
    score.add_argument("--cases", type=Path, default=None, help="directory of case yaml files")
    score.add_argument("--mechanisms", type=Path, default=None, help="mechanism library directory")
    score.add_argument("--held-out", action="append", default=None, help="held-out group; repeatable")
    score.add_argument("--mandate", type=Path, default=None, help="mandate yaml; default config/mandate.yaml if present")
    score.set_defaults(func=_cmd_score)


def _cmd_score(args: argparse.Namespace) -> int:
    """Replay every case and print the sign score (spec §10.1 item 2, §10.3)."""
    case_dir = args.cases if args.cases is not None else default_cases_dir()
    mechanism_dir = args.mechanisms if args.mechanisms is not None else _default_mechanisms()
    held_out = set(args.held_out) if args.held_out else {"supply"}
    library = load_library(mechanism_dir)
    operating = load_operating_parameters()
    missing = missing_operating_parameters(library, operating)
    if missing:
        print("operating parameters missing: " + ", ".join(missing))
        return 1
    report = CaseSuite().run(load_cases(case_dir), library, held_out)
    points = sum(item.n_scored for item in report.results)
    correct = sum(item.n_sign_correct for item in report.results)
    wrong = sum(item.n_sign_wrong for item in report.results)
    errors = sum(len(item.errors) for item in report.results)
    print(f"{'case':<18} {'group':<12} {'scored':>6} {'hit':>5} {'miss':>5} {'open':>5}")
    for item in report.results:
        print(
            f"{item.case_id:<18} {item.held_out_group:<12} {item.n_scored:6d} "
            f"{item.n_sign_correct:5d} {item.n_sign_wrong:5d} {item.n_unmapped:5d}"
        )
        if item.dominant_competing_listed is False:
            print("  competing effect named on the case is not on the mechanism")
        for name in item.unmapped:
            print(f"  open {name}")
        for message in item.errors:
            print(f"  error {message}")
    accuracy = (correct / points) if points else None
    accuracy_text = "n/a" if accuracy is None else f"{accuracy:.0%}"
    print(f"sign agreement {accuracy_text} on {points} outcomes ({correct} hit, {wrong} miss)")
    held_points = report.held_out.n_scored
    held_accuracy = (report.held_out.n_sign_correct / held_points) if held_points else None
    held_text = "n/a" if held_accuracy is None else f"{held_accuracy:.0%}"
    print(f"held-out {sorted(held_out)} sign agreement {held_text} on {held_points} outcomes")
    print(
        "These ranges keep each formula's sign. They are not fitted magnitudes. "
        "The weekly job does not load them, and this score does not start paper trading."
    )
    _print_gate(args.mandate, report.results, accuracy, held_accuracy)
    return 1 if errors else 0


def _print_gate(mandate_path: Path | None, results, accuracy: float | None, held_accuracy: float | None) -> None:
    path = mandate_path if mandate_path is not None else Path("config/mandate.yaml")
    if not path.is_file():
        return
    gate = Mandate.load(path).promotion_gates.research_to_paper
    scored_cases = sum(1 for item in results if item.n_scored > 0)
    reasons: list[str] = []
    if scored_cases < gate.min_cases_scored:
        reasons.append(f"{scored_cases} cases produced a sign, the bar is {gate.min_cases_scored}")
    if gate.min_sign_accuracy is not None:
        if held_accuracy is None:
            reasons.append("held-out split produced no signs")
        elif held_accuracy < gate.min_sign_accuracy:
            reasons.append(f"held-out agreement {held_accuracy:.0%} is under {gate.min_sign_accuracy:.0%}")
        if accuracy is None:
            reasons.append("no signs were scored")
        elif accuracy < gate.min_sign_accuracy:
            reasons.append(f"overall agreement {accuracy:.0%} is under {gate.min_sign_accuracy:.0%}")
    if reasons:
        print("research sign bar: not met")
        for reason in reasons:
            print(f"  {reason}")
    else:
        bar = "unset" if gate.min_sign_accuracy is None else f"{gate.min_sign_accuracy:.0%}"
        print(f"research sign bar: met ({scored_cases} cases, held-out bar {bar})")
    print("paper clock: not started")


def _default_mechanisms() -> Path:
    return Path(__file__).resolve().parents[2] / "mechanisms"
