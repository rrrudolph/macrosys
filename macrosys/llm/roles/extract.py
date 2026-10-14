"""Role 1: event extraction (spec §8.3). Documents become shock candidates, or they are dropped.

A candidate carries the shock fields from spec §4.1: type, subtype, origin, size with a unit,
realization-ladder stage, and start date, plus source spans. Every numeric component of the size
must appear in a cited quote, and that quote must be a substring of the named document. Numbers
the source does not contain are not returned.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.decide.shock import INVESTMENT_STAGES, POLICY_STAGES, SUBTYPES
from macrosys.llm.cost import BudgetGuard, CostLedger, LlmConfig
from macrosys.llm.guard import (
    GuardLog,
    GuardRejectedError,
    OutputPolicy,
    Toolkit,
    evidence_block,
    require_toolkit,
    system_prompt,
)
from macrosys.llm.numbers import number_tokens, quantity_tokens
from macrosys.llm.provider import CompletionRequest, ContentBlock, Provider
from macrosys.llm.runtime import call_model, json_object_schema
from macrosys.llm.trace import GuardEvent, RoleTrace

_TASK = (
    "Role 1, event extraction (spec §8.3). Read the evidence blocks and emit shock candidates. "
    "Every number on a candidate must appear in a cited quote from that evidence. "
    "Use the shock taxonomy and the realization ladder (spec §4.1). Do not propose trades."
)

_NULLABLE = {"anyOf": [{"type": "number"}, {"type": "null"}]}


class SourceDocument(BaseModel):
    """One approved text the extractor may cite (spec §8.3 role 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str = Field(min_length=1)
    text: str


class SourceSpan(BaseModel):
    """A quote inside a document. The quote is the citation for the candidate (spec §8.3 role 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class ShockCandidate(BaseModel):
    """Shock-compatible fields plus the spans that support them (spec §8.3 role 1, §4.1).

    ``type``, ``subtype``, ``origin``, ``size``, ``stage``, and ``start`` are the fields a
    ``Shock`` record requires from extraction. Certainty is not taken from the model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str = Field(min_length=1)
    subtype: str = Field(min_length=1)
    origin: EntityKey
    size: Quantity
    stage: str = Field(min_length=1)
    start: date
    source_spans: list[SourceSpan] = Field(min_length=1)


def extract_schema() -> dict[str, Any]:
    """JSON schema sent to the provider for role 1 (spec §8.3)."""
    span = json_object_schema({"doc_id": {"type": "string"}, "quote": {"type": "string"}})
    origin = json_object_schema({"entity_type": {"type": "string"}, "entity_id": {"type": "string"}})
    size = json_object_schema(
        {"value": _NULLABLE, "unit": {"type": "string"}, "low": _NULLABLE, "high": _NULLABLE}
    )
    candidate = json_object_schema(
        {
            "type": {"type": "string"},
            "subtype": {"type": "string"},
            "origin": origin,
            "size": size,
            "stage": {"type": "string"},
            "start": {"type": "string"},
            "source_spans": {"type": "array", "items": span},
        }
    )
    return json_object_schema({"candidates": {"type": "array", "items": candidate}})


def extract_shocks(
    provider: Provider,
    toolkit: Toolkit,
    documents: list[SourceDocument],
    *,
    model: str,
    guard: BudgetGuard | None = None,
    ledger: CostLedger | None = None,
    config: LlmConfig | None = None,
    max_tokens: int = 1024,
) -> tuple[list[ShockCandidate], RoleTrace]:
    """Extract shock candidates from ``documents`` (spec §8.3 role 1, guardrail a).

    ``toolkit`` is required so the call sits on the approved-snapshot boundary and cannot fetch.
    Candidates whose size is not in a cited span are dropped and a guard event is recorded.
    The return is the kept candidates and the call trace.
    """
    require_toolkit(toolkit)
    log = GuardLog()
    policy = OutputPolicy(log)
    schema = extract_schema()
    blocks: list[ContentBlock] = [evidence_block(document.doc_id, document.text) for document in documents]
    blocks.append(ContentBlock(kind="instruction", text="Emit shock candidates for the evidence."))
    blocks.append(ContentBlock(kind="schema", text=_schema_text(schema)))
    result, trace = call_model(
        provider=provider,
        request=CompletionRequest(
            system=system_prompt(_TASK),
            blocks=blocks,
            json_schema=schema,
            model=model,
            max_tokens=max_tokens,
        ),
        role="extract",
        guard=guard,
        ledger=ledger,
        config=config,
    )
    try:
        policy.review(result.raw_text, result.parsed, schema, role="extract")
    except GuardRejectedError:
        return [], trace.with_events(log.events)
    kept = _keep_candidates(result.parsed, documents, log)
    return kept, trace.with_events(log.events)


def _schema_text(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, sort_keys=True)


def _keep_candidates(
    parsed: dict[str, Any], documents: list[SourceDocument], log: GuardLog
) -> list[ShockCandidate]:
    raw_items = parsed.get("candidates")
    if not isinstance(raw_items, list):
        raise ValueError("role 1 output has no candidates list")
    kept: list[ShockCandidate] = []
    for item in raw_items:
        try:
            candidate = ShockCandidate.model_validate(item)
        except ValidationError as exc:
            log.record(GuardEvent(kind="invalid_candidate", detail=str(exc.errors()[0]["msg"]), role="extract"))
            continue
        accepted = _accept(candidate, documents, log)
        if accepted is not None:
            kept.append(accepted)
    return kept


def _accept(candidate: ShockCandidate, documents: list[SourceDocument], log: GuardLog) -> ShockCandidate | None:
    try:
        _taxonomy(candidate)
    except ValueError as exc:
        log.record(GuardEvent(kind="invalid_candidate", detail=str(exc), role="extract"))
        return None
    by_id = {document.doc_id: document.text for document in documents}
    for span in candidate.source_spans:
        source = by_id.get(span.doc_id)
        if source is None or span.quote not in source:
            log.record(
                GuardEvent(
                    kind="quote_not_in_source",
                    detail=f"{span.doc_id} does not contain the cited quote",
                    role="extract",
                )
            )
            return None
    quoted = number_tokens("\n".join(span.quote for span in candidate.source_spans))
    missing = sorted(quantity_tokens(candidate.size) - quoted)
    if missing:
        log.record(
            GuardEvent(
                kind="unquoted_number",
                detail=f"size tokens {missing} are not in a cited span",
                role="extract",
            )
        )
        return None
    if candidate.size.provenance is None:
        provenance = candidate.source_spans[0].doc_id
        candidate = candidate.model_copy(
            update={"size": candidate.size.model_copy(update={"provenance": provenance})}
        )
    return candidate


def _taxonomy(candidate: ShockCandidate) -> None:
    if candidate.type not in SUBTYPES:
        raise ValueError(f"type {candidate.type!r} is not in the shock taxonomy")
    if candidate.subtype not in SUBTYPES[candidate.type]:
        raise ValueError(f"subtype {candidate.subtype!r} is not in the {candidate.type} taxonomy")
    stages = INVESTMENT_STAGES | POLICY_STAGES
    if candidate.stage not in stages:
        raise ValueError(f"stage {candidate.stage!r} is not on the realization ladder")
    if candidate.type == "policy" and candidate.stage not in POLICY_STAGES:
        raise ValueError("policy shocks use the policy realization ladder (spec §4.1)")
    if candidate.subtype == "investment" and candidate.stage not in INVESTMENT_STAGES:
        raise ValueError("investment shocks use the investment realization ladder (spec §4.1)")
