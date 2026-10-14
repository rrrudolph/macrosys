from datetime import UTC, date, datetime

import pytest

from macrosys.instruments import HoldingRow, check_weights, normalize_ccy, normalize_country
from macrosys.instruments.holdings import ISO3_COUNTRIES


def _row(**overrides: object) -> HoldingRow:
    base: dict[str, object] = {
        "instrument_id": "FUND",
        "as_of": date(2024, 6, 30),
        "published_at": datetime(2024, 8, 1, 12, tzinfo=UTC),
        "source": "synthetic",
        "holding_key": "A",
        "name": "Alpha",
        "weight": 1.0,
        "country": "US",
        "sector": "tech",
        "ccy": "USD",
        "asset_class": "equity",
        "market_value": None,
        "ccy_mv": None,
    }
    base.update(overrides)
    return HoldingRow(**base)  # type: ignore[arg-type]


def test_country_and_currency_vocabularies():
    assert len(ISO3_COUNTRIES) >= 61
    assert "EA" in ISO3_COUNTRIES and "USA" in ISO3_COUNTRIES
    assert normalize_country("US") == "USA"
    assert normalize_country("United States") == "USA"
    assert normalize_country("jp") == "JPN"
    assert normalize_country("UK") == "GBR"
    assert normalize_country("Eurozone") == "EA"
    assert normalize_country("EL") == "GRC"
    assert normalize_ccy("usd") == "USD"
    assert normalize_ccy("€") == "EUR"
    assert normalize_ccy("yen") == "JPY"
    assert normalize_ccy("RMB") == "CNY"
    assert normalize_ccy("CNH") == "CNH"
    with pytest.raises(ValueError):
        normalize_country("Narnia")
    with pytest.raises(ValueError):
        normalize_country("EU")
    with pytest.raises(ValueError):
        normalize_ccy("XYZ")
    with pytest.raises(ValueError):
        normalize_ccy("dollar")


def test_holding_row_fields_and_normalization():
    assert set(HoldingRow.model_fields) == {
        "instrument_id",
        "as_of",
        "published_at",
        "source",
        "holding_key",
        "name",
        "weight",
        "country",
        "sector",
        "ccy",
        "asset_class",
        "market_value",
        "ccy_mv",
    }
    row = _row(country="jp", ccy="€", ccy_mv="usd", market_value=10.0)
    assert row.country == "JPN"
    assert row.ccy == "EUR"
    assert row.ccy_mv == "USD"
    with pytest.raises(ValueError, match="timezone"):
        _row(published_at=datetime(2024, 8, 1, 12))


def test_weight_band_and_derivative_flag():
    assert check_weights([_row(weight=0.95)]).flag == "ok"
    assert check_weights([_row(weight=1.05)]).flag == "ok"
    low = check_weights([_row(weight=0.949)])
    assert low.flag == "weight_sum_out_of_band" and low.in_band is False
    high = check_weights([_row(weight=1.051)])
    assert high.flag == "weight_sum_out_of_band"
    derivative = check_weights(
        [_row(weight=0.4, asset_class="derivative", holding_key="FUT", country=None)]
    )
    assert derivative.flag == "derivatives_present"
    assert derivative.derivatives_present is True
    inside = check_weights(
        [
            _row(weight=0.5),
            _row(weight=0.5, asset_class="derivative", holding_key="FUT", country=None),
        ]
    )
    assert inside.flag == "derivatives_present" and inside.in_band is True
