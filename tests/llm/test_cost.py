"""Price table and the monthly budget guard (spec §8.3, §9.7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from macrosys.core.quantity import Quantity
from macrosys.llm.cost import BudgetExceededError, BudgetGuard, CostLedger, LedgerRow, load_llm_config
from macrosys.llm.guard import Toolkit
from macrosys.llm.roles.critique import critique_thesis
from macrosys.llm.roles.extract import extract_shocks
from tests.decide.support import make_thesis
from tests.llm.fakes import ROOT, FakeProvider, load_json, make_run

_WHEN = datetime(2026, 10, 20, tzinfo=UTC)


def _config():
    return load_llm_config(ROOT / "config" / "llm.example.yaml")


def _ledger(path, rows: list[LedgerRow] | None = None) -> CostLedger:
    ledger = CostLedger(path, now=lambda: _WHEN)
    for row in rows or []:
        ledger.append(row)
    return ledger


def _row(config, *, role: str, cost: float, when: datetime = _WHEN) -> LedgerRow:
    return LedgerRow(
        ts=when,
        role=role,
        model=config.model_for(role),
        provider="anthropic",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cost=Quantity(value=cost, unit="USD", provenance="test"),
        prompt_hash="abc",
    )


def test_example_config_carries_the_spec_prices() -> None:
    config = _config()
    assert config.as_of.isoformat() == "2026-10-14"
    assert config.monthly_budget_usd == 25
    assert config.chars_per_token == pytest.approx(4)
    assert config.tokenizer_factor == pytest.approx(1.3)
    assert config.roles == {
        "extract": "claude-haiku-5.5",
        "propose": "claude-sonnet-5.5",
        "critique": "claude-opus-5.5",
        "narrate": "claude-sonnet-5.5",
    }
    haiku = config.price_for("claude-haiku-5.5")
    sonnet = config.price_for("claude-sonnet-5.5")
    opus = config.price_for("claude-opus-5.5")
    gpt5 = config.price_for("gpt-5")
    mini = config.price_for("gpt-5-mini")
    assert (haiku.input_usd_per_mtok, haiku.output_usd_per_mtok) == pytest.approx((0.10, 0.50))
    assert (sonnet.input_usd_per_mtok, sonnet.output_usd_per_mtok) == pytest.approx((2, 10))
    assert (opus.input_usd_per_mtok, opus.output_usd_per_mtok) == pytest.approx((4, 20))
    assert (gpt5.input_usd_per_mtok, gpt5.output_usd_per_mtok) == pytest.approx((1.25, 10))
    assert (mini.input_usd_per_mtok, mini.output_usd_per_mtok) == pytest.approx((0.25, 2))


def test_input_estimate_applies_the_tokenizer_factor(tmp_path) -> None:
    config = _config()
    guard = BudgetGuard(_ledger(tmp_path / "empty.jsonl"), config)
    estimate = guard.allow(
        role="extract",
        model=config.model_for("extract"),
        prompt_chars=10,
        max_output_tokens=0,
    )
    # ceil(10 / 4 * 1.3) = 4 input tokens at the Haiku input price.
    assert estimate.unit == "USD"
    assert estimate.value == pytest.approx(4 * 0.10 / 1_000_000)


def test_budget_refuses_over_spend_and_keeps_the_critique_reserve(tmp_path) -> None:
    config = _config()
    # 24.50 USD is inside the reserve: other roles stop at 24, critique still has room under 25.
    ledger = _ledger(tmp_path / "reserve.jsonl", [_row(config, role="propose", cost=24.5)])
    prior = CostLedger(ledger.path, now=lambda: _WHEN)
    assert prior.spent(_WHEN).value == pytest.approx(24.5)
    guard = BudgetGuard(ledger, config)
    with pytest.raises(BudgetExceededError):
        guard.allow(role="propose", model=config.model_for("propose"), prompt_chars=10, max_output_tokens=16)
    critique_estimate = guard.allow(
        role="critique", model=config.model_for("critique"), prompt_chars=10, max_output_tokens=16
    )
    assert critique_estimate.unit == "USD"
    assert critique_estimate.value is not None and critique_estimate.value > 0

    full = _ledger(tmp_path / "full.jsonl", [_row(config, role="critique", cost=25)])
    with pytest.raises(BudgetExceededError):
        BudgetGuard(full, config).allow(
            role="critique", model=config.model_for("critique"), prompt_chars=10, max_output_tokens=16
        )

    september = datetime(2026, 9, 30, tzinfo=UTC)
    mixed = _ledger(
        tmp_path / "mixed.jsonl",
        [
            _row(config, role="propose", cost=100, when=september),
            _row(config, role="propose", cost=1, when=_WHEN),
        ],
    )
    assert mixed.spent(_WHEN).value == pytest.approx(1)


def test_over_budget_role_does_not_call_the_provider(tmp_path) -> None:
    config = _config()
    guard = BudgetGuard(_ledger(tmp_path / "full.jsonl", [_row(config, role="extract", cost=25)]), config)
    provider = FakeProvider(load_json("extract.json"))
    with pytest.raises(BudgetExceededError):
        extract_shocks(provider, Toolkit(), [], model=config.model_for("extract"), guard=guard, config=config)
    assert provider.requests == []

    reserve = BudgetGuard(_ledger(tmp_path / "reserve.jsonl", [_row(config, role="propose", cost=24.5)]), config)
    critique_provider = FakeProvider(load_json("critique.json"))
    objections, _trace = critique_thesis(
        critique_provider,
        Toolkit(),
        make_thesis(),
        make_run(),
        model=config.model_for("critique"),
        guard=reserve,
        config=config,
    )
    assert critique_provider.requests
    assert len(objections) == 1
