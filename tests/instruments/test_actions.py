from datetime import date

import numpy as np
import pandas as pd
import pytest

from macrosys.instruments import (
    PRICE_DISCREPANCY_FLAG,
    CorporateAction,
    compare_with_vendor_adjusted,
    total_return_series,
)


def test_total_return_reinvests_dividend_at_ex_close_and_applies_split():
    # Raw closes. $2 cash dividend on Jan 4. 2-for-1 split on Jan 5 (close is already post-split).
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"])
    closes = pd.Series([100.0, 110.0, 108.0, 55.0, 56.0], index=dates, name="close")
    actions = [
        CorporateAction(instrument_id="SPY", ex_date=date(2024, 1, 4), type="cash_div", amount=2.0, ccy="usd"),
        CorporateAction(instrument_id="SPY", ex_date=date(2024, 1, 5), type="split", ratio=2.0),
    ]
    adjusted = total_return_series(closes, actions)
    # Split-adjusted closes: 50, 55, 54, 55, 56. Dividend per split-adjusted share is $1.
    # Backward factor on dates before the ex-date: 54 / (54 + 1).
    expected = np.array([50.0 * 54.0 / 55.0, 54.0, 54.0, 55.0, 56.0])
    assert adjusted.to_numpy() == pytest.approx(expected)
    assert adjusted.iloc[-1] == 56.0
    assert adjusted.name == "adj_close"
    # Ex-date return matches reinvestment at that close: (108 + 2) / 110 - 1 = 0.
    assert adjusted.iloc[2] / adjusted.iloc[1] - 1.0 == pytest.approx(0.0)
    # Split day: one share at 108 becomes two shares at 55.
    assert adjusted.iloc[3] / adjusted.iloc[2] - 1.0 == pytest.approx(110.0 / 108.0 - 1.0)


def test_capital_gain_and_compound_splits():
    dates = pd.to_datetime(["2024-02-01", "2024-02-02"])
    closes = pd.Series([100.0, 100.0], index=dates)
    gain = CorporateAction(
        instrument_id="SPY", ex_date=date(2024, 2, 2), type="capital_gain", amount=5.0, ccy="USD"
    )
    adjusted = total_return_series(closes, [gain])
    assert adjusted.iloc[0] == pytest.approx(100.0 * 100.0 / 105.0)
    assert adjusted.iloc[1] == 100.0
    assert adjusted.iloc[1] / adjusted.iloc[0] - 1.0 == pytest.approx(0.05)

    split_dates = pd.to_datetime(["2024-03-01", "2024-03-04", "2024-03-05"])
    raw = pd.Series([100.0, 100.0, 25.0], index=split_dates)
    splits = [
        CorporateAction(instrument_id="SPY", ex_date=date(2024, 3, 4), type="split", ratio=2.0),
        CorporateAction(instrument_id="SPY", ex_date=date(2024, 3, 5), type="split", ratio=2.0),
    ]
    assert total_return_series(raw, splits).to_numpy() == pytest.approx([25.0, 50.0, 25.0])


def test_vendor_adjusted_flags_relative_differences():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    ours = pd.Series([50.0, 54.0, 54.0], index=dates)
    vendor = ours.copy()
    vendor.iloc[1] = 54.0 * 1.02
    vendor.iloc[2] = 54.0 * 1.005
    assert compare_with_vendor_adjusted(ours, vendor, 0.01) == [date(2024, 1, 3)]
    assert compare_with_vendor_adjusted(ours, vendor, 0.03) == []
    vendor.iloc[0] = np.nan
    flagged = compare_with_vendor_adjusted(ours, vendor, 0.01)
    assert date(2024, 1, 2) in flagged
    assert date(2024, 1, 3) in flagged
    assert PRICE_DISCREPANCY_FLAG == "price_discrepancy"
    with pytest.raises(ValueError, match="ex_date"):
        total_return_series(
            ours,
            [CorporateAction(instrument_id="SPY", ex_date=date(2024, 1, 9), type="split", ratio=2.0)],
        )
    with pytest.raises(ValueError):
        CorporateAction(instrument_id="SPY", ex_date=date(2024, 1, 2), type="split")
