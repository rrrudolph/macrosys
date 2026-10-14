"""Data-source connectors (spec §9.1–9.6).

`SOURCES` maps each implemented registry `source_id` to its connector class. Rate limits and tiers
are read from `config/source_registry.yaml` inside those classes.
"""

from macrosys.sources.bea import BeaSource
from macrosys.sources.bls import BlsSource
from macrosys.sources.cboe import CboeSource
from macrosys.sources.census import CensusSource
from macrosys.sources.cftc import CftcSource
from macrosys.sources.comtrade import ComtradeSource
from macrosys.sources.ecb import EcbSource
from macrosys.sources.eia import EiaSource
from macrosys.sources.fred import FredSource
from macrosys.sources.imf import ImfSource
from macrosys.sources.mt5 import MT5Source
from macrosys.sources.nyfed import NyFedSource
from macrosys.sources.oecd import OecdSource
from macrosys.sources.portwatch import PortWatchSource
from macrosys.sources.sec import SecSource
from macrosys.sources.tiingo import TiingoSource
from macrosys.sources.tradingview import TradingViewSource
from macrosys.sources.treasury import FedGswSource, TreasurySource
from macrosys.sources.worldbank import WorldBankSource

SOURCES: dict[str, type] = {
    FredSource.source_id: FredSource,
    BlsSource.source_id: BlsSource,
    BeaSource.source_id: BeaSource,
    CensusSource.source_id: CensusSource,
    EcbSource.source_id: EcbSource,
    ImfSource.source_id: ImfSource,
    OecdSource.source_id: OecdSource,
    ComtradeSource.source_id: ComtradeSource,
    CftcSource.source_id: CftcSource,
    SecSource.source_id: SecSource,
    TreasurySource.source_id: TreasurySource,
    FedGswSource.source_id: FedGswSource,
    PortWatchSource.source_id: PortWatchSource,
    EiaSource.source_id: EiaSource,
    NyFedSource.source_id: NyFedSource,
    WorldBankSource.source_id: WorldBankSource,
    CboeSource.source_id: CboeSource,
    TiingoSource.source_id: TiingoSource,
    MT5Source.source_id: MT5Source,
    TradingViewSource.source_id: TradingViewSource,
}

__all__ = [
    "SOURCES",
    "BeaSource",
    "BlsSource",
    "CboeSource",
    "CensusSource",
    "CftcSource",
    "ComtradeSource",
    "EcbSource",
    "EiaSource",
    "FedGswSource",
    "FredSource",
    "ImfSource",
    "MT5Source",
    "NyFedSource",
    "OecdSource",
    "PortWatchSource",
    "SecSource",
    "TiingoSource",
    "TradingViewSource",
    "TreasurySource",
    "WorldBankSource",
]
