"""Append-only forecast ledger: hash chain and scores (spec §10.2 item 7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from macrosys.eval.ledger import ForecastLedger, Outcome
from tests.decide.support import make_thesis


def _weighted(thesis_id: str):
    return make_thesis(
        thesis_id=thesis_id,
        expected_effect={
            "entity": {"type": "commodity", "id": "Cu"},
            "variable": "price",
            "change": {"unit": "pct", "low": 2.0, "high": 5.0},
            "window_q": [2, 6],
        },
        priced_in={
            "source": ["C07 curve"],
            "implied_change": {"unit": "pct", "low": 0.0, "high": 1.0},
            "method_notes": "curve",
        },
        gap={"unit": "pct", "low": 2.0, "high": 4.0},
        scenarios=[
            {"name": "base", "weight": 0.7, "outcome_range": {"unit": "pct", "low": 2.0, "high": 5.0}},
            {"name": "alt", "weight": 0.3, "outcome_range": {"unit": "pct", "low": -4.0, "high": -1.0}},
        ],
    )


def test_verify_detects_a_tampered_line(tmp_path) -> None:
    ledger = ForecastLedger(tmp_path / "forecasts.jsonl")
    when = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
    ledger.freeze(make_thesis(), frozen_at=when)
    ledger.freeze(make_thesis(thesis_id="T-second"), frozen_at=when)
    assert ledger.verify() is True
    text = ledger.path.read_text(encoding="utf-8")
    ledger.path.write_text(text.replace("Copper", "Silver", 1), encoding="utf-8")
    assert ledger.verify() is False


def test_score_sign_coverage_and_brier(tmp_path) -> None:
    ledger = ForecastLedger(tmp_path / "forecasts.jsonl")
    when = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    ledger.freeze(_weighted("T-hit"), frozen_at=when)
    ledger.freeze(_weighted("T-miss"), frozen_at=when)
    outcomes = {
        "T-hit": Outcome(realized_change=3.0, scenario="base"),
        "T-miss": Outcome(realized_change=-2.0, scenario="alt"),
    }
    before = ledger.score(datetime(2026, 1, 1, tzinfo=UTC), outcomes)
    assert before.empty
    frame = ledger.score(datetime(2026, 2, 1, tzinfo=UTC), outcomes)
    assert list(frame["thesis_id"]) == ["T-hit", "T-miss"]
    hit = frame.iloc[0]
    miss = frame.iloc[1]
    assert bool(hit["sign_correct"]) is True
    assert bool(hit["interval_covered"]) is True
    assert hit["interval_level"] == pytest.approx(0.8)
    assert hit["brier"] == pytest.approx(0.18)
    assert bool(miss["sign_correct"]) is False
    assert bool(miss["interval_covered"]) is False
    assert miss["brier"] == pytest.approx(0.98)
