"""Mandate fields added by the 2026-10-15 owner decisions: shorting with a borrow rate, a baseline."""

from __future__ import annotations

from pathlib import Path

import pytest

from macrosys.decide.mandate import Mandate, MandateIncompleteError, load_example_mandate

REPO = Path(__file__).resolve().parents[2]


def test_working_mandate_allows_shorts_and_names_a_baseline() -> None:
    mandate = Mandate.load(REPO / "config" / "mandate.yaml")
    assert mandate.shorting.allowed is True
    assert mandate.leverage.allowed is False
    assert mandate.baseline is not None
    assert mandate.baseline.method == "inverse_vol"
    assert len(mandate.baseline.instruments) == len(set(mandate.baseline.instruments))
    # The borrow rate is a provider fact the owner has not entered: a short cannot be costed yet.
    with pytest.raises(MandateIncompleteError, match="borrow_cost_bp_annual"):
        mandate.short_borrow_cost_bp_annual()


def test_example_mandate_is_cash_and_long_only() -> None:
    mandate = load_example_mandate()
    assert mandate.baseline is None
    assert mandate.shorting.allowed is False
    with pytest.raises(MandateIncompleteError, match="shorting.allowed"):
        mandate.short_borrow_cost_bp_annual()


def test_borrow_rate_is_read_when_entered() -> None:
    mandate = load_example_mandate()
    allowed = mandate.model_copy(
        update={
            "shorting": mandate.shorting.model_copy(update={"allowed": True, "borrow_cost_bp_annual": 35.0})
        }
    )
    assert allowed.short_borrow_cost_bp_annual() == 35.0
    with pytest.raises(ValueError, match="cannot be negative"):
        type(mandate.shorting)(allowed=True, borrow_cost_bp_annual=-1.0)


def test_baseline_config_rejects_duplicates_and_short_windows() -> None:
    from macrosys.decide.mandate import BaselineConfig

    with pytest.raises(ValueError, match="duplicate"):
        BaselineConfig(method="inverse_vol", window_days=63, instruments=["SPY", "SPY"])
    with pytest.raises(ValueError, match="at least 2"):
        BaselineConfig(method="inverse_vol", window_days=1, instruments=["SPY"])
