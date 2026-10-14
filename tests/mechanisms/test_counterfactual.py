"""Counterfactual sign tests (spec §10.1 item 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from macrosys.mechanisms import check_monotone, get_impl, load_example, load_library

LIB = load_library(Path(__file__).resolve().parents[2] / "mechanisms")
MONOTONE = [mech_id for mech_id in LIB.ids() if LIB.get(mech_id).monotone]


def test_every_shipped_mechanism_with_a_sign_map_is_covered():
    assert len(MONOTONE) == 18


@pytest.mark.parametrize("mech_id", MONOTONE, ids=MONOTONE)
def test_monotone(mech_id: str):
    spec = LIB.get(mech_id)
    inputs, params, _expected = load_example(spec)
    check_monotone(spec, get_impl(spec.id), inputs, params)
