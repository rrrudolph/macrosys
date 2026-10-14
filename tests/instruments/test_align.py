import math

import pandas as pd
import pytest

from macrosys.instruments import PREMIUM_DISCOUNT_FLAG, aligned_returns, premium_discount


def _frame(dates: list[str], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": closes}, index=pd.to_datetime(dates))


def test_aligned_returns_keep_gaps_as_nan_and_flag_stale():
    bars = {
        "A": _frame(
            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"],
            [100.0, 110.0, 121.0, 133.1, 146.41],
        ),
        "B": _frame(["2024-01-02", "2024-01-08"], [50.0, 55.0]),
    }
    panel = aligned_returns(bars, "union", 2)
    assert panel.stale == ("B",)
    assert "A" not in panel.stale
    b_gap = panel.prices.loc[pd.Timestamp("2024-01-03"):pd.Timestamp("2024-01-05"), "B"]
    assert bool(b_gap.isna().all())
    assert pd.isna(panel.returns.loc[pd.Timestamp("2024-01-02"), "A"])
    assert panel.returns.loc[pd.Timestamp("2024-01-03"), "A"] == pytest.approx(0.1)
    # The day after the hole is NaN: the return is not bridged back to Jan 2, and not filled with 0.
    assert pd.isna(panel.returns.loc[pd.Timestamp("2024-01-08"), "B"])
    assert panel.returns.loc[pd.Timestamp("2024-01-08"), "B"] != 0.0

    logged = aligned_returns(bars, "union", 2, method="log")
    assert logged.returns.loc[pd.Timestamp("2024-01-03"), "A"] == pytest.approx(math.log(1.1))

    narrow = aligned_returns(bars, "intersection", 2)
    assert narrow.stale == ()
    assert list(narrow.prices.index) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-08")]
    assert narrow.returns.loc[pd.Timestamp("2024-01-08"), "B"] == pytest.approx(55.0 / 50.0 - 1.0)


def test_stale_threshold_is_strictly_greater_than_max_gap():
    bars = {
        "A": _frame(["2024-04-01", "2024-04-02", "2024-04-03", "2024-04-04"], [1, 1, 1, 1]),
        "B": _frame(["2024-04-01", "2024-04-04"], [1, 1]),
    }
    assert aligned_returns(bars, "union", 2).stale == ()
    assert aligned_returns(bars, "union", 1).stale == ("B",)


def test_premium_discount_flags_breaches():
    dates = pd.to_datetime(["2024-05-01", "2024-05-02"])
    price = pd.Series([101.0, 103.0], index=dates)
    nav = pd.Series([100.0, 100.0], index=dates)
    premium = premium_discount(price, nav, threshold=0.02)
    assert premium.iloc[0] == pytest.approx(0.01)
    assert premium.iloc[1] == pytest.approx(0.03)
    assert premium.attrs["flag"] == PREMIUM_DISCOUNT_FLAG
    assert premium.attrs["breach_dates"] == [dates[1].date()]
    # Equal to the threshold is not a breach. A flat print is exactly zero.
    exact = premium_discount(nav.iloc[:1], nav.iloc[:1], threshold=0.0)
    assert exact.iloc[0] == pytest.approx(0.0)
    assert exact.attrs["flag"] is None
    assert exact.attrs["breach_dates"] == []
    hole = premium_discount(price, pd.Series([100.0], index=dates[:1]), threshold=0.05)
    assert pd.isna(hole.iloc[1])
    assert hole.iloc[1] != 0.0
