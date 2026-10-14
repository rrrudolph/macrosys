from datetime import UTC, date, datetime

import numpy as np
import pytest

from macrosys.instruments import (
    ExposureVector,
    HoldingRow,
    Instrument,
    apply_currency_hedge,
    exposure_from_holdings,
    net_currency_exposure,
    portfolio_exposure,
)

AS_OF = date(2024, 6, 30)
PUBLISHED = datetime(2024, 8, 1, 12, tzinfo=UTC)


def _row(key: str, weight: float, country: str | None, sector: str | None, ccy: str, asset: str = "equity") -> HoldingRow:
    return HoldingRow(
        instrument_id="FUND",
        as_of=AS_OF,
        published_at=PUBLISHED,
        source="synthetic",
        holding_key=key,
        name=key,
        weight=weight,
        country=country,
        sector=sector,
        ccy=ccy,
        asset_class=asset,  # type: ignore[arg-type]
        market_value=None,
        ccy_mv=None,
    )


def _book() -> list[HoldingRow]:
    return [
        _row("AAPL", 0.40, "US", "tech", "USD"),
        _row("MSFT", 0.25, "US", "tech", "USD"),
        _row("7203", 0.15, "JP", "financials", "JPY"),
        _row("SAP", 0.12, "DE", "industrials", "EUR"),
        _row("SHEL", 0.08, "GB", "energy", "GBP"),
    ]


def test_exposure_sums_and_effective_n():
    weights = np.array([0.40, 0.25, 0.15, 0.12, 0.08])
    vector = exposure_from_holdings(_book())
    assert vector.effective_n == pytest.approx(1.0 / np.square(weights).sum())
    assert vector.top10_weight == pytest.approx(1.0)
    assert vector.country == pytest.approx({"USA": 0.65, "JPN": 0.15, "DEU": 0.12, "GBR": 0.08})
    assert vector.sector["tech"] == pytest.approx(0.65)
    assert vector.ccy["USD"] == pytest.approx(0.65)
    assert vector.ccy["JPY"] == pytest.approx(0.15)
    assert vector.asset_class == pytest.approx({"equity": 1.0})
    assert sum(vector.country.values()) == pytest.approx(1.0)
    assert vector.as_of == AS_OF
    assert vector.source == "synthetic"
    assert vector.underlying is None


def test_currency_hedge_moves_exposure_to_base():
    vector = exposure_from_holdings(_book())
    untouched = apply_currency_hedge(vector, 0.0, "USD")
    assert untouched.ccy == pytest.approx(vector.ccy)
    half = apply_currency_hedge(vector, 0.5, "usd")
    assert half.ccy["JPY"] == pytest.approx(0.075)
    assert half.ccy["EUR"] == pytest.approx(0.06)
    assert half.ccy["GBP"] == pytest.approx(0.04)
    assert half.ccy["USD"] == pytest.approx(0.825)
    assert half.country == vector.country
    assert half.effective_n == vector.effective_n
    full = apply_currency_hedge(vector, 1.0, "USD")
    assert full.ccy == pytest.approx({"USD": 1.0})
    assert net_currency_exposure(full, "USD") == {}
    assert net_currency_exposure(vector, "USD")["JPY"] == pytest.approx(0.15)
    assert "USD" not in net_currency_exposure(vector, "USD")
    assert vector.ccy["JPY"] == pytest.approx(0.15)  # input is not mutated
    with pytest.raises(ValueError):
        apply_currency_hedge(vector, 1.5, "USD")


def test_portfolio_exposure_is_the_weighted_sum():
    fund = Instrument(instrument_id="FUND", ticker="FUND", structure="physical")
    other = Instrument(instrument_id="BOND", ticker="BOND", structure="physical")
    equity = exposure_from_holdings(_book())
    bond = ExposureVector(
        country={"USA": 1.0},
        sector={"rates": 1.0},
        ccy={"USD": 1.0},
        asset_class={"fixed_income": 1.0},
        effective_n=1.0,
        top10_weight=1.0,
        as_of=AS_OF,
        source="issuer_b",
    )
    portfolio = portfolio_exposure([(fund, 0.6, equity), (other, 0.4, bond)])
    assert portfolio.country["USA"] == pytest.approx(0.6 * 0.65 + 0.4)
    assert portfolio.country["JPN"] == pytest.approx(0.09)
    assert portfolio.country["DEU"] == pytest.approx(0.072)
    assert portfolio.country["GBR"] == pytest.approx(0.048)
    assert portfolio.ccy["EUR"] == pytest.approx(0.6 * 0.12)
    assert portfolio.asset_class["equity"] == pytest.approx(0.6)
    assert portfolio.asset_class["fixed_income"] == pytest.approx(0.4)
    assert portfolio.source == "issuer_b+synthetic"
    invested_hhi = (0.6**2) / equity.effective_n + (0.4**2) / 1.0
    assert portfolio.effective_n == pytest.approx(1.0 / invested_hhi)
    assert portfolio.top10_weight == pytest.approx(0.6 * equity.top10_weight + 0.4 * 1.0)

    full = portfolio_exposure([(fund, 0.5, equity), (other, 0.5, bond)])
    half = portfolio_exposure([(fund, 0.25, equity), (other, 0.25, bond)])
    assert half.country["USA"] == pytest.approx(0.5 * full.country["USA"])
    assert sum(half.country.values()) == pytest.approx(0.5)

    doubled = portfolio_exposure([(fund, 0.2, equity), (fund, 0.3, equity)])
    assert doubled.country["USA"] == pytest.approx(0.5 * 0.65)
    assert doubled.effective_n == pytest.approx(equity.effective_n)


def test_futures_exposure_is_the_underlying_not_a_country():
    uso = Instrument(instrument_id="USO", ticker="USO", structure="futures", underlying="WTI")
    row = _row("CL", 1.0, "US", None, "USD", asset="commodity")
    vector = exposure_from_holdings([row], underlying="WTI")
    assert vector.country == {}
    assert vector.underlying == "WTI"
    assert vector.asset_class["commodity"] == pytest.approx(1.0)
    assert vector.ccy["USD"] == pytest.approx(1.0)
    portfolio = portfolio_exposure([(uso, 1.0, vector)])
    assert portfolio.country == {}
    assert portfolio.underlying == "WTI"
    equity = exposure_from_holdings(_book())
    fund = Instrument(instrument_id="FUND", ticker="FUND", structure="physical")
    mixed = portfolio_exposure([(fund, 0.5, equity), (uso, 0.5, vector)])
    assert mixed.underlying is None
    assert mixed.country["USA"] == pytest.approx(0.5 * 0.65)
    assert "JPN" in mixed.country
    with pytest.raises(ValueError, match="underlying"):
        portfolio_exposure(
            [(Instrument(instrument_id="USO", ticker="USO", structure="futures"), 1.0, vector.model_copy(update={"underlying": None}))]
        )
