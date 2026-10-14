"""Thesis schema, shock taxonomy, and status transitions (spec §4.1, §7.7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from macrosys.decide.thesis import IllegalThesisTransitionError, Thesis, ThesisStatus
from tests.decide.support import make_shock, make_thesis


def test_spec_shaped_payload_loads_every_field() -> None:
    thesis = make_thesis()
    assert thesis.thesis_id == "T-2026-0412"
    assert thesis.expected_effect.entity.entity_id == "Cu"
    assert thesis.expected_effect.window_q == (2, 6)
    assert thesis.invalidation.time_stop.isoformat() == "2027-04-14"
    assert thesis.gap.unit == "bp"
    assert thesis.status is ThesisStatus.proposed
    assert thesis.missing_evidence == []


def test_string_missing_evidence_is_non_blocking() -> None:
    thesis = make_thesis(missing_evidence=["off-exchange refined inventory"])
    assert thesis.missing_evidence[0].description == "off-exchange refined inventory"
    assert thesis.missing_evidence[0].blocking is False


def test_gap_unit_must_match_expected_effect() -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        make_thesis(gap={"unit": "USD", "low": 1.0, "high": 2.0})


def test_gap_straddle_cannot_be_approved() -> None:
    thesis = make_thesis(gap={"unit": "bp", "low": -5.0, "high": 10.0})
    with pytest.raises(ValidationError, match="straddles zero"):
        thesis.transition(ThesisStatus.approved)


def test_blocking_evidence_cannot_be_approved() -> None:
    thesis = make_thesis(missing_evidence=[{"description": "no off-exchange inventory", "blocking": True}])
    with pytest.raises(ValidationError, match="blocking missing evidence"):
        thesis.transition("approved")


def test_legal_transition_and_illegal_jump() -> None:
    approved = make_thesis().transition(ThesisStatus.approved)
    assert approved.status is ThesisStatus.approved
    live = approved.transition(ThesisStatus.live)
    assert live.status is ThesisStatus.live
    with pytest.raises(IllegalThesisTransitionError):
        make_thesis().transition(ThesisStatus.live)


def test_shock_taxonomy_and_stage_ladder() -> None:
    shock = make_shock()
    assert shock.type == "demand"
    assert shock.stage == "contracted"
    with pytest.raises(ValidationError, match="policy realization ladder"):
        make_shock(type="policy", subtype="monetary", stage="announced")
    with pytest.raises(ValidationError, match="not in the demand taxonomy"):
        make_shock(subtype="monetary")


def test_round_trip_preserves_thesis() -> None:
    thesis = make_thesis()
    again = Thesis.model_validate(thesis.model_dump(mode="json"))
    assert again == thesis
