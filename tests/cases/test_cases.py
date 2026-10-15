"""Case schema, replay, and the held-out split (spec §10.1 items 1–2).

These tests check that the harness works: a wrong sign is counted as a miss, blocked is not a
miss, duplicate write-ups are one fact, and non-evidential outcomes are labelled. They do not
assert that the library is right about history; that is the score's job, not the test's.
"""

from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from macrosys.cases.runner import CaseRunner, CaseSuite, read_input_level
from macrosys.cases.schema import FixtureLevel, load_cases
from macrosys.mechanisms.library import load_library
from macrosys.store.observations import ObservationStore
from macrosys.store.schema import create_schema
from tests.store.factories import obs

CASES = load_cases(Path(__file__).resolve().parents[2] / "cases")
LIBRARY = load_library(Path(__file__).resolve().parents[2] / "mechanisms")
BY_ID = {case.case_id: case for case in CASES}


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


def test_replay_runs_fiscal_and_cny_without_errors():
    runner = CaseRunner()
    fiscal = runner.replay(BY_ID["us_fiscal_2021"], LIBRARY)
    cny = runner.replay(BY_ID["cny_2015"], LIBRARY)
    assert fiscal.n_scored >= 1
    assert fiscal.errors == []
    assert fiscal.rank_correlation is None
    assert cny.n_scored >= 1
    assert cny.errors == []
    assert cny.rank_correlation is None
    # B16's pass-through coefficients are replay fixtures, so the CNY outcome is not evidential.
    assert cny.n_fixture_params == cny.n_scored
    assert cny.n_evidential == 0


def test_replay_without_fixtures_blocks_cny_and_does_not_count_a_miss():
    cny = CaseRunner(replay_parameters=False).replay(BY_ID["cny_2015"], LIBRARY)
    assert cny.n_blocked >= 1
    assert cny.n_scored == 0
    assert cny.n_sign_wrong == 0
    assert any(item.parameter == "pt_import" for item in cny.blocked)


def test_flipped_observed_sign_is_counted_as_a_miss():
    case = BY_ID["us_fiscal_2021"]
    flipped_points = [
        point.model_copy(update={"sign": -point.sign}) if point.sign != 0 else point
        for point in case.observed_structure
    ]
    flipped = case.model_copy(update={"observed_structure": flipped_points})
    original = CaseRunner().replay(case, LIBRARY)
    result = CaseRunner().replay(flipped, LIBRARY)
    assert result.n_scored == original.n_scored
    assert result.n_sign_wrong == original.n_sign_correct
    assert result.n_sign_correct == original.n_sign_wrong


def test_author_fed_input_sign_is_not_evidential():
    oil = CaseRunner().replay(BY_ID["oil_2014"], LIBRARY)
    # B03 receives a hand-set negative demand gap and B05 a hand-set pipeline; neither is the recorded shock.
    assert oil.n_scored >= 2
    assert oil.n_author_fed == oil.n_scored
    assert oil.n_evidential == 0


def test_duplicate_write_ups_of_one_outcome_score_once():
    case = BY_ID["us_fiscal_2021"]
    doubled = case.model_copy(update={"observed_structure": list(case.observed_structure) * 2})
    once = CaseRunner().replay(case, LIBRARY)
    twice = CaseRunner().replay(doubled, LIBRARY)
    assert twice.n_scored == once.n_scored


def test_snapshot_input_ignores_a_reading_published_after_the_cutoff():
    cutoff = datetime(2020, 6, 30, 23, 59, 59, tzinfo=UTC)
    level = FixtureLevel(value=0.85, unit="share", series_id="FRED:TCU", scale=0.01)
    conn = duckdb.connect(database=":memory:")
    try:
        create_schema(conn)
        ObservationStore(conn).append(
            [
                obs(
                    "FRED:TCU",
                    date(2020, 4, 1),
                    date(2020, 4, 30),
                    64.2,
                    datetime(2020, 5, 15, tzinfo=UTC),
                    "v1",
                    unit="pct",
                ),
                obs(
                    "FRED:TCU",
                    date(2020, 5, 1),
                    date(2020, 5, 31),
                    99.0,
                    datetime(2020, 7, 15, tzinfo=UTC),
                    "v2",
                    unit="pct",
                ),
            ]
        )
        view = ObservationStore(conn).snapshot(cutoff)
        got = read_input_level(level, view)
    finally:
        conn.close()
    assert got.value == 0.642
    assert got.unit == "share"
    assert got.provenance == "snapshot:FRED:TCU"
    assert read_input_level(level, None).provenance == "case_fixture"
    assert read_input_level(level, None).value == 0.85


def test_held_out_split_adds_up():
    report = CaseSuite().run(CASES, LIBRARY, {"supply"})
    assert report.held_out.n_cases + report.development.n_cases == len(report.results)
    assert sum(group.n_cases for group in report.by_group) == len(report.results)
    for field in ("n_blocked", "n_scored", "n_sign_correct", "n_sign_wrong", "n_evidential", "n_evidential_correct"):
        assert sum(getattr(group, field) for group in report.by_group) == (
            getattr(report.held_out, field) + getattr(report.development, field)
        )
    assert report.held_out.n_cases == sum(1 for case in CASES if case.held_out_group == "supply")
    for item in report.results:
        assert item.n_evidential <= item.n_scored
        assert item.n_evidential_correct <= item.n_evidential
        assert item.n_author_fed + item.n_fixture_params >= item.n_scored - item.n_evidential
