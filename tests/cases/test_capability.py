"""Named capability fixtures (spec §10.1 item 8)."""

from pathlib import Path

import pytest

from macrosys.cases.capability_tests import assert_capability, run_capability
from macrosys.mechanisms.library import load_library

LIBRARY = load_library(Path(__file__).resolve().parents[2] / "mechanisms")

_XFAIL = {
    "abundant_supply": "parameter capture is TODO_estimate",
    "scarce_complement": "parameters eps_s_slack, sigma, and slippage are TODO_estimate",
    "productivity": "parameters pass_through, eps_sr, and capture are TODO_estimate",
    "currency_depreciation": "parameter pt_import is TODO_estimate",
    "anticipated_rate": "parameters risk_premium and multiplier are TODO_estimate",
    "funding_gross_vs_net": "parameter elasticity is TODO_estimate",
}

_PASSING = (
    "inflation_hedge",
    "future_dated_document",
    "priced_in_no_trade",
    "release_matches_forecast",
    "release_already_priced",
)


@pytest.mark.parametrize("name", _PASSING)
def test_capability_passes(name: str):
    result = run_capability(name, LIBRARY)
    assert_capability(name, result)


@pytest.mark.parametrize(
    "name",
    [pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=reason)) for name, reason in _XFAIL.items()],
)
def test_capability_blocked_on_library_parameter(name: str):
    result = run_capability(name, LIBRARY)
    assert_capability(name, result)
