"""Call trace attached to every role result (spec §8.3).

The trace records the prompt hash, the configured model id, token counts, and the USD cost so a
run can be audited without storing the evidence text.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from macrosys.core.quantity import Quantity
from macrosys.llm.provider import CompletionRequest

RoleName = Literal["extract", "propose", "critique", "narrate"]


class GuardEvent(BaseModel):
    """One guardrail rejection or strip, logged and kept on the role trace (spec §8.3 guardrail a)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    role: str | None = None


class RoleTrace(BaseModel):
    """What one role call cost and which guard events it produced (spec §8.3, §9.7).

    ``cost`` is USD from the configured price table. Token fields count uncached input, output, and
    cache reads separately so a cache hit is not billed twice.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: RoleName
    prompt_hash: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cost: Quantity
    guard_events: list[GuardEvent] = Field(default_factory=list)

    def with_events(self, events: list[GuardEvent]) -> RoleTrace:
        """Return a copy whose guard events are ``events`` (spec §8.3 guardrail a)."""
        return self.model_copy(update={"guard_events": list(events)})


def prompt_hash(request: CompletionRequest) -> str:
    """sha256 of the completion request. The evidence itself is not stored (spec §8.3, §10.4)."""
    payload = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
