"""Role 2: path proposal (spec §8.3). A shock and retrieved context become mechanism paths.

The model may name ordered mechanism ids, entity bindings, and missing links. It may not set
parameters: any parameter object is stripped and a guard event is recorded. Each proposal is
passed through ``validate_path``. An invalid path is kept and marked ``valid=False`` with the
validator's reason. Wiring uses a unit-compatible port when one exists, otherwise the first
ports, so a unit mismatch fails in the engine rather than being repaired here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from macrosys.core.quantity import Quantity
from macrosys.core.units import compatible
from macrosys.decide.shock import Shock
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
from macrosys.mechanisms.compose import MechanismInstance, PropagationPath, Wire, validate_path
from macrosys.mechanisms.library import Library
from macrosys.mechanisms.spec import MechanismSpec

_TASK = (
    "Role 2, path proposal (spec §8.3). Propose ordered mechanism ids, entity bindings, and "
    "missing links for the shock. Do not set mechanism parameters. The engine validates "
    "composition (spec §3.4)."
)
_PARAM_KEYS = ("parameters", "parameter_overrides", "params")


class ProposedStep(BaseModel):
    """One mechanism on a proposed path. Bindings name entities; parameters are not accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanism_id: str = Field(min_length=1)
    bindings: dict[str, str] = Field(default_factory=dict)


