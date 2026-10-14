"""Price table, append-only cost ledger, and the monthly budget guard (spec §8.3, §9.7).

Prices, the monthly cap, the critique reserve, and the tokenizer heuristic are loaded from
configuration. A call is refused when month-to-date spend plus the estimated cost would exceed
the cap. Roles other than critique cannot spend the reserve, so the red-team role keeps a
small allowance. Input tokens are estimated as ``ceil(chars / chars_per_token * tokenizer_factor)``.
The output estimate is the request's ``max_tokens`` cap, which the API will not exceed, so the
30% tokenizer factor is applied only to the character estimate.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.quantity import Quantity
from macrosys.llm.provider import CompletionResult

_MILLION = 1_000_000


class BudgetExceededError(RuntimeError):
    """The call would push this month's spend past the configured USD cap (spec §9.7)."""

    def __init__(self, role: str, estimate: Quantity, spent: Quantity, cap: Quantity) -> None:
        self.role = role
        self.estimate = estimate
        self.spent = spent
        self.cap = cap
        super().__init__(
            f"{role} estimate {estimate.value} {estimate.unit} plus month spend {spent.value} {spent.unit} "
            f"exceeds cap {cap.value} {cap.unit}"
        )


class ModelPrice(BaseModel):
    """USD per million tokens for one model id (spec §8.3). ``cache_read`` may be null."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    cache_read_usd_per_mtok: float | None = Field(default=None, ge=0)


class MaterialityConfig(BaseModel):
    """Path materiality loaded from configuration (spec §3.4 rule 4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float
    unit: str
    provenance: str = Field(min_length=1)

    def quantity(self) -> Quantity:
        """The threshold as a ``Quantity`` (spec §3.4 rule 4)."""
        return Quantity(value=self.value, unit=self.unit, provenance=self.provenance)


class LlmConfig(BaseModel):
    """Model ids, prices, and the monthly budget (spec §8.3, §9.7). None of these are code constants."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: date
    monthly_budget_usd: float = Field(gt=0)
    chars_per_token: float = Field(gt=0)
    tokenizer_factor: float = Field(gt=0)
    critique_reserve_usd: float = Field(ge=0)
    retrieval_token_budget: int = Field(gt=0)
    materiality_threshold: MaterialityConfig
    roles: dict[str, str]
    prices: dict[str, ModelPrice]

    @model_validator(mode="after")
    def _reserve_fits(self) -> LlmConfig:
        if self.critique_reserve_usd >= self.monthly_budget_usd:
            raise ValueError("critique reserve must be smaller than the monthly budget")
        return self

    def model_for(self, role: str) -> str:
        """Configured model id for ``role`` (spec §8.3)."""
        try:
            return self.roles[role]
        except KeyError:
            raise KeyError(f"no model configured for role {role}") from None

    def price_for(self, model: str) -> ModelPrice:
        """Configured price for ``model`` (spec §8.3)."""
        try:
            return self.prices[model]
        except KeyError:
            raise KeyError(f"no price configured for model {model}") from None


class LedgerRow(BaseModel):
    """One billed call. ``cost`` is USD (spec §9.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: datetime
    role: str
    model: str
    provider: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(ge=0)
    cost: Quantity
    prompt_hash: str


def load_llm_config(path: str | Path) -> LlmConfig:
    """Load the price table and budget from YAML (spec §8.3, §9.7)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: yaml root must be a mapping")
    return LlmConfig.model_validate(raw)


def call_cost(config: LlmConfig, result: CompletionResult) -> Quantity:
    """USD for one completion. Cache reads use the cache price, or the input price when it is null.

    [J] Spec §8.3 states input and output prices only. Billing a cache read at the input rate
    does not understate the budget when the config leaves the cache price null.
    """
    price = config.price_for(result.model)
    cache_rate = price.input_usd_per_mtok if price.cache_read_usd_per_mtok is None else price.cache_read_usd_per_mtok
    amount = (
        result.input_tokens * price.input_usd_per_mtok
        + result.output_tokens * price.output_usd_per_mtok
        + result.cache_read_tokens * cache_rate
    ) / _MILLION
    return Quantity(value=amount, unit="USD", provenance=f"{result.provider}:{result.model}")


def estimate_cost(
    config: LlmConfig,
    *,
    model: str,
    prompt_chars: int,
    max_output_tokens: int,
) -> Quantity:
    """USD estimate before the call (spec §8.3 tokenizer note, §9.7).

    Input tokens are ``ceil(prompt_chars / chars_per_token * tokenizer_factor)``. Output tokens
    are ``max_output_tokens``. Cache reads are unknown before the call and are estimated as zero.
    """
    if prompt_chars < 0:
        raise ValueError("prompt_chars must be >= 0")
    if max_output_tokens < 0:
        raise ValueError("max_output_tokens must be >= 0")
    price = config.price_for(model)
    input_tokens = math.ceil(prompt_chars / config.chars_per_token * config.tokenizer_factor)
    amount = (input_tokens * price.input_usd_per_mtok + max_output_tokens * price.output_usd_per_mtok) / _MILLION
    return Quantity(value=amount, unit="USD", provenance=f"estimate:{model}")


class CostLedger:
    """Append-only JSONL of calls (spec §9.7). ``spent`` re-reads the file."""

    def __init__(self, path: str | Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self._now = now if now is not None else (lambda: datetime.now(UTC))
        self._lock = threading.Lock()

    def now(self) -> datetime:
        """Clock used for new rows and for which month ``spent`` selects (spec §9.7)."""
        moment = self._now()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("ledger clock must be timezone-aware")
        return moment

    def append(self, row: LedgerRow) -> None:
        """Append one call. The cost unit must be USD (spec §9.7)."""
        if row.cost.unit != "USD":
            raise ValueError(f"ledger cost unit must be USD, got {row.cost.unit}")
        if row.ts.tzinfo is None or row.ts.utcoffset() is None:
            raise ValueError("ledger timestamp must be timezone-aware")
        payload = row.model_dump(mode="json")
        line = json.dumps(payload, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def rows(self) -> list[LedgerRow]:
        """Every stored call, in append order (spec §9.7)."""
        if not self.path.exists():
            return []
        found: list[LedgerRow] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload: Any = json.loads(line)
            found.append(LedgerRow.model_validate(payload))
        return found

    def spent(self, moment: datetime) -> Quantity:
        """USD spent in the calendar month of ``moment`` (spec §9.7)."""
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("moment must be timezone-aware")
        total = 0.0
        for row in self.rows():
            if row.ts.year == moment.year and row.ts.month == moment.month and row.cost.value is not None:
                total += row.cost.value
        return Quantity(value=total, unit="USD", provenance="cost_ledger")


class BudgetGuard:
    """Refuse a call that would break the month's USD budget (spec §8.3, §9.7).

    Critique may spend the reserve. Every other role stops at ``budget - reserve``.
    """

    def __init__(self, ledger: CostLedger, config: LlmConfig) -> None:
        self.ledger = ledger
        self.config = config

    def cap_for(self, role: str) -> Quantity:
        """USD cap for ``role`` this month, after the critique reserve (spec §9.7)."""
        cap = self.config.monthly_budget_usd
        if role != "critique":
            cap -= self.config.critique_reserve_usd
        return Quantity(value=cap, unit="USD", provenance="config/llm")

    def allow(self, *, role: str, model: str, prompt_chars: int, max_output_tokens: int) -> Quantity:
        """Return the USD estimate, or raise ``BudgetExceededError`` when the month cannot afford it."""
        estimate = estimate_cost(
            self.config, model=model, prompt_chars=prompt_chars, max_output_tokens=max_output_tokens
        )
        spent = self.ledger.spent(self.ledger.now())
        cap = self.cap_for(role)
        spent_value = spent.value if spent.value is not None else 0.0
        estimate_value = estimate.value if estimate.value is not None else 0.0
        cap_value = cap.value if cap.value is not None else 0.0
        if spent_value + estimate_value > cap_value:
            raise BudgetExceededError(role, estimate, spent, cap)
        return estimate
