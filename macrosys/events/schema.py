"""Typed records for the release event study (spec §10.1 item 11, §4.7 three timestamps)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from macrosys.core.quantity import Quantity

Sign = Literal[-1, 1]


class SurpriseEvent(BaseModel):
    """One scheduled print: the as-reported change against a forecast made from earlier prints only.

    ``published_at`` is the release instant from ALFRED (``reconstructed_pubtime`` when the source
    gave a date only). ``event_date`` is the trading date whose close first reflects the print:
    the release date, or the next day when the release clock is at or after the close cutoff.
    ``actual_change`` is ``first_print(P) - latest_visible(P-1)`` at ``published_at``, so a revision
    of the prior period that arrived with this release is part of the headline, as it was for a
    reader that morning. ``forecast_change`` uses prior events only. ``surprise_sigma`` divides the
    surprise by the standard deviation of earlier surprises and is ``None`` until enough history
    exists. Nothing on this record uses data published after ``published_at``.
    """

    model_config = ConfigDict(frozen=True)

    series_id: str
    ref_period_start: date
    published_at: datetime
    event_date: date
    publication_quality: str
    actual: Quantity
    previous: Quantity
    actual_change: Quantity
    forecast_change: Quantity
    surprise: Quantity
    surprise_sigma: float | None
    forecaster: str
    n_history: int

    @model_validator(mode="after")
    def _aware(self) -> Self:
        if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
            raise ValueError("published_at must be timezone-aware")
        if self.event_date < self.published_at.date():
            raise ValueError("event_date cannot precede the release date")
        return self


class EventOutcome(BaseModel):
    """A market response measured from the close before ``event_date`` to the ``horizon_days``-th close on or after it."""

    model_config = ConfigDict(frozen=True)

    series_id: str
    ref_period_start: date
    market_id: str
    horizon_days: int = Field(ge=1)
    pre_date: date
    post_date: date
    change: Quantity


class RegimeSplit(BaseModel):
    """Coefficient and sign agreement inside one state (spec §3.4 rule 5 tested on data)."""

    model_config = ConfigDict(frozen=True)

    regime: str
    state: bool
    predicted_sign: Sign
    n: int
    n_signed: int
    sign_accuracy: float | None
    beta: Quantity | None
    t_stat: float | None


class ResponseStudy(BaseModel):
    """Result for one release-to-market pair at one horizon (spec §10.1 items 1, 5, 6, 10).

    ``beta`` is the OLS slope of the market change on the standardized surprise, in the market
    change's unit per one standard deviation of surprise, with an HC1 interval. ``sign_accuracy``
    counts events with ``|surprise_sigma| >= sign_threshold`` whose realized sign matched the
    predicted one; ``p_value`` is the two-sided binomial test against one half. ``oos_sign_accuracy``
    uses the sign of ``beta`` fitted on purged training folds to predict the test fold, the
    walk-forward check in §10.2 item 1. ``n_effective`` is ``n`` adjusted for horizon overlap.
    """

    model_config = ConfigDict(frozen=True)

    release_id: str
    market_id: str
    horizon_days: int
    predicted_sign: Sign | None
    mechanisms: list[str]
    reference: str | None
    n: int
    n_effective: float
    n_signed: int
    sign_accuracy: float | None
    p_value: float | None
    beta: Quantity | None
    t_stat: float | None
    r_squared: float | None
    oos_sign_accuracy: float | None
    oos_n: int
    beta_fold_range: float | None
    regimes: list[RegimeSplit] = Field(default_factory=list)
    first_event: date | None
    last_event: date | None