class PathProposal(BaseModel):
    """An ordered path, the missing links, and the engine's validity flag (spec §8.3 role 2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: list[ProposedStep] = Field(min_length=1)
    missing_links: list[str]
    valid: bool
    reason: str


def propose_schema() -> dict[str, Any]:
    """JSON schema for role 2. Parameters are not in the schema (spec §8.3).

    Bindings are a list of name/entity pairs so the object schema can set
    ``additionalProperties`` false, which the Responses API strict mode requires.
    """
    binding = json_object_schema({"name": {"type": "string"}, "entity": {"type": "string"}})
    step = json_object_schema(
        {"mechanism_id": {"type": "string"}, "bindings": {"type": "array", "items": binding}}
    )
    proposal = json_object_schema(
        {
            "steps": {"type": "array", "items": step},
            "missing_links": {"type": "array", "items": {"type": "string"}},
        }
    )
    return json_object_schema({"proposals": {"type": "array", "items": proposal}})


def propose_paths(
    provider: Provider,
    toolkit: Toolkit,
    shock: Shock,
    context: list[tuple[str, str]],
    library: Library,
    *,
    materiality: Quantity,
    model: str,
    guard: BudgetGuard | None = None,
    ledger: CostLedger | None = None,
    config: LlmConfig | None = None,
    max_tokens: int = 1024,
) -> tuple[list[PathProposal], RoleTrace]:
    """Propose mechanism paths for ``shock`` and validate each one (spec §8.3 role 2, §3.4).

    ``context`` is ``(doc_id, text)`` already retrieved for this call. ``toolkit`` is the
    snapshot boundary. ``materiality`` is the path threshold in share units. Invalid proposals
    are returned with ``valid`` false. Stripped parameters produce a guard event.
    """
    require_toolkit(toolkit)
    log = GuardLog()
    policy = OutputPolicy(log)
    schema = propose_schema()
    blocks = [evidence_block(shock.id, shock.model_dump_json())]
    blocks.extend(evidence_block(doc_id, text) for doc_id, text in context if text)
    blocks.append(ContentBlock(kind="instruction", text="Propose mechanism paths. Do not set parameters."))
    blocks.append(ContentBlock(kind="schema", text=_dumps(schema)))
    result, trace = call_model(
        provider=provider,
        request=CompletionRequest(
            system=system_prompt(_TASK),
            blocks=blocks,
            json_schema=schema,
            model=model,
            max_tokens=max_tokens,
        ),
        role="propose",
        guard=guard,
        ledger=ledger,
        config=config,
    )
    stripped = _strip_parameters(result.parsed, log)
    try:
        policy.review(result.raw_text, stripped, schema, role="propose")
    except GuardRejectedError:
        return [], trace.with_events(log.events)
    proposals = _validate_all(stripped, library, materiality, log)
    return proposals, trace.with_events(log.events)


def _dumps(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, sort_keys=True)


def _strip_parameters(parsed: dict[str, Any], log: GuardLog) -> dict[str, Any]:
    proposals = parsed.get("proposals")
    if not isinstance(proposals, list):
        return parsed
    cleaned: list[Any] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            cleaned.append(proposal)
            continue
        proposal = dict(proposal)
        for key in _PARAM_KEYS:
            if key in proposal:
                proposal.pop(key)
                log.record(GuardEvent(kind="parameters_stripped", detail=f"stripped {key} from proposal", role="propose"))
        steps: list[Any] = []
        for step in proposal.get("steps") or []:
            if not isinstance(step, dict):
                steps.append(step)
                continue
            step = dict(step)
            removed = [key for key in _PARAM_KEYS if key in step]
            for key in removed:
                step.pop(key)
            if removed:
                mechanism_id = step.get("mechanism_id", "?")
                log.record(
                    GuardEvent(
                        kind="parameters_stripped",
                        detail=f"stripped {', '.join(removed)} from {mechanism_id}",
                        role="propose",
                    )
                )
            steps.append(step)
        proposal["steps"] = steps
        cleaned.append(proposal)
    return {**parsed, "proposals": cleaned}


def _validate_all(
    parsed: dict[str, Any], library: Library, materiality: Quantity, log: GuardLog
) -> list[PathProposal]:
    raw_items = parsed.get("proposals")
    if not isinstance(raw_items, list):
        raise ValueError("role 2 output has no proposals list")
    proposals: list[PathProposal] = []
    for item in raw_items:
        try:
            draft_steps, missing = _as_steps(item)
        except (ValidationError, KeyError, TypeError) as exc:
            log.record(GuardEvent(kind="invalid_proposal", detail=str(exc), role="propose"))
            continue
        valid, reason = _check(draft_steps, library, materiality)
        proposals.append(PathProposal(steps=draft_steps, missing_links=missing, valid=valid, reason=reason))
    return proposals


def _as_steps(item: dict[str, Any]) -> tuple[list[ProposedStep], list[str]]:
    steps: list[ProposedStep] = []
    for raw in item["steps"]:
        step = dict(raw)
        step["bindings"] = _bindings(step.get("bindings", []))
        steps.append(ProposedStep.model_validate(step))
    missing = [str(link) for link in item.get("missing_links", [])]
    return steps, missing


def _bindings(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return {str(item["name"]): str(item["entity"]) for item in raw}
    raise TypeError("bindings must be an object or a list of name/entity pairs")


def _check(steps: list[ProposedStep], library: Library, materiality: Quantity) -> tuple[bool, str]:
    try:
        path = PropagationPath(steps=_instances(steps, library), materiality_threshold=materiality)
        validate_path(path, library)
    except KeyError as exc:
        return False, f"unknown mechanism {exc}"
    except ValueError as exc:
        return False, str(exc)
    return True, "accepted"


def _instances(steps: list[ProposedStep], library: Library) -> list[MechanismInstance]:
    specs = [library.get(step.mechanism_id) for step in steps]
    instances: list[MechanismInstance] = []
    for index, (step, spec) in enumerate(zip(steps, specs, strict=True)):
        consumes: list[Wire] = []
        if index > 0:
            consumes.append(_wire(specs[index - 1], spec, index - 1))
        instances.append(
            MechanismInstance(mechanism_id=step.mechanism_id, bindings=dict(step.bindings), consumes=consumes)
        )
    return instances


def _wire(previous: MechanismSpec, current: MechanismSpec, previous_index: int) -> Wire:
    for output in previous.outputs:
        for incoming in current.inputs:
            if output.unit == incoming.unit or compatible(output.unit, incoming.unit):
                return Wire(input=incoming.name, from_step=previous_index, output=output.name)
    return Wire(input=current.inputs[0].name, from_step=previous_index, output=previous.outputs[0].name)
