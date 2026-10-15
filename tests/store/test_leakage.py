"""In-memory historical P&L eligibility (spec §10.2 item 6). No store and no network."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from macrosys.store import leakage
from macrosys.store.leakage import HistoricalPnLRow, historical_pnl_eligibility

PUBLISHED = datetime(2024, 6, 3, 12, 30, tzinfo=UTC)


def _row(series_id: str, flag: str, published: datetime = PUBLISHED) -> dict[str, object]:
    return {"series_id": series_id, "quality_flag": flag, "published_at": published, "value": 1.0}


def test_ok_rows_are_eligible() -> None:
    result = historical_pnl_eligibility([_row("TIINGO:SPY", "ok")])

    assert result.exclusions == []
    assert len(result.eligible) == 1
    assert result.eligible[0].series_id == "TIINGO:SPY"
    assert result.eligible[0].quality_flag == "ok"
    assert result.eligible[0].published_at == PUBLISHED


def test_reconstructed_pubtime_is_excluded_with_one_line() -> None:
    result = historical_pnl_eligibility([_row("FRED:GDPC1", "reconstructed_pubtime")])

    assert result.eligible == []
    assert len(result.exclusions) == 1
    exclusion = result.exclusions[0]
    assert exclusion.series_id == "FRED:GDPC1"
    assert exclusion.published_at == PUBLISHED
    assert exclusion.reason == (
        "FRED:GDPC1 excluded from historical P&L: "
        "published_at is reconstructed (quality_flag=reconstructed_pubtime)"
    )
    assert "\n" not in exclusion.reason


def test_mixed_rows_keep_caller_order() -> None:
    later = datetime(2024, 7, 1, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
    rows = [
        _row("TIINGO:SPY", "ok"),
        _row("FRED:GDPC1", "reconstructed_pubtime"),
        _row("CBOE:VIX", "unofficial", later),
        _row("FRED:GDPC1", "reconstructed_pubtime", later),
        HistoricalPnLRow(series_id="BLS:CUSR0000SA0", quality_flag="ok", published_at=PUBLISHED),
    ]

    result = historical_pnl_eligibility(rows)

    assert [row.series_id for row in result.eligible] == ["TIINGO:SPY", "CBOE:VIX", "BLS:CUSR0000SA0"]
    assert result.eligible[1].published_at == later
    assert [item.series_id for item in result.exclusions] == ["FRED:GDPC1", "FRED:GDPC1"]
    assert result.exclusions[1].published_at == later
    assert all("\n" not in item.reason for item in result.exclusions)


def test_other_quality_flags_are_eligible() -> None:
    flags = ("estimated", "parser_changed", "imputed", "unofficial")
    result = historical_pnl_eligibility([_row(f"SRC:{flag}", flag) for flag in flags])

    assert result.exclusions == []
    assert [row.quality_flag for row in result.eligible] == list(flags)


def test_empty_snapshot_has_no_rows() -> None:
    result = historical_pnl_eligibility([])

    assert result.eligible == []
    assert result.exclusions == []


def test_naive_published_at_raises() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        historical_pnl_eligibility([_row("FRED:GDPC1", "ok", datetime(2024, 6, 3, 12, 30))])


def test_null_utcoffset_raises() -> None:
    class _NullOffset(tzinfo):
        def utcoffset(self, dt: datetime | None) -> None:
            return None

    published = datetime(2024, 6, 3, 12, 30, tzinfo=_NullOffset())
    with pytest.raises(ValueError, match="timezone-aware"):
        historical_pnl_eligibility([_row("FRED:GDPC1", "ok", published)])


def test_unknown_quality_flag_raises() -> None:
    with pytest.raises(ValueError, match="quality_flag"):
        historical_pnl_eligibility([_row("FRED:GDPC1", "reconstructed")])


def test_non_mapping_row_raises() -> None:
    with pytest.raises(TypeError, match="mapping"):
        historical_pnl_eligibility(["FRED:GDPC1"])  # type: ignore[list-item]


def test_checker_does_not_load_rows() -> None:
    source = inspect.getsource(leakage)
    assert "latest" not in source
    lowered = source.lower()
    assert "forexfactory" not in lowered
    assert "n-port" not in lowered
    assert "nport" not in lowered
