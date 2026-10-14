"""Role 4: narrative for the human (spec §8.3). An approved thesis becomes markdown.

Every number in the markdown must already appear in the thesis, the propagation run, or a
monitoring delta. A number that does not is a failure: the function raises rather than returning
the text. The model is not a source of new claims.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.quantity import Quantity
from macrosys.decide.thesis import Thesis, ThesisStatus
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
from macrosys.llm.numbers import number_tokens
from macrosys.llm.provider import CompletionRequest, ContentBlock, Provider
from macrosys.llm.runtime import call_model, json_object_schema
from macrosys.llm.trace import RoleTrace
from macrosys.mechanisms.compose import PropagationRun

_TASK = (
    "Role 4, narrative (spec §8.3). Write markdown from the approved thesis, the propagation "
    "run, and the monitoring deltas. Every number must already appear in that material. "
    "Do not add claims."
)


class MonitoringDelta(BaseModel):
    """A point already computed for the monitoring note. Its numbers may appear in the narrative."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    series_id: str = Field(min_length=1)
    value: Quantity
    note: str = ""


class Narrative(BaseModel):
    """Human-readable markdown whose numbers trace to the thesis or the run (spec §8.3 role 4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    markdown: str = Field(min_length=1)


class NarrativeNumberError(ValueError):
    """The narrative contains a number that is not in the thesis, the run, or the deltas."""

    def __init__(self, numbers: list[str], trace: RoleTrace) -> None:
        self.numbers = numbers
        self.trace = trace
        joined = ", ".join(numbers)
        super().__init__(f"narrative numbers not in the thesis or run: {joined}")


def narrate_schema() -> dict[str, Any]:
    """JSON schema for role 4 (spec §8.3)."""
    return json_object_schema({"markdown": {"type": "string"}})


def narrate(
    provider: Provider,
    toolkit: Toolkit,
    thesis: Thesis,
    run: PropagationRun,
    deltas: list[MonitoringDelta],
    *,
    model: str,
    guard: BudgetGuard | None = None,
    ledger: CostLedger | None = None,
    config: LlmConfig | None = None,
    max_tokens: int = 1024,
) -> tuple[Narrative, RoleTrace]:
    """Narrate an approved thesis (spec §8.3 role 4). Raises when a number is not in the inputs.

    ``thesis.status`` must be approved. ``toolkit`` is the snapshot boundary. Monitoring deltas
    are evidence, and their numbers are in the allowed set.
    """
    require_toolkit(toolkit)
    if thesis.status is not ThesisStatus.approved:
        raise ValueError("narrative requires an approved thesis (spec §8.3 role 4)")
    log = GuardLog()
    policy = OutputPolicy(log)
    schema = narrate_schema()
    blocks = [
        evidence_block(thesis.thesis_id, thesis.model_dump_json()),
        evidence_block(run.run_id, run.model_dump_json()),
    ]
    for delta in deltas:
        blocks.append(evidence_block(delta.series_id, delta.model_dump_json()))
    blocks.append(ContentBlock(kind="instruction", text="Write the markdown. Use only numbers from the evidence."))
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
        role="narrate",
        guard=guard,
        ledger=ledger,
        config=config,
    )
    try:
        policy.review(result.raw_text, result.parsed, schema, role="narrate")
    except GuardRejectedError:
        trace = trace.with_events(log.events)
        raise
    markdown = str(result.parsed.get("markdown") or "")
    narrative = Narrative(markdown=markdown)
    allowed = number_tokens(thesis.model_dump_json()) | number_tokens(run.model_dump_json())
    for delta in deltas:
        allowed |= number_tokens(delta.model_dump_json())
    extra = sorted(number_tokens(narrative.markdown) - allowed)
    if extra:
        raise NarrativeNumberError(extra, trace.with_events(log.events))
    return narrative, trace.with_events(log.events)


def _dumps(schema: dict[str, Any]) -> str:
    import json

    return json.dumps(schema, sort_keys=True)
