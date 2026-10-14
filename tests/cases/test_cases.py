"""Case schema, replay, and the held-out split (spec §10.1 items 1–2)."""

from pathlib import Path
from urllib.parse import urlsplit

from macrosys.cases.runner import CaseRunner, CaseSuite
from macrosys.cases.schema import load_cases
from macrosys.mechanisms.library import load_library

CASES = load_cases(Path(__file__).resolve().parents[2] / "cases")
LIBRARY = load_library(Path(__file__).resolve().parents[2] / "mechanisms")


def test_case_files_validate():
    assert len(CASES) >= 12
    ids = [case.case_id for case in CASES]
    assert len(ids) == len(set(ids))
    for case in CASES:
        assert "case_fixture" in case.notes
        assert case.shock.size.provenance == "case_fixture"
        for point in case.expected_structure:
            assert point.sign in (-1, 0, 1)
        for point in case.observed_structure:
            assert point.sign in (-1, 0, 1)
            parsed = urlsplit(point.source_url)
            assert parsed.scheme in {"http", "https"}
            assert parsed.netloc


def test_replay_scores_fiscal_and_blocks_cny_without_counting_blocks_as_misses():
    by_id = {case.case_id: case for case in CASES}
    runner = CaseRunner()
    fiscal = runner.replay(by_id["us_fiscal_2021"], LIBRARY)
    cny = runner.replay(by_id["cny_2015"], LIBRARY)
    assert fiscal.n_scored >= 1
    assert fiscal.n_sign_correct >= 1
    assert fiscal.n_sign_wrong == 0
    assert fiscal.n_blocked >= 1
    assert any(item.parameter == "multiplier" for item in fiscal.blocked)
    assert fiscal.rank_correlation is None
    assert cny.n_blocked >= 1
    assert cny.n_sign_wrong == 0
    assert any(item.parameter == "pt_import" for item in cny.blocked)
    assert cny.rank_correlation is None


def test_held_out_split_adds_up():
    report = CaseSuite().run(CASES, LIBRARY, {"supply"})
    assert report.held_out.n_cases + report.development.n_cases == len(report.results)
    assert sum(group.n_cases for group in report.by_group) == len(report.results)
    assert sum(group.n_blocked for group in report.by_group) == (
        report.held_out.n_blocked + report.development.n_blocked
    )
    assert sum(group.n_scored for group in report.by_group) == (
        report.held_out.n_scored + report.development.n_scored
    )
    assert sum(group.n_sign_correct for group in report.by_group) == (
        report.held_out.n_sign_correct + report.development.n_sign_correct
    )
    assert sum(group.n_sign_wrong for group in report.by_group) == (
        report.held_out.n_sign_wrong + report.development.n_sign_wrong
    )
    assert report.held_out.n_cases == sum(1 for case in CASES if case.held_out_group == "supply")
