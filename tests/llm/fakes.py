"""Canned provider and paths shared by the language-model tests. No network."""

from __future__ import annotations

import json
from pathlib import Path

from macrosys.core.contracts import Gate, PathStepResult
from macrosys.core.quantity import Quantity
from macrosys.llm.provider import CompletionRequest, CompletionResult
from macrosys.mechanisms.compose import PropagationRun

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"


def load_json(name: str) -> dict:
    """Canned role output from ``tests/fixtures/llm``."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeProvider:
    """Returns one canned JSON object. Implements the provider protocol without a network call."""

    def __init__(self, parsed: dict) -> None:
        self.parsed = parsed
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """Echo the canned JSON and the request's model id (spec §8.3)."""
        self.requests.append(request)
        return CompletionResult(
            parsed=self.parsed,
            raw_text=json.dumps(self.parsed),
            input_tokens=20,
            output_tokens=10,
            cache_read_tokens=0,
            provider="fake",
            model=request.model,
            latency_s=0.0,
        )


def make_run() -> PropagationRun:
    """A one-step run whose only number is 45 days of cover."""
    return PropagationRun(
        run_id="R-2026-100",
        library_version="mech-0",
        steps=[
            PathStepResult(
                mechanism_id="B04_inventory_cycle",
                inputs={},
                outputs={"days_of_cover": Quantity(value=45.0, unit="d", provenance="run")},
                gate=Gate(status="on", factor=1.0, reason="approved snapshot"),
            )
        ],
        identity_checks={},
        stopped_branches=[],
        unstable_loops=[],
    )
