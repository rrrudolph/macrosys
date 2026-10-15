"""Load and validate ``config/events.yaml`` (spec §10.1 item 11; parameters live in data, not code)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from macrosys.core.units import assert_unit
from macrosys.mechanisms.spec import MECHANISM_ID

Transform = Literal["pct_change", "diff"]
Response = Literal["pct_change", "diff"]
ClaimLabel = Literal["M", "E", "H", "J"]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReleaseSpec(_Strict):
    """A scheduled release series and how its headline change is formed from consecutive prints."""

    series_id: str
    label: str
    transform: Transform


class MarketSpec(_Strict):
    """A daily market series and how its response is measured."""

    series_id: str
    label: str
    response: Response


class RegimeSpec(_Strict):
    """A point-in-time state rule. ``sahm``: 3-month mean of the latest visible prints minus the 12-month minimum."""

    name: str
    series_id: str
    rule: Literal["sahm"]
    threshold_pp: float = Field(gt=0)


class StateSign(_Strict):
    """A sign that depends on a named regime: ``when_true`` inside the state, ``when_false`` outside it."""

    regime: str
    when_true: Literal[-1, 1]
    when_false: Literal[-1, 1]


class ResponseSpec(_Strict):
    """The sign the library predicts for one release-to-market pair, with its mechanisms and source."""

    release: str
    market: str
    sign: Literal[-1, 1] | StateSign
    mechanisms: list[str] = Field(min_length=1)
    reference: str | None
    label: ClaimLabel

    @field_validator("mechanisms")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        for mechanism_id in value:
            if not MECHANISM_ID.match(mechanism_id):
                raise ValueError(f"{mechanism_id!r} is not a mechanism id")
        return value

    @property
    def regime_name(self) -> str | None:
        return self.sign.regime if isinstance(self.sign, StateSign) else None


class ForecasterSpec(_Strict):
    """How the next headline change is forecast from earlier ones.

    ``trailing_mean``: mean of the last ``window`` changes. ``ar1``: OLS of change on its lag over
    the last ``window`` changes, extrapolated one step, falling back to the trailing mean until
    ``min_fit`` changes exist. A persistent series (CPI month-on-month in 2021-22) makes every
    print a "surprise" against a trailing mean; the AR(1) extrapolates the persistence instead.
    """

    method: Literal["trailing_mean", "ar1"]
    window: int = Field(ge=1)
    min_fit: int = Field(default=12, ge=3)

    @property
    def name(self) -> str:
        return f"{self.method}:{self.window}"


ConsensusKind = Literal["change", "level", "annualized_q_pct"]


class ConsensusMapping(_Strict):
    """How one calendar event's printed numbers become the release's headline change (spec §9.8, P2.4).

    ``kind`` says what the printed number is: ``change`` when it already is the headline change in
    the release's change unit (payrolls in thousands, CPI month-on-month in percent); ``level`` when
    it is the level the release reports (the unemployment rate), so the change is formed against the
    prior print exactly as the actual is; ``annualized_q_pct`` when it is a quarterly rate quoted at an
    annual rate (GDP), de-annualized to the quarterly percent change the store computes. ``unit`` is
    the unit of the printed number; the calendar source dropped the suffix, so it is restated here.
    ``tolerance`` is the largest gap, in change units, allowed between the calendar's printed actual and
    the store's as-reported change before the match is refused; it is the print's rounding resolution.
    """

    release: str
    event: str
    kind: ConsensusKind
    unit: str
    tolerance: float = Field(gt=0)

    @field_validator("unit")
    @classmethod
    def _known_unit(cls, value: str) -> str:
        assert_unit(value)
        return value


class ConsensusConfig(_Strict):
    """The survey-consensus input to the surprise (spec §9.8). Loaded from a local file, never fetched."""

    source_id: str
    forecaster_name: str
    mappings: list[ConsensusMapping] = Field(min_length=1)

    def mapping_for(self, release_id: str) -> ConsensusMapping | None:
        return next((item for item in self.mappings if item.release == release_id), None)

    @property
    def events(self) -> set[str]:
        return {item.event for item in self.mappings}


class EventsConfig(_Strict):
    """Everything the event study reads from configuration (spec §10.1 item 11)."""

    close_cutoff_utc_hour: int = Field(ge=0, le=23)
    max_release_lag_days: int = Field(ge=1)
    horizons_days: list[int] = Field(min_length=1)
    min_history_events: int = Field(ge=2)
    sigma_window: int = Field(ge=2)
    sigma_method: Literal["mad", "std"]
    sigma_cap: float = Field(gt=0)
    sign_threshold_sigma: float = Field(ge=0)
    min_events_for_regression: int = Field(ge=3)
    walk_forward_splits: int = Field(ge=2)
    forecaster: ForecasterSpec
    releases: list[ReleaseSpec] = Field(min_length=1)
    markets: list[MarketSpec] = Field(min_length=1)
    regimes: list[RegimeSpec] = Field(default_factory=list)
    responses: list[ResponseSpec] = Field(min_length=1)
    consensus: ConsensusConfig | None = None

    @model_validator(mode="after")
    def _cross_refs(self) -> Self:
        if any(h < 1 for h in self.horizons_days):
            raise ValueError("horizons_days must be positive")
        if len(set(self.horizons_days)) != len(self.horizons_days):
            raise ValueError("horizons_days must be distinct")
        releases = {item.series_id for item in self.releases}
        markets = {item.series_id for item in self.markets}
        regimes = {item.name for item in self.regimes}
        if len(releases) != len(self.releases):
            raise ValueError("duplicate release series_id")
        if len(markets) != len(self.markets):
            raise ValueError("duplicate market series_id")
        if len(regimes) != len(self.regimes):
            raise ValueError("duplicate regime name")
        seen: set[tuple[str, str]] = set()
        for item in self.responses:
            if item.release not in releases:
                raise ValueError(f"response names unknown release {item.release!r}")
            if item.market not in markets:
                raise ValueError(f"response names unknown market {item.market!r}")
            if item.regime_name is not None and item.regime_name not in regimes:
                raise ValueError(f"response names unknown regime {item.regime_name!r}")
            key = (item.release, item.market)
            if key in seen:
                raise ValueError(f"duplicate response for {item.release} -> {item.market}")
            seen.add(key)
        if self.consensus is not None:
            if self.consensus.forecaster_name == self.forecaster.name:
                raise ValueError("consensus forecaster_name collides with the statistical forecaster name")
            mapped_releases: set[str] = set()
            mapped_events: set[str] = set()
            for mapping in self.consensus.mappings:
                if mapping.release not in releases:
                    raise ValueError(f"consensus mapping names unknown release {mapping.release!r}")
                if mapping.release in mapped_releases:
                    raise ValueError(f"duplicate consensus mapping for release {mapping.release!r}")
                if mapping.event in mapped_events:
                    raise ValueError(f"calendar event {mapping.event!r} is mapped to two releases")
                mapped_releases.add(mapping.release)
                mapped_events.add(mapping.event)
        return self

    def release(self, series_id: str) -> ReleaseSpec:
        return next(item for item in self.releases if item.series_id == series_id)

    def market(self, series_id: str) -> MarketSpec:
        return next(item for item in self.markets if item.series_id == series_id)

    def regime(self, name: str) -> RegimeSpec:
        return next(item for item in self.regimes if item.name == name)


def default_events_config_path() -> Path:
    """Repository ``config/events.yaml``."""
    return Path(__file__).resolve().parents[2] / "config" / "events.yaml"


def load_events_config(path: str | Path | None = None) -> EventsConfig:
    """Parse and validate the events configuration; unknown keys and dangling references are errors."""
    file_path = Path(path) if path is not None else default_events_config_path()
    loaded = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{file_path} must be a mapping")
    return EventsConfig.model_validate(loaded)
