"""Historical P&L eligibility for publication-time leakage (spec §10.2 item 6, §9.2).

The caller supplies rows from a point-in-time snapshot. This module does not load them and
does not score holdings-based exposure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

from macrosys.core.contracts import QualityFlag

# [J] Spec §10.2 item 6 excludes a reconstructed publication time from historical P&L.
# Other quality flags stay eligible here; source tier and thesis gates are separate checks.
_RECONSTRUCTED = "reconstructed_pubtime"


def _require_aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("published_at must be timezone-aware")
    return value


class HistoricalPnLRow(BaseModel):
    """One snapshot row offered for historical profit-and-loss (spec §10.2 item 6)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    series_id: str
    quality_flag: QualityFlag
    published_at: Annotated[datetime, BeforeValidator(_require_aware)]


class HistoricalPnLExclusion(BaseModel):
    """A snapshot row that cannot enter historical profit-and-loss (spec §10.2 item 6)."""

    model_config = ConfigDict(frozen=True)

    series_id: str
    published_at: datetime
    reason: str


class HistoricalPnLEligibility(BaseModel):
    """Rows eligible for historical P&L, and one line of reason for each exclusion (spec §10.2 item 6)."""

    model_config = ConfigDict(frozen=True)

    eligible: list[HistoricalPnLRow]
    exclusions: list[HistoricalPnLExclusion]


def _as_row(row: HistoricalPnLRow | Mapping[str, Any]) -> HistoricalPnLRow:
    if isinstance(row, HistoricalPnLRow):
        return row
    if isinstance(row, Mapping):
        return HistoricalPnLRow.model_validate(row)
    raise TypeError(f"row must be a HistoricalPnLRow or a mapping, got {type(row).__name__}")


def _reason(series_id: str) -> str:
    return (
        f"{series_id} excluded from historical P&L: "
        "published_at is reconstructed (quality_flag=reconstructed_pubtime)"
    )


def historical_pnl_eligibility(
    rows: Sequence[HistoricalPnLRow | Mapping[str, Any]],
) -> HistoricalPnLEligibility:
    """Split caller-supplied snapshot rows into historical P&L eligibility (spec §10.2 item 6, §9.2).

    Each row carries ``series_id``, ``quality_flag``, and ``published_at``. A row whose
    ``quality_flag`` is ``reconstructed_pubtime`` is excluded, with one line stating that its
    publication time was reconstructed rather than observed. A row flagged ``ok`` is eligible.
    The other flags in the quality vocabulary are eligible for this check as well: they are not
    a reconstructed publication time. ``published_at`` must be timezone-aware, or this function
    raises. Input order is preserved within the eligible rows and within the exclusions.
    """
    checked = [_as_row(row) for row in rows]
    eligible: list[HistoricalPnLRow] = []
    exclusions: list[HistoricalPnLExclusion] = []
    for row in checked:
        if row.quality_flag == _RECONSTRUCTED:
            exclusions.append(
                HistoricalPnLExclusion(
                    series_id=row.series_id,
                    published_at=row.published_at,
                    reason=_reason(row.series_id),
                )
            )
        else:
            eligible.append(row)
    return HistoricalPnLEligibility(eligible=eligible, exclusions=exclusions)
