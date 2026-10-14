"""Operating sign ranges cover every library refusal and the score command runs (spec §10.1)."""

from pathlib import Path

from macrosys.cases.cli_commands import _cmd_score
from macrosys.cases.operating import load_operating_parameters, missing_operating_parameters
from macrosys.cases.runner import CaseSuite
from macrosys.cases.schema import load_cases
from macrosys.mechanisms.library import load_library

ROOT = Path(__file__).resolve().parents[2]


def test_operating_ranges_cover_every_todo_parameter():
    library = load_library(ROOT / "mechanisms")
    missing = missing_operating_parameters(library, load_operating_parameters())
    assert missing == []


def test_case_replay_has_no_crashes_and_scores_every_case():
    library = load_library(ROOT / "mechanisms")
    report = CaseSuite().run(load_cases(ROOT / "cases"), library, {"supply"})
    assert all(item.errors == [] for item in report.results)
    assert all(item.n_scored > 0 for item in report.results)
    assert sum(item.n_sign_wrong for item in report.results) == 0


class _Args:
    def __init__(self) -> None:
        self.cases = ROOT / "cases"
        self.mechanisms = ROOT / "mechanisms"
        self.held_out = ["supply"]
        self.mandate = ROOT / "config" / "mandate.yaml"


def test_score_command_exits_clean(capsys):
    assert _cmd_score(_Args()) == 0
    out = capsys.readouterr().out
    assert "sign agreement" in out
    assert "does not start paper trading" in out
