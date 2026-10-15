"""Release event study: scheduled macro prints as the high-count test of mechanism signs (spec §10.1 item 11, E4.4).

The historical case library (§10.1 item 2) has a dozen events. Scheduled releases have a dozen a
month. This package turns ALFRED first prints into surprise events (actual as-reported change minus
a point-in-time statistical forecast), measures market responses at fixed horizons, and tests the
signs the mechanism library predicts for each release-to-market pair, by state where the library
says the sign depends on state. It also estimates response coefficients with provenance, which is
the first data-estimated parameter family in the system (§8.4 "estimated").
"""

from macrosys.events.config import (
    ConsensusConfig,
    ConsensusMapping,
    EventsConfig,
    MarketSpec,
    RegimeSpec,
    ReleaseSpec,
    ResponseSpec,
    load_events_config,
)
from macrosys.events.consensus import ConsensusLookup, read_legacy_sqlite
from macrosys.events.outcomes import market_outcomes
from macrosys.events.schema import ConsensusPrint, EventOutcome, ResponseStudy, SurpriseEvent
from macrosys.events.store import ConsensusStore, SurpriseStore, create_events_schema
from macrosys.events.study import run_study
from macrosys.events.surprise import build_surprises

__all__ = [
    "ConsensusConfig",
    "ConsensusLookup",
    "ConsensusMapping",
    "ConsensusPrint",
    "ConsensusStore",
    "EventOutcome",
    "EventsConfig",
    "MarketSpec",
    "RegimeSpec",
    "ReleaseSpec",
    "ResponseSpec",
    "ResponseStudy",
    "SurpriseEvent",
    "SurpriseStore",
    "build_surprises",
    "create_events_schema",
    "load_events_config",
    "market_outcomes",
    "read_legacy_sqlite",
    "run_study",
]
