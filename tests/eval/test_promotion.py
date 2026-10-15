"""Research-to-paper thresholds come from the caller, not from a hard-coded bar (spec §10.3)."""

from __future__ import annotations

import pytest

from macrosys.eval.promotion import ResearchCounts, research_to_paper


def _counts(**overrides: int) -> ResearchCounts:
    base = dict(
        n_scored=12,
        n_evidential=12,
        n_evidential_correct=9,
        n_cases_with_evidential=12,
        held_out_evidential=4,
        held_out_evidential_correct=3,
        n_cases_naming_competing=12,
        n_cases_competing_listed=8,
    )
    base.update(overrides)
    return ResearchCounts(**base)


def test_zero_evidential_is_not_a_pass() -> None:
    result = research_to_paper(
        min_cases_scored=12,
        min_sign_accuracy=0.60,
        counts=_counts(n_evidential=0, n_evidential_correct=0, n_cases_with_evidential=0),
    )
    assert result.evaluable is False
    assert result.met is False
    assert "formula direction" in result.reasons[0]


def test_bar_met_when_counts_clear_the_mandate() -> None:
    result = research_to_paper(min_cases_scored=12, min_sign_accuracy=0.60, counts=_counts())
    assert result.evaluable is True
    assert result.met is True
    assert result.reasons == ()


def test_held_out_below_the_mandate_fails() -> None:
    result = research_to_paper(
        min_cases_scored=12,
        min_sign_accuracy=0.60,
        counts=_counts(held_out_evidential_correct=1),
    )
    assert result.met is False
    assert any("held-out" in reason for reason in result.reasons)


def test_competing_effect_must_be_listed_in_most_cases() -> None:
    result = research_to_paper(
        min_cases_scored=12,
        min_sign_accuracy=None,
        counts=_counts(n_cases_competing_listed=6),
    )
    assert result.met is False
    assert any("competing" in reason for reason in result.reasons)


def test_accuracy_outside_unit_interval_raises() -> None:
    with pytest.raises(ValueError):
        research_to_paper(min_cases_scored=12, min_sign_accuracy=1.5, counts=_counts())
