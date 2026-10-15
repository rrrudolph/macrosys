"""A slope becomes a library range only when the interval and the windows agree (spec §8.4)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from macrosys.cases.runner import todo_fixture_overrides
from macrosys.core.quantity import Quantity
from macrosys.estimate.local import judge_slope
from macrosys.mechanisms.library import load_library


def test_stable_negative_slope_installs_the_negated_range() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=90)
    y = -4.0 * x + rng.normal(scale=0.01, size=90)
    status, reason, low, high, beta, n = judge_slope(
        x, y, beta_sign=-1, map_name="negate", minimum_n=60, minimum_window=24, windows=3
    )
    assert status == "installed"
    assert reason.startswith("interval")
    assert n == 90
    assert beta is not None and beta < 0.0
    assert low is not None and high is not None
    assert 3.5 < low < high < 4.5


def test_noise_is_not_installed() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=90)
    y = rng.normal(size=90)
    status, reason, low, high, _beta, _n = judge_slope(
        x, y, beta_sign=1, map_name="identity", minimum_n=60, minimum_window=24, windows=3
    )
    assert status == "rejected"
    assert low is None and high is None
    assert "zero" in reason or "sign" in reason or "window" in reason


def test_a_sign_that_flips_across_windows_is_rejected() -> None:
    x = np.linspace(-1.0, 1.0, 90)
    y = np.concatenate([-x[:45], x[45:]])
    status, _reason, low, _high, _beta, _n = judge_slope(
        x, y, beta_sign=-1, map_name="negate", minimum_n=60, minimum_window=24, windows=2
    )
    assert status == "rejected"
    assert low is None


def test_short_sample_is_rejected() -> None:
    x = np.array([1.0, 2.0, 3.0])
    y = -x
    status, reason, *_rest = judge_slope(
        x, y, beta_sign=-1, map_name="negate", minimum_n=60, minimum_window=24, windows=3
    )
    assert status == "rejected"
    assert "below" in reason


def test_one_minus_stores_the_complement() -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(size=90)
    y = 0.25 * x + rng.normal(scale=0.01, size=90)
    status, _reason, low, high, beta, _n = judge_slope(
        x, y, beta_sign=1, map_name="one_minus", minimum_n=60, minimum_window=24, windows=3
    )
    assert status == "installed"
    assert beta is not None and 0.2 < beta < 0.3
    assert low is not None and high is not None
    assert 0.7 < low < high < 0.8


def test_instrument_recovers_the_structural_slope() -> None:
    rng = np.random.default_rng(3)
    z = rng.normal(size=120)
    x = z + rng.normal(scale=0.1, size=120)
    y = -1.5 * x + rng.normal(scale=0.05, size=120)
    status, _reason, low, high, beta, _n = judge_slope(
        x,
        y,
        beta_sign=-1,
        map_name="identity",
        minimum_n=60,
        minimum_window=24,
        windows=3,
        instrument=z,
    )
    assert status == "installed"
    assert beta is not None and -1.7 < beta < -1.3
    assert low is not None and high is not None and low < beta < high


def test_half_life_of_a_persistent_quarterly_rate() -> None:
    from macrosys.estimate.local import bounds_half_life

    bounds = bounds_half_life(0.90, 0.98, 4)
    assert bounds is not None
    short, long = bounds
    assert 1.0 < short < long
    assert bounds_half_life(0.90, 1.0, 4) is None


def test_mean_outside_the_share_range_is_rejected() -> None:
    from macrosys.estimate.local import judge_mean

    values = np.full(90, 1.4)
    status, _reason, low, _high, _beta, _n = judge_mean(values, minimum_n=60, minimum_window=24, windows=3)
    assert status == "rejected"
    assert low is None


def test_inline_parameter_is_rewritten_as_a_range(tmp_path) -> None:
    from datetime import UTC, datetime

    from macrosys.estimate.local import Coefficient, EstimateReport, install_supported

    card = tmp_path / "B05_capacity_pipeline.yaml"
    card.write_text(
        "parameters:\n  - {name: slippage, unit: share, source: TODO_estimate}\n",
        encoding="utf-8",
    )
    report = EstimateReport(
        as_of=datetime(2026, 10, 15, tzinfo=UTC),
        results=(
            Coefficient(
                mechanism="B05_capacity_pipeline",
                parameter="slippage",
                status="installed",
                reason="the mean is a share",
                unit="share",
                low=0.04,
                high=0.08,
                beta=0.06,
                n=100,
                source="local_projection:test",
            ),
        ),
    )
    assert install_supported(report, tmp_path) == ["B05_capacity_pipeline"]
    text = card.read_text(encoding="utf-8")
    assert "TODO_estimate" not in text
    assert "low: 0.04" in text
    assert 'source: "local_projection:test"' in text


def test_install_does_not_rewrite_the_next_parameter(tmp_path) -> None:
    from datetime import UTC, datetime

    from macrosys.estimate.local import Coefficient, EstimateReport, install_supported

    card = tmp_path / "B10_phillips.yaml"
    card.write_text(
        "parameters:\n"
        "- name: slope_wage\n"
        "  unit: ratio\n"
        "  source: TODO_estimate\n"
        "- name: slope_core\n"
        "  unit: ratio\n"
        "  source: TODO_estimate\n",
        encoding="utf-8",
    )
    report = EstimateReport(
        as_of=datetime(2026, 10, 15, tzinfo=UTC),
        results=(
            Coefficient(
                mechanism="B10_phillips",
                parameter="slope_wage",
                status="installed",
                reason="interval excludes zero and the sign is stable",
                unit="ratio",
                low=0.01,
                high=0.11,
                beta=-0.06,
                n=100,
                source="local_projection:test",
            ),
        ),
    )
    install_supported(report, tmp_path)
    text = card.read_text(encoding="utf-8")
    assert text.count("TODO_estimate") == 1
    assert "name: slope_core" in text
    assert "low: 0.01" in text


def test_replay_fixture_does_not_cover_an_estimated_parameter() -> None:
    library = load_library(Path(__file__).resolve().parents[2] / "mechanisms")
    spec = library.get("B09_okun")
    bound = {
        "okun": Quantity(value=1.0, unit="ratio", provenance="case_fixture:replay_sign"),
    }
    assert todo_fixture_overrides(spec, bound) == {}
