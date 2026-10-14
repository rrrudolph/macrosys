"""Fed-funds path and meeting probabilities (spec §3.3 C09)."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from macrosys.pricing.errors import CurveError
from macrosys.pricing.policy_path import implied_policy_path, meeting_probabilities

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pricing" / "fedfunds_settles.csv"


def _settlements() -> list[tuple[date, float]]:
    rows: list[tuple[date, float]] = []
    with _FIXTURE.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            rows.append((date.fromisoformat(record["contract_month"]), float(record["settle_price"])))
    return rows


def test_fed_funds_fixture_matches_hand_rates_and_does_not_shift_for_the_premium():
    path = implied_policy_path(_settlements(), "fedfunds", as_of=date(2026, 3, 1))
    # 100 - 95.75 = 4.25, 100 - 95.60 = 4.40. The TODO band widens and does not move these.
    rates = [point.value for _horizon, point in path]
    assert rates == pytest.approx([4.25, 4.40])
    for _horizon, point in path:
        assert point.unit == "pct"
        assert point.low is not None and point.high is not None
        assert point.low < point.value < point.high
        assert point.provenance is not None and "TODO_estimate" in point.provenance


def test_meeting_probability_under_the_no_premium_assumption():
    # April 2026 has 30 days. Meeting on the 16th: post-meeting weight
    # (30 - 16 + 1) / 30 = 0.5, counting the announcement day at the new rate.
    # March average 4.25, April average 4.40.
    # Expected change = (4.40 - 4.25) / 0.5 = 0.30 percent = 30 bp.
    # On a 25 bp grid: 80% on +25 bp and 20% on +50 bp.
    path = implied_policy_path(_settlements(), "fedfunds", as_of=date(2026, 3, 1))
    dist = meeting_probabilities(path, [date(2026, 4, 16)], step_bp=25)
    assert len(dist) == 1
    meeting = dist[0]
    assert meeting.assumption == "no_premium"
    assert "no risk premium" in meeting.notes.lower()
    assert meeting.expected_change_bp.unit == "bp"
    assert meeting.expected_change_bp.value == pytest.approx(30.0)
    probs = {step.change_bp: step.probability.value for step in meeting.outcomes}
    assert probs[25] == pytest.approx(0.8)
    assert probs[50] == pytest.approx(0.2)
    assert sum(probs.values()) == pytest.approx(1.0)


def test_sofr_is_100_minus_price_and_meetings_are_refused():
    path = implied_policy_path([(date(2026, 6, 1), 95.0)], "sofr3m", as_of=date(2026, 5, 1))
    assert path[0][1].value == pytest.approx(5.0)
    with pytest.raises(CurveError, match="fed-funds"):
        meeting_probabilities(path, [date(2026, 6, 17)])
