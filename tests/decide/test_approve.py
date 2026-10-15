"""Store approver: exposure, cost, point-in-time vol, and a scenario cap (spec §7.4, §7.5, §11.4)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from macrosys.core.contracts import PriceBar
from macrosys.decide.approve import StoreApprover
from macrosys.decide.baseline import TRADING_DAYS
from macrosys.decide.mandate import Mandate
from macrosys.instruments.holdings import HoldingRow
from macrosys.instruments.master import load_universe
from macrosys.pipeline.jobs import PipelineConfig, PriceGroup
from macrosys.store.holdings import HoldingStore
from macrosys.store.prices import PriceStore
from macrosys.store.schema import create_schema
from tests.decide.support import make_thesis

ROOT = Path(__file__).resolve().parents[2]
MANDATE = Mandate.load(ROOT / "config" / "mandate.yaml")
VOL_WINDOW = 63


def test_remaining_universe_fees_match_the_issuer_pages() -> None:
    book = {row.ticker: row for row in load_universe(ROOT / "config" / "universe.yaml")}
    for ticker in ("XLB", "XLI", "XLF", "XLK", "XLU", "XLV", "XLY", "XLP", "XLRE"):
        assert book[ticker].expense_ratio == 0.0008
        assert book[ticker].exposure is not None
        assert book[ticker].exposure.country == "USA"
        assert book[ticker].exposure.currency == "USD"
        assert book[ticker].exposure.asset_class == "equity"
    assert book["XHB"].expense_ratio == 0.0035
    assert book["XHB"].exposure is not None
    assert book["XHB"].exposure.sector == "homebuilders"
    assert book["BIL"].expense_ratio == 0.001353
    assert book["BIL"].exposure is not None
    assert book["BIL"].exposure.duration_bucket == "1_3m"
    assert book["USO"].expense_ratio == 0.0086
    assert book["UNG"].expense_ratio == 0.0117
    assert book["UNG"].exposure is not None
    assert book["UNG"].exposure.commodity == "natural gas"
    assert book["UUP"].expense_ratio == 0.0078
    assert book["UUP"].exposure is not None
    assert book["UUP"].exposure.currency == "USD"
    assert book["FXE"].expense_ratio == 0.0040
    assert book["FXE"].exposure is not None
    assert book["FXE"].exposure.currency == "EUR"
    assert book["FXY"].expense_ratio == 0.0040
    assert book["FXY"].exposure is not None
    assert book["FXY"].exposure.currency == "JPY"


def test_exposure_match_cost_and_a_positive_weight(tmp_path: Path) -> None:
    approver, conn, as_of, calm = _world(tmp_path)
    try:
        approved = approver.approve(_thesis("EWJ", "economy", "JPN", horizon=6.0), conn, as_of, MANDATE, {})
        assert approved.exposure_match is True
        assert approved.cost_estimate_bp == 34.5
        assert approved.weight > 0.0
        assert approved.post_trade_ok is True
        assert approved.regime_flag in {"stable", "stressed"}
        assert approved.vol_annual == _vol(calm["EWJ"])
        assert math.isfinite(approved.scenario_loss)
        assert approved.scenario_loss > 0.0

        rejected = approver.approve(_thesis("EWJ", "economy", "USA"), conn, as_of, MANDATE, {})
        assert rejected.exposure_match is False
        assert rejected.weight == 0.0
        assert any("USA" in reason for reason in rejected.reasons)
    finally:
        conn.close()


def test_short_with_null_borrow_is_not_post_trade_ok(tmp_path: Path) -> None:
    approver, conn, as_of, _calm = _world(tmp_path)
    try:
        approval = approver.approve(
            _thesis("EWJ", "economy", "JPN", direction="short", gap=(-20_000.0, -20_000.0)),
            conn,
            as_of,
            MANDATE,
            {},
        )
        assert approval.exposure_match is True
        assert approval.post_trade_ok is False
        assert approval.weight == 0.0
        assert "borrow cost not entered on the mandate" in approval.reasons
        assert math.isinf(approval.cost_estimate_bp)
    finally:
        conn.close()


def test_scenario_budget_breach_zeroes_the_weight(tmp_path: Path) -> None:
    approver, conn, as_of, _calm = _world(tmp_path)
    try:
        tight = MANDATE.model_copy(
            update={"risk": MANDATE.risk.model_copy(update={"scenario_loss_budget_pct": 0.0})}
        )
        approval = approver.approve(_thesis("EWJ", "economy", "JPN"), conn, as_of, tight, {})
        assert approval.weight == 0.0
        assert any("scenario budget" in reason for reason in approval.reasons)
    finally:
        conn.close()


def test_vol_ignores_closes_after_as_of(tmp_path: Path) -> None:
    approver, conn, as_of, calm = _world(tmp_path)
    try:
        before = approver.approve(_thesis("EWJ", "economy", "JPN"), conn, as_of, MANDATE, {})
        later = as_of + timedelta(days=5)
        after = approver.approve(_thesis("EWJ", "economy", "JPN"), conn, later, MANDATE, {})
        assert before.vol_annual == _vol(calm["EWJ"])
        assert after.vol_annual != before.vol_annual
    finally:
        conn.close()


def test_holdings_override_a_contradictory_declaration(tmp_path: Path) -> None:
    universe = tmp_path / "universe.yaml"
    _write_configs(tmp_path, ewj_country="USA")
    approver, conn, as_of, _calm = _world(tmp_path, universe=universe)
    try:
        HoldingStore(conn).append(
            [
                HoldingRow(
                    instrument_id="EWJ",
                    as_of=as_of.date() - timedelta(days=20),
                    published_at=as_of - timedelta(days=10),
                    source="test",
                    holding_key="japan-equity",
                    name="Japan equity",
                    weight=1.0,
                    country="JPN",
                    sector=None,
                    ccy="JPY",
                    asset_class="equity",
                )
            ]
        )
        japan = approver.approve(_thesis("EWJ", "economy", "JPN"), conn, as_of, MANDATE, {})
        usa = approver.approve(_thesis("EWJ", "economy", "USA"), conn, as_of, MANDATE, {})
        assert japan.exposure_match is True
        assert usa.exposure_match is False
    finally:
        conn.close()


def test_futures_roll_is_not_costed_at_zero(tmp_path: Path) -> None:
    approver, conn, as_of, _calm = _world(tmp_path)
    try:
        approval = approver.approve(_thesis("CPER", "commodity", "Cu"), conn, as_of, MANDATE, {})
        assert approval.exposure_match is True
        assert approval.post_trade_ok is False
        assert approval.weight == 0.0
        assert any("roll cost" in reason for reason in approval.reasons)
        assert math.isinf(approval.cost_estimate_bp)
    finally:
        conn.close()


def test_store_approver_is_deterministic(tmp_path: Path) -> None:
    approver, conn, as_of, _calm = _world(tmp_path)
    try:
        thesis = _thesis("EWJ", "economy", "JPN")
        first = approver.approve(thesis, conn, as_of, MANDATE, {})
        second = approver.approve(thesis, conn, as_of, MANDATE, {})
        assert first.model_dump() == second.model_dump()
    finally:
        conn.close()


def _thesis(
    instrument: str,
    entity_type: str,
    entity_id: str,
    *,
    direction: str = "long",
    horizon: float = 12.0,
    gap: tuple[float, float] = (20_000.0, 20_000.0),
):
    low, high = gap
    return make_thesis(
        expression={
            "instrument": instrument,
            "direction": direction,
            "exposure_vector_check": "pending",
            "structure_notes": "test expression",
            "cost_estimate_bp": 0.0,
        },
        expected_effect={
            "entity": {"type": entity_type, "id": entity_id},
            "variable": "price",
            "change": {"unit": "bp", "low": low, "high": high},
            "window_q": [1, 4],
        },
        gap={"unit": "bp", "low": low, "high": high},
        expected_return={
            "net_of_costs_range": {"unit": "bp", "low": low, "high": high},
            "horizon_m": horizon,
        },
    )


def _world(
    tmp_path: Path, *, universe: Path | None = None
) -> tuple[StoreApprover, duckdb.DuckDBPyConnection, datetime, dict[str, pd.Series]]:
    path = universe or (tmp_path / "universe.yaml")
    if universe is None:
        _write_configs(tmp_path, ewj_country="JPN")
    calm, jumped = _prices()
    db = tmp_path / "approve.duckdb"
    conn = duckdb.connect(str(db))
    create_schema(conn)
    ingested = datetime(2026, 10, 14, 22, 0, tzinfo=UTC)
    bars = []
    for ticker, series in jumped.items():
        for ts, value in series.items():
            bars.append(
                PriceBar(
                    instrument_id=f"TIINGO:{ticker}",
                    trade_date=pd.Timestamp(ts).date(),
                    open=None,
                    high=None,
                    low=None,
                    close=float(value),
                    volume=None,
                    ccy="USD",
                    source_id="TIINGO",
                    tier="record",
                    ingested_at=ingested,
                )
            )
    PriceStore(conn).append_bars(bars)
    config = PipelineConfig(
        db_path=db,
        raw_root=tmp_path / "raw",
        manifest_dir=tmp_path / "manifests",
        prices=[PriceGroup(source_id="TIINGO", instrument_ids=["SPY", "EWJ", "CPER"])],
    )
    as_of = datetime(pd.Timestamp(calm["EWJ"].index[-1]).year, pd.Timestamp(calm["EWJ"].index[-1]).month, pd.Timestamp(calm["EWJ"].index[-1]).day, 16, 0, tzinfo=UTC)
    return StoreApprover(config, path), conn, as_of, calm


def _write_configs(tmp_path: Path, *, ewj_country: str) -> None:
    instruments = [
        {
            "ticker": "SPY",
            "structure": "physical",
            "role": "factor_global_equity",
            "expense_ratio": 0.000945,
            "exposure": {"asset_class": "equity", "country": "USA", "currency": "USD"},
        },
        {
            "ticker": "EWJ",
            "structure": "physical",
            "expense_ratio": 0.0049,
            "exposure": {"asset_class": "equity", "country": ewj_country, "currency": "JPY"},
        },
        {
            "ticker": "CPER",
            "structure": "futures",
            "underlying": "copper",
            "expense_ratio": 0.0088,
            "exposure": {"asset_class": "commodity", "commodity": "copper"},
        },
    ]
    (tmp_path / "universe.yaml").write_text(
        yaml.safe_dump({"instruments": instruments}, sort_keys=False), encoding="utf-8"
    )
    (tmp_path / "scenarios.yaml").write_text(
        yaml.safe_dump(
            {
                "scenarios": [
                    {
                        "name": "equity_down",
                        "kind": "factor_shock",
                        "factor_moves_pct": {"global_equity": -50},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "liquidity.yaml").write_text(
        yaml.safe_dump(
            {"default_tier": "standard", "half_spread_bp": {"standard": 5.0}, "tiers": {"standard": []}}
        ),
        encoding="utf-8",
    )


def _prices() -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    rng = np.random.default_rng(4)
    index = pd.bdate_range("2024-01-02", periods=320)
    shock = rng.normal(0.0, 0.01, size=len(index))
    calm = {
        "SPY": pd.Series(100.0 * np.exp(np.cumsum(shock)), index=index),
        "EWJ": pd.Series(50.0 * np.exp(np.cumsum(shock)), index=index),
        "CPER": pd.Series(30.0 * np.exp(np.cumsum(rng.normal(0.0, 0.012, size=len(index)))), index=index),
    }
    jump_day = index[-1] + pd.offsets.BDay(1)
    jumped = {
        ticker: pd.concat([series, pd.Series([float(series.iloc[-1]) * 4.0], index=[jump_day])])
        for ticker, series in calm.items()
    }
    return calm, jumped


def _vol(closes: pd.Series) -> float:
    window = closes.pct_change().dropna().iloc[-VOL_WINDOW:]
    return float(window.std(ddof=1) * math.sqrt(TRADING_DAYS))
