"""Role 3: red-team critique (spec §8.3). A thesis and its run become cited objections.

Each objection is keyed to a thesis field and carries a kind from the closed list. An objection
with no citation of a mechanism id, series id, or document id is dropped and a guard event is
recorded. The model is not asked for a magnitude or an outcome.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from macrosys.decide.thesis import Thesis
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
from macrosys.llm.provider import CompletionRequest, ContentBlock, Provider
from macrosys.llm.runtime import call_model, json_object_schema
from macrosys.llm.trace import GuardEvent, RoleTrace
from macrosys.mechanisms.compose import PropagationRun

ObjectionKind = Literal[
    "competing_omitted",
    "priced_in_underestimated",
    "unit_error",
    "horizon_mismatch",
    "data_stale",
    "claim_as_observation",
    "other",
]

_TASK = (
    "Role 3, red-team critique (spec §8.3). Emit objections keyed to thesis fields. "
    "Every objection must cite a mechanism id or a series or document id. "
    "Do not invent magnitudes or say what happened."
)


class Objection(BaseModel):
    """One cited objection against a thesis field (spec §8.3 role 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1)
    kind: ObjectionKind
    detail: str = Field(min_length=1)
    cites: list[str]


def critique_schema() -> dict[str, Any]:
    """JSON schema for role 3 (spec §8.3)."""
    objection = json_object_schema(
        {
            "field": {"type": "string"},
            "kind": {"type": "string"},
            "detail": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}},
        }
    )
    return json_object_schema({"objections": {"type": "array", "items": objection}})


def critique_thesis(
    provider: Provider,
    toolkit: Toolkit,
    thesis: Thesis,
    run: PropagationRun,
    *,
    model: str,
    guard: BudgetGuard | None = None,
    ledger: CostLedger | None = None,
    config: LlmConfig | None = None,
    max_tokens: int = 1024,
) -> tuple[list[Objection], RoleTrace]:
    """Critique ``thesis`` against ``run`` (spec §8.3 role 3, guardrail a).

    Objections without a citation are dropped. ``toolkit`` is the snapshot boundary and is not
    a fetch client. The thesis and the run summary are sent as evidence, not as instructions.
    """
    require_toolkit(toolkit)
    log = GuardLog()
    policy = OutputPolicy(log)
    schema = critique_schema()
    blocks = [
        evidence_block(thesis.thesis_id, thesis.model_dump_json()),
        evidence_block(run.run_id, run.model_dump_json()),
        ContentBlock(kind="instruction", text="State objections. Cite a mechanism, series, or document id."),
        ContentBlock(kind="schema", text=_dumps(schema)),
    ]
    result, trace = call_model(
        provider=provider,
        request=CompletionRequest(
            system=system_prompt(_TASK),
            blocks=blocks,
            json_schema=schema,
            model=model,
            max_tokens=max_tokens,
        ),
        role="critique",
        guard=guard,
        ledger=ledger,
        config=config,
    )
    try:
        policy.review(result.raw_text, result.parsed, schema, role="critique")
    except GuardRejectedError:
        return [], trace.with_events(log.events)
    objections = _keep(result.parsed, log)
    return objections, trace.with_events(log.events)


def _dumps(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, sort_keys=True)


def _keep(parsed: dict[str, Any], log: GuardLog) -> list[Objection]:
    raw_items = parsed.get("objections")
    if not isinstance(raw_items, list):
        raise ValueError("role 3 output has no objections list")
    kept: list[Objection] = []
    for item in raw_items:
        try:
            objection = Objection.model_validate(item)
        except ValidationError as exc:
            log.record(GuardEvent(kind="invalid_objection", detail=str(exc.errors()[0]["msg"]), role="critique"))
            continue
        if objection.field not in Thesis.model_fields:
            log.record(
                GuardEvent(kind="unknown_thesis_field", detail=objection.field, role="critique")
            )
            continue
        cites = [cite.strip() for cite in objection.cites if cite.strip()]
        if not cites:
            log.record(
                GuardEvent(
                    kind="uncited_objection",
                    detail=f"dropped objection on {objection.field}",
                    role="critique",
                )
            )
            continue
        kept.append(objection.model_copy(update={"cites": cites}))
    return kept
