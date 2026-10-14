"""Shared call path: budget check, completion, cost row, prompt hash (spec §8.3, §9.7)."""

from __future__ import annotations

import json
from typing import Any

from macrosys.core.quantity import Quantity
from macrosys.llm.cost import BudgetGuard, CostLedger, LedgerRow, LlmConfig, call_cost
from macrosys.llm.provider import CompletionRequest, CompletionResult, Provider
from macrosys.llm.trace import RoleName, RoleTrace, prompt_hash


def json_object_schema(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    """Object schema with ``additionalProperties: false`` so extra keys are visible (spec §8.3 a)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else required,
    }


def prompt_character_count(request: CompletionRequest) -> int:
    """Characters in the system text, blocks, and schema. The budget heuristic starts here (spec §8.3)."""
    parts = [request.system, json.dumps(request.json_schema, sort_keys=True)]
    parts.extend(block.text for block in request.blocks)
    return sum(len(part) for part in parts)


def call_model(
    *,
    provider: Provider,
    request: CompletionRequest,
    role: RoleName,
    guard: BudgetGuard | None = None,
    ledger: CostLedger | None = None,
    config: LlmConfig | None = None,
) -> tuple[CompletionResult, RoleTrace]:
    """Check the budget, complete, and append the ledger row (spec §8.3, §9.7).

    The estimate uses the character count of the request. The ledger stores the cost of the
    tokens the provider actually reported. ``cost`` is USD.
    """
    if guard is not None:
        guard.allow(
            role=role,
            model=request.model,
            prompt_chars=prompt_character_count(request),
            max_output_tokens=request.max_tokens,
        )
    result = provider.complete(request)
    pricing = config if config is not None else (guard.config if guard is not None else None)
    if pricing is None:
        cost = Quantity(value=0.0, unit="USD", provenance="unpriced")
    else:
        cost = call_cost(pricing, result)
    trace = RoleTrace(
        role=role,
        prompt_hash=prompt_hash(request),
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cost=cost,
    )
    if ledger is not None:
        ledger.append(
            LedgerRow(
                ts=ledger.now(),
                role=role,
                model=result.model,
                provider=result.provider,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cost=cost,
                prompt_hash=trace.prompt_hash,
            )
        )
    return result, trace
