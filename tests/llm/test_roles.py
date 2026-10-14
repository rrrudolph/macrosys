"""The four roles against a canned provider (spec §8.3). No network."""

from __future__ import annotations

import pytest

from macrosys.core.quantity import Quantity
from macrosys.decide.thesis import ThesisStatus
from macrosys.llm.guard import EVIDENCE_RULE, Toolkit, evidence_text
from macrosys.llm.roles.critique import critique_thesis
from macrosys.llm.roles.extract import SourceDocument, extract_shocks
from macrosys.llm.roles.narrate import NarrativeNumberError, narrate
from macrosys.llm.roles.propose import propose_paths
from macrosys.mechanisms.library import load_library
from tests.decide.support import make_shock, make_thesis
from tests.llm.fakes import ROOT, FakeProvider, load_json, make_run

_DOCUMENT = (
    "The ministry said the program is now contracted: "
    "announced a 12.5 USD billion investment starting 2026-01-15."
)


def test_extraction_rejects_an_unquoted_number() -> None:
    provider = FakeProvider(load_json("extract.json"))
    documents = [SourceDocument(doc_id="doc-capex", text=_DOCUMENT)]
    kept, trace = extract_shocks(provider, Toolkit(), documents, model="test-model")
    assert len(kept) == 1
    assert kept[0].size.value == pytest.approx(12.5)
    assert kept[0].size.unit == "USD_bn"
    assert kept[0].size.provenance == "doc-capex"
    assert kept[0].stage == "contracted"
    assert any(event.kind == "unquoted_number" for event in trace.guard_events)
    request = provider.requests[0]
    assert EVIDENCE_RULE in request.system
    assert request.blocks[0].kind == "evidence"
    assert request.blocks[0].text == evidence_text("doc-capex", _DOCUMENT)
    assert trace.prompt_hash
    assert trace.model == "test-model"
    assert trace.input_tokens == 20
    assert trace.cost.unit == "USD"


def test_proposal_strips_parameters_and_flags_the_unit_mismatch() -> None:
    library = load_library(ROOT / "mechanisms")
    provider = FakeProvider(load_json("propose.json"))
    proposals, trace = propose_paths(
        provider,
        Toolkit(library=library),
        make_shock(),
        [],
        library,
        materiality=Quantity(value=0.01, unit="share", provenance="spec_3.4_rule4"),
        model="test-model",
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.valid is False
    assert "unit" in proposal.reason.lower()
    assert proposal.steps[0].bindings == {"product": "Cu"}
    assert proposal.missing_links == ["cost share of transformers in grid capex not in library"]
    assert any(event.kind == "parameters_stripped" for event in trace.guard_events)
    assert not any(hasattr(step, "parameters") for step in proposal.steps)


def test_critique_drops_objections_without_a_citation() -> None:
    provider = FakeProvider(load_json("critique.json"))
    objections, trace = critique_thesis(
        provider,
        Toolkit(),
        make_thesis(),
        make_run(),
        model="test-model",
    )
    assert len(objections) == 1
    assert objections[0].field == "priced_in"
    assert objections[0].kind == "priced_in_underestimated"
    assert objections[0].cites == ["C07"]
    assert any(event.kind == "uncited_objection" for event in trace.guard_events)


def test_narrative_rejects_a_number_that_is_not_in_the_thesis() -> None:
    thesis = make_thesis().transition(ThesisStatus.approved)
    provider = FakeProvider(load_json("narrate_bad.json"))
    with pytest.raises(NarrativeNumberError) as caught:
        narrate(provider, Toolkit(), thesis, make_run(), [], model="test-model")
    assert "12345" in caught.value.numbers


def test_narrative_keeps_numbers_that_are_already_in_the_thesis() -> None:
    thesis = make_thesis().transition(ThesisStatus.approved)
    provider = FakeProvider(load_json("narrate_ok.json"))
    narrative, trace = narrate(provider, Toolkit(), thesis, make_run(), [], model="test-model")
    assert "40" in narrative.markdown
    assert "90" in narrative.markdown
    assert trace.role == "narrate"
    assert trace.guard_events == []
