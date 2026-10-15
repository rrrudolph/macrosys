"""CLI for the historical case score (spec §10.1 item 2, §10.3).

``cases score`` replays the library and prints sign agreement against the observed outcomes,
split into evidential outcomes (shock sign from the recorded case shock, parameters from the
library) and non-evidential ones (the case author supplied the mechanism's input sign, or the
mechanism ran on replay fixtures for ``TODO_estimate`` parameters). A non-evidential hit is a
formula-direction check. The research promotion gate reads evidential outcomes only and also
requires the dominant competing effect to be on the mechanism in most cases (spec §10.3).
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import duckdb

from macrosys.cases.replay_params import load_replay_parameters, missing_replay_parameters
from macrosys.cases.runner import CaseResult, CaseRunner, CaseSuite
from macrosys.cases.schema import default_cases_dir, load_cases
from macrosys.decide.mandate import Mandate
from macrosys.eval.promotion import ResearchCounts, research_to_paper
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
    score.add_argument(
        "--db",
        type=Path,
        default=None,
        help="read-only store for snapshot inputs; default data/store/macrosys.duckdb when that file exists",
    )
    score.set_defaults(func=_cmd_score)


def _cmd_score(args: argparse.Namespace) -> int:
    """Replay every case and print the sign score (spec §10.1 item 2, §10.3)."""
    case_dir = args.cases if args.cases is not None else default_cases_dir()
    mechanism_dir = args.mechanisms if args.mechanisms is not None else _default_mechanisms()
    held_out = set(args.held_out) if args.held_out else {"supply"}
    library = load_library(mechanism_dir)
    fixtures = load_replay_parameters()
    missing = missing_replay_parameters(library, fixtures)
    if missing:
        print("replay fixtures missing for: " + ", ".join(missing))
        return 1
    store = _open_store(getattr(args, "db", None))
    try:
        report = CaseSuite(CaseRunner(store=store)).run(load_cases(case_dir), library, held_out)
    finally:
        if store is not None:
            store.close()
    results = report.results
    points = sum(item.n_scored for item in results)
    correct = sum(item.n_sign_correct for item in results)
    wrong = sum(item.n_sign_wrong for item in results)
    errors = sum(len(item.errors) for item in results)
    print(f"{'case':<18} {'group':<12} {'scored':>6} {'hit':>5} {'miss':>5} {'open':>5} {'fed':>4} {'fix':>4} {'evid':>5}")
    for item in results:
        print(
            f"{item.case_id:<18} {item.held_out_group:<12} {item.n_scored:6d} "
            f"{item.n_sign_correct:5d} {item.n_sign_wrong:5d} {item.n_unmapped:5d} "
            f"{item.n_author_fed:4d} {item.n_fixture_params:4d} {item.n_evidential:5d}"
        )
        if item.dominant_competing_listed is False:
            print("  competing effect named on the case is not on the mechanism")
        for name in item.unmapped:
            print(f"  open {name}")
        for message in item.errors:
            print(f"  error {message}")
    print("fed: input sign supplied by the case author; fix: ran on replay fixtures; evid: neither")
    print(f"formula-direction agreement {_pct(correct, points)} on {points} outcomes ({correct} hit, {wrong} miss)")
    evidential = sum(item.n_evidential for item in results)
    evidential_correct = sum(item.n_evidential_correct for item in results)
    print(f"evidential agreement {_pct(evidential_correct, evidential)} on {evidential} outcomes")
    held = report.held_out
    print(
        f"held-out {sorted(held_out)}: formula-direction {_pct(held.n_sign_correct, held.n_scored)} "
        f"on {held.n_scored}; evidential {_pct(held.n_evidential_correct, held.n_evidential)} on {held.n_evidential}"
    )
    print(
        "Formula-direction hits show each form has the sign it states. They are not evidence that the "
        "library reaches the historical outcome from the shock, and they do not start paper trading."
    )
    for line in research_gate_lines(args.mandate, results, held_out):
        print(line)
    return 1 if errors else 0


def research_gate_lines(mandate_path: Path | None, results: Sequence[CaseResult], held_out: set[str]) -> list[str]:
    """Evaluate the research-to-paper bar on evidential outcomes only (spec §10.3).

    The spec's exit condition is held-out sign accuracy clearly above chance *with the dominant
    competing effect identified in most cases*. "Most" is read as strictly more than half of the
    cases that name one [J]. A case counts as scored only if it produced at least one evidential
    outcome. When no evidential outcome exists the bar is reported as not evaluable rather than met.
    """
    path = mandate_path if mandate_path is not None else Path("config/mandate.yaml")
    if not path.is_file():
        return []
    gate = Mandate.load(path).promotion_gates.research_to_paper
    held = [item for item in results if item.held_out_group in held_out]
    named = [item for item in results if item.dominant_competing_listed is not None]
    evidential = sum(item.n_evidential for item in results)
    scored_cases = sum(1 for item in results if item.n_evidential > 0)
    result = research_to_paper(
        min_cases_scored=gate.min_cases_scored,
        min_sign_accuracy=gate.min_sign_accuracy,
        counts=ResearchCounts(
            n_scored=sum(item.n_scored for item in results),
            n_evidential=evidential,
            n_evidential_correct=sum(item.n_evidential_correct for item in results),
            n_cases_with_evidential=scored_cases,
            held_out_evidential=sum(item.n_evidential for item in held),
            held_out_evidential_correct=sum(item.n_evidential_correct for item in held),
            n_cases_naming_competing=len(named),
            n_cases_competing_listed=sum(1 for item in named if item.dominant_competing_listed),
        ),
    )
    lines: list[str] = []
    if not result.evaluable:
        lines.append("research sign bar: not evaluable")
        lines.extend(f"  {reason}" for reason in result.reasons)
    elif not result.met:
        lines.append("research sign bar: not met")
        lines.extend(f"  {reason}" for reason in result.reasons)
    else:
        bar = "unset" if gate.min_sign_accuracy is None else f"{gate.min_sign_accuracy:.0%}"
        lines.append(
            f"research sign bar: met ({scored_cases} cases, {evidential} evidential outcomes, held-out bar {bar})"
        )
    lines.append("paper orders are not started by this command")
    return lines


def _pct(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{numerator / denominator:.0%}"


def _default_mechanisms() -> Path:
    return Path(__file__).resolve().parents[2] / "mechanisms"


def _open_store(path: Path | None) -> duckdb.DuckDBPyConnection | None:
    """Open the observation store read-only for cutoff snapshots (spec §9.2).

    An explicit path is opened as given. The default path is used only when that file exists,
    so a checkout without the live store still replays on the case fixtures.
    """
    if path is not None:
        return duckdb.connect(str(path), read_only=True)
    candidate = Path("data/store/macrosys.duckdb")
    if not candidate.is_file():
        return None
    return duckdb.connect(str(candidate), read_only=True)
