"""Replay fixtures cover every library refusal, cannot smuggle magnitudes, and the gate reads only evidence (spec §10.1, §10.3)."""

from pathlib import Path

import pytest

from macrosys.cases.cli_commands import _cmd_score, research_gate_lines
from macrosys.cases.replay_params import load_replay_parameters, missing_replay_parameters
from macrosys.cases.runner import CaseResult, CaseSuite
from macrosys.cases.schema import load_cases
from macrosys.mechanisms.library import load_library

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = load_library(ROOT / "mechanisms")


def test_replay_fixtures_cover_every_todo_parameter():
    assert missing_replay_parameters(LIBRARY, load_replay_parameters()) == []


def test_replay_fixtures_are_unit_signs_or_explained_levels():
    for mechanism_id, params in load_replay_parameters().items():
        for name, quantity in params.items():
            assert quantity.provenance in ("case_fixture:replay_sign", "case_fixture:replay_level"), f"{mechanism_id}.{name}"
            if quantity.provenance == "case_fixture:replay_sign":
                assert quantity.value in (1.0, -1.0), f"{mechanism_id}.{name} is a sign fixture with a magnitude"


@pytest.mark.parametrize(
    "entry",
    [
        "{role: sign, value: 0.5, unit: ratio}",
        "{role: sign, value: 1.0, unit: ratio, why: no}",
        "{role: level, value: 0.3, unit: share}",
        "{role: estimate, value: 1.0, unit: ratio}",
        "{low: 0.2, high: 1.0, unit: ratio}",
    ],
)
def test_loader_rejects_magnitudes_dressed_as_fixtures(tmp_path, entry):
    path = tmp_path / "replay_parameters.yaml"
    path.write_text(f"parameters:\n  B11_monetary_transmission:\n    multiplier: {entry}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_replay_parameters(path)


def test_case_replay_has_no_crashes_and_labels_every_outcome():
    report = CaseSuite().run(load_cases(ROOT / "cases"), LIBRARY, {"supply"})
    assert all(item.errors == [] for item in report.results)
    assert all(item.n_scored > 0 for item in report.results)
    for item in report.results:
        assert item.n_evidential + item.n_author_fed + item.n_fixture_params >= item.n_scored


def _result(case_id: str, group: str, *, scored: int, correct: int, evidential: int, evidential_correct: int, dominant: bool | None) -> CaseResult:
    from datetime import UTC, datetime

    return CaseResult(
        case_id=case_id,
        as_of=datetime(2020, 1, 1, tzinfo=UTC),
        held_out_group=group,
        n_blocked=0,
        n_scored=scored,
        n_sign_correct=correct,
        n_sign_wrong=scored - correct,
        sign_accuracy=correct / scored if scored else None,
        rank_correlation=None,
        horizon_error=None,
        dominant_competing_listed=dominant,
        refresh_skipped=0,
        refresh_written=0,
        n_evidential=evidential,
        n_evidential_correct=evidential_correct,
        n_fixture_params=scored - evidential,
    )


def test_gate_is_not_evaluable_when_no_outcome_is_evidential():
    results = [_result(f"c{i}", "supply", scored=1, correct=1, evidential=0, evidential_correct=0, dominant=True) for i in range(12)]
    lines = research_gate_lines(ROOT / "config" / "mandate.yaml", results, {"supply"})
    assert lines[0] == "research sign bar: not evaluable"
    assert "formula direction only" in lines[1]


def test_gate_requires_dominant_competing_in_most_cases():
    results = [
        _result(f"c{i}", "supply" if i % 2 else "demand", scored=1, correct=1, evidential=1, evidential_correct=1, dominant=False)
        for i in range(12)
    ]
    lines = research_gate_lines(ROOT / "config" / "mandate.yaml", results, {"supply"})
    assert lines[0] == "research sign bar: not met"
    assert any("dominant competing effect" in line for line in lines)


def test_gate_met_only_on_evidential_signs_with_competing_listed():
    results = [
        _result(f"c{i}", "supply" if i % 2 else "demand", scored=1, correct=1, evidential=1, evidential_correct=1, dominant=True)
        for i in range(12)
    ]
    lines = research_gate_lines(ROOT / "config" / "mandate.yaml", results, {"supply"})
    assert lines[0].startswith("research sign bar: met")


class _Args:
    def __init__(self) -> None:
        self.cases = ROOT / "cases"
        self.mechanisms = ROOT / "mechanisms"
        self.held_out = ["supply"]
        self.mandate = ROOT / "config" / "mandate.yaml"


def test_score_command_reports_evidential_split_and_does_not_claim_the_bar(capsys):
    assert _cmd_score(_Args()) == 0
    out = capsys.readouterr().out
    assert "formula-direction agreement" in out
    assert "evidential agreement" in out
    assert "do not start paper trading" in out
    assert "research sign bar: met" not in out
