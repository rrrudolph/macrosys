"""Point-in-time release assessment for the gap gate (spec §7.3, §9.2, §10.1 item 11, §11.3).

The decision path asks, as of one timestamp: which standardized surprise was already knowable, how far
the front end had moved, and whether that move has used up the estimated response. ``build_surprises``
and the market vintages are both cut at ``as_of``. The comparison itself is ``check_response`` /
``response_block``; this module does not recompute the slope.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import numpy as np
from pydantic import BaseModel, ConfigDict

from macrosys.core.quantity import Quantity
from macrosys.events.config import EventsConfig, MarketSpec, ReleaseSpec, load_events_config
from macrosys.events.outcomes import market_series, response_change
from macrosys.events.response import check_response, load_responses, response_block
from macrosys.events.schema import SurpriseEvent
from macrosys.events.surprise import build_surprises


class ReleaseAssessment(BaseModel):
    """What was knowable about one release, and whether the gap gate must fail (spec §7.3, §11.3).

    ``blocked`` and ``reason`` are the ``ResponseCheck`` result. ``implied`` and ``residual`` are
    that check's quantities when the slope was applied. ``surprise_sigma`` is the dimensionless
    standardized surprise (standard deviations), the same number ``check_response`` takes; it is
    ``None`` when the event is not yet standardized. ``observed`` is the front-end move in the
    market's response unit. A block that happens before a move exists leaves ``observed`` empty
    rather than substituting zero.
    """

    model_config = ConfigDict(frozen=True)

    blocked: bool
    reason: str
    implied: Quantity | None = None
    residual: Quantity | None = None
    event_date: date | None = None
    surprise_sigma: float | None = None
    observed: Quantity | None = None


def assess_release(
    conn: duckdb.DuckDBPyConnection,
    release_id: str,
    market_id: str,
    horizon_days: int,
    as_of: datetime,
    *,
    config: EventsConfig | None = None,
    parameters: str | Path | None = None,
) -> ReleaseAssessment:
    """Assess one release against one market as of ``as_of`` (spec §7.3, §9.2, §10.1 item 11, §11.3).

    Nothing published after ``as_of`` enters the surprise or the move. The surprise is the latest
    ``build_surprises`` event with ``published_at <= as_of``. The move runs from the close before
    that event's trading date to the last close on or before ``as_of``, and stops at
    ``horizon_days`` trading days, using the response transform ``config`` gives the market so the
    unit matches the slope. A surprise that is not yet standardized blocks without an invented
    sigma. A front end that has not printed a post-event close blocks without treating the move as
    zero. Otherwise the result is ``check_response`` / ``response_block``: a missing slope, a zero
    surprise, an interval that contains zero, or a front end that has already covered the slope's
    conservative edge fails the gap gate.
    """
    if horizon_days < 1:
        raise ValueError(f"horizon_days must be positive, got {horizon_days}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    events_config = config if config is not None else load_events_config()
    release = _named(events_config.releases, release_id, "release")
    market = _named(events_config.markets, market_id, "market")
    book = load_responses(parameters)
    event = _latest_event(conn, release, events_config, as_of)
    if event is None:
        return _blocked(f"no release event for {release_id} is knowable as of {as_of.isoformat()}")
    if event.surprise_sigma is None:
        return _blocked(
            f"surprise is not yet standardized ({release_id} {event.ref_period_start.isoformat()}, "
            f"published {event.published_at.isoformat()})",
            event,
        )
    observed = _observed_move(conn, event, market, horizon_days, as_of)
    if observed is None:
        return _blocked(
            f"front end has not printed ({market_id} after {event.event_date.isoformat()} "
            f"as of {as_of.isoformat()}, horizon {horizon_days}d)",
            event,
        )
    checked = check_response(book, release_id, market_id, horizon_days, event.surprise_sigma, observed)
    gate = response_block(book, release_id, market_id, horizon_days, event.surprise_sigma, observed)
    return ReleaseAssessment(
        blocked=gate is not None,
        reason=gate if gate is not None else checked.reason,
        implied=checked.implied,
        residual=checked.residual,
        event_date=event.event_date,
        surprise_sigma=event.surprise_sigma,
        observed=observed,
    )


def _named[T: ReleaseSpec | MarketSpec](items: list[T], series_id: str, kind: str) -> T:
    found = next((item for item in items if item.series_id == series_id), None)
    if found is None:
        raise ValueError(f"unknown {kind} series {series_id}")
    return found


def _latest_event(
    conn: duckdb.DuckDBPyConnection, release: ReleaseSpec, config: EventsConfig, as_of: datetime
) -> SurpriseEvent | None:
    """Latest surprise whose release instant is knowable at ``as_of`` (spec §9.2)."""
    visible = [event for event in build_surprises(conn, release, config, as_of) if event.published_at <= as_of]
    if not visible:
        return None
    return max(visible, key=lambda event: (event.published_at, event.ref_period_start))


def _observed_move(
    conn: duckdb.DuckDBPyConnection,
    event: SurpriseEvent,
    market: MarketSpec,
    horizon_days: int,
    as_of: datetime,
) -> Quantity | None:
    """Move from the pre-event close to the last knowable close inside the horizon.

    ``None`` when no post-event close has been published by ``as_of``. The window never extends
    past ``horizon_days`` trading days, matching how the study counts a horizon (spec §10.1 item 11).
    """
    series = market_series(conn, market, as_of)
    if series.empty:
        return None
    as_of_day = as_of.astimezone(UTC).date()
    rows = [
        (day, float(value), str(unit))
        for day, value, unit in zip(series["date"].tolist(), series["value"].tolist(), series["unit"].tolist(), strict=True)
        if day <= as_of_day
    ]
    if not rows:
        return None
    dates = np.array([row[0] for row in rows], dtype=object)
    first = int(np.searchsorted(dates, event.event_date, side="left"))
    if first >= len(rows):
        return None
    pre_index = first - 1
    if pre_index < 0:
        raise ValueError(f"no market close before {event.event_date.isoformat()} is knowable for {market.series_id}")
    post_index = min(first + horizon_days - 1, len(rows) - 1)
    pre_day, pre_value, pre_unit = rows[pre_index]
    post_day, post_value, post_unit = rows[post_index]
    if pre_unit != post_unit:
        raise ValueError(f"{market.series_id}: unit changed inside the outcome window")
    return response_change(
        pre_value,
        post_value,
        post_unit,
        market.response,
        provenance=f"{market.series_id}:{pre_day.isoformat()}..{post_day.isoformat()}",
    )


def _blocked(reason: str, event: SurpriseEvent | None = None) -> ReleaseAssessment:
    return ReleaseAssessment(
        blocked=True,
        reason=reason,
        event_date=None if event is None else event.event_date,
        surprise_sigma=None if event is None else event.surprise_sigma,
    )
