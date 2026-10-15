"""The event-study slope as a gap-gate input (spec §7.3, §11.3, §10.1 item 11)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from macrosys.core.quantity import Quantity
from macrosys.core.units import UnitError
from macrosys.decide.gates import decide
from macrosys.events.cli_commands import _cmd_check
from macrosys.events.response import check_response, load_responses, response_block
from tests.decide.support import make_thesis

ROOT = Path(__file__).resolve().parents[2]


def _book(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "event_response.yaml"
    path.write_text(yaml.safe_dump({"entries": entries}, sort_keys=False), encoding="utf-8")
    return path


def _slope(**overrides: object) -> dict:
    beta = {
        "value": 1.0,
        "low": 0.5,
        "high": 2.0,
        "unit": "bp",
        "per": "1 sd of standardized surprise",
        "source": "event_study:fixture",
    }
    beta.update(overrides.pop("beta", {}))  # type: ignore[arg-type]
    entry = {
        "name": "REL->MKT@1d",
        "release": "REL",
        "market": "MKT",
        "horizon_days": 1,
        "beta": beta,
        "predicted_sign": 1,
        "mechanisms": ["C09_policy_path"],
        "n": 100,
    }
    entry.update(overrides)
    return entry


def _decide(response: str | None):
    return decide(
        make_thesis(),
        run_valid=True,
        identity_residuals_ok=True,
        min_gap_bp=20.0,
        exposure_match=True,
        horizon_ok=True,
        post_trade_ok=True,
        regime_flag="stable",
        response_block=response,
    )


def test_slope_scales_and_flips_with_the_surprise(tmp_path: Path) -> None:
    book = load_responses(_book(tmp_path, [_slope()]))
    up = check_response(book, "REL", "MKT", 1, 2.0, Quantity(value=0.0, unit="bp"))
    assert up.implied is not None
    assert up.implied.unit == "bp"
    assert up.implied.value == pytest.approx(2.0)
    assert up.implied.low == pytest.approx(1.0)
    assert up.implied.high == pytest.approx(4.0)
    assert up.implied.provenance == "event_study:fixture"
    down = check_response(book, "REL", "MKT", 1, -1.0, Quantity(value=0.0, unit="bp"))
    assert down.implied is not None
    assert down.implied.value == pytest.approx(-1.0)
    assert down.implied.low == pytest.approx(-2.0)
    assert down.implied.high == pytest.approx(-0.5)


def test_front_end_blocks_only_once_it_covers_the_conservative_edge(tmp_path: Path) -> None:
    book = load_responses(_book(tmp_path, [_slope()]))
    short = check_response(book, "REL", "MKT", 1, 1.0, Quantity(value=0.4, unit="bp"))
    assert short.blocked is False
    assert short.residual is not None and short.residual.low == pytest.approx(0.1)
    covered = response_block(book, "REL", "MKT", 1, 1.0, Quantity(value=0.5, unit="bp"))
    assert covered is not None and "already moved" in covered
    wrong_way = response_block(book, "REL", "MKT", 1, 1.0, Quantity(value=-3.0, unit="bp"))
    assert wrong_way is None


def test_percent_point_observation_converts_before_the_comparison(tmp_path: Path) -> None:
    book = load_responses(_book(tmp_path, [_slope()]))
    # 0.01 pp = 1 bp, which is past the 0.5 bp conservative edge.
    blocked = response_block(book, "REL", "MKT", 1, 1.0, Quantity(value=0.01, unit="pp"))
    assert blocked is not None and "already moved" in blocked


def test_zero_surprise_and_a_zero_interval_and_a_missing_pair_block(tmp_path: Path) -> None:
    wide = _slope(name="REL->MKT@5d", horizon_days=5, beta={"value": 0.1, "low": -1.0, "high": 1.0})
    book = load_responses(_book(tmp_path, [_slope(), wide]))
    zero = check_response(book, "REL", "MKT", 1, 0.0, Quantity(value=5.0, unit="bp"))
    assert zero.blocked and "surprise is 0" in zero.reason
    straddling = response_block(book, "REL", "MKT", 5, 1.0, Quantity(value=0.0, unit="bp"))
    assert straddling is not None and "contains zero" in straddling
    missing = response_block(book, "REL", "OTHER", 1, 1.0, Quantity(value=0.0, unit="bp"))
    assert missing is not None and missing.startswith("no estimated response")


def test_unit_mismatch_is_an_error(tmp_path: Path) -> None:
    book = load_responses(_book(tmp_path, [_slope()]))
    with pytest.raises(UnitError):
        check_response(book, "REL", "MKT", 1, 1.0, Quantity(value=1.0, unit="USD"))


def test_duplicate_pair_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        load_responses(_book(tmp_path, [_slope(), _slope()]))


def test_a_blocked_response_fails_the_gap_gate_of_an_otherwise_passing_thesis(tmp_path: Path) -> None:
    book = load_responses(_book(tmp_path, [_slope()]))
    reason = response_block(book, "REL", "MKT", 1, 1.0, Quantity(value=3.0, unit="bp"))
    decision = _decide(reason)
    assert decision.action == "no_trade"
    assert decision.failing_gate == "gap"
    assert reason is not None and decision.reason == reason
    assert _decide(None).action == "trade"


def test_check_command_prints_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _book(tmp_path, [_slope()])
    args = SimpleNamespace(
        parameters=path, release="REL", market="MKT", horizon=1, surprise=1.0, observed=0.4, unit="bp"
    )
    assert _cmd_check(args) == 0
    assert "has not covered" in capsys.readouterr().out


def test_committed_file_blocks_on_its_own_intervals() -> None:
    book = load_responses(ROOT / "parameters" / "event_response.yaml")
    assert book.entries
    saw_open = False
    saw_closed = False
    for item in book.entries:
        flat = Quantity(value=0.0, unit=item.beta.unit, provenance="test")
        at_zero = check_response(book, item.release, item.market, item.horizon_days, 1.0, flat)
        assert at_zero.implied is not None and at_zero.implied.provenance == item.beta.provenance
        if item.beta.straddles_zero():
            assert at_zero.blocked and "contains zero" in at_zero.reason
            saw_closed = True
            continue
        assert at_zero.blocked is False
        saw_open = True
        far = Quantity(value=item.beta.scale(10.0).mid, unit=item.beta.unit, provenance="test")
        covered = check_response(book, item.release, item.market, item.horizon_days, 1.0, far)
        assert covered.blocked and "already moved" in covered.reason
    assert saw_open and saw_closed
    payrolls = book.get("FRED:PAYEMS", "FRED:DGS2", 1)
    assert payrolls is not None and payrolls.beta.unit == "bp"
    assert payrolls.beta.straddles_zero() is False
