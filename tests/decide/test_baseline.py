"""The resting book (spec §11.3, amended 2026-10-15). No network."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from macrosys.decide.baseline import BaselineUnavailableError, inverse_vol_book
from macrosys.decide.mandate import BaselineConfig

AS_OF = date(2026, 10, 14)


def _closes(days: int, vols: dict[str, float], end: date = AS_OF, seed: int = 7) -> pd.DataFrame:
    """Geometric random walks with the given daily volatilities, ending at ``end``."""
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    frame = {}
    for ticker, vol in vols.items():
        steps = rng.normal(0.0, vol, size=days)
        frame[ticker] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(frame, index=index)


def _config(tickers: list[str], window: int = 20) -> BaselineConfig:
    return BaselineConfig(method="inverse_vol", window_days=window, instruments=tickers)


def test_lower_volatility_gets_more_weight_and_weights_sum_to_one() -> None:
    closes = _closes(60, {"IEF": 0.004, "SPY": 0.012, "DBC": 0.016})
    book = inverse_vol_book(closes, _config(["SPY", "IEF", "DBC"]), AS_OF, position_cap=None)
    assert sum(book.weights.values()) == pytest.approx(1.0)
    assert book.weights["IEF"] > book.weights["SPY"] > book.weights["DBC"]
    assert set(book.vol_annual) == {"SPY", "IEF", "DBC"}
    assert "IEF" in book.sentence() and "%" in book.sentence()


def test_position_cap_is_applied_and_excess_is_spread() -> None:
    closes = _closes(60, {"IEF": 0.002, "SPY": 0.012, "DBC": 0.012, "GLD": 0.012})
    book = inverse_vol_book(closes, _config(["SPY", "IEF", "DBC", "GLD"]), AS_OF, position_cap=0.40)
    assert book.weights["IEF"] == pytest.approx(0.40)
    assert sum(book.weights.values()) == pytest.approx(1.0)
    assert all(weight <= 0.40 + 1e-9 for weight in book.weights.values())


def test_cap_too_tight_to_fill_the_book_is_unavailable() -> None:
    closes = _closes(60, {"IEF": 0.004, "SPY": 0.012})
    with pytest.raises(BaselineUnavailableError, match="cannot sum to 100%"):
        inverse_vol_book(closes, _config(["SPY", "IEF"]), AS_OF, position_cap=0.25)


def test_short_history_missing_column_and_stale_feed_are_unavailable() -> None:
    closes = _closes(60, {"IEF": 0.004, "SPY": 0.012})
    with pytest.raises(BaselineUnavailableError, match="no closes for baseline instruments"):
        inverse_vol_book(closes, _config(["SPY", "GLD"]), AS_OF, None)
    with pytest.raises(BaselineUnavailableError, match="fewer than 70 daily returns"):
        inverse_vol_book(closes, _config(["SPY", "IEF"], window=70), AS_OF, None)
    with pytest.raises(BaselineUnavailableError, match="more than 7 days before"):
        inverse_vol_book(closes, _config(["SPY", "IEF"]), date(2026, 11, 30), None)


def test_rows_after_as_of_are_not_read() -> None:
    closes = _closes(80, {"IEF": 0.004, "SPY": 0.012}, end=date(2026, 11, 13))
    cutoff = date(2026, 10, 14)
    book = inverse_vol_book(closes, _config(["SPY", "IEF"]), cutoff, None)
    trimmed = closes.loc[closes.index.date <= cutoff]  # type: ignore[attr-defined]
    expected = inverse_vol_book(trimmed, _config(["SPY", "IEF"]), cutoff, None)
    assert book.weights == expected.weights
    assert book.as_of == cutoff
