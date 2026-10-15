"""Instrument master, holdings exposure, total return, FX, and aligned returns (spec D0.3, A09)."""

from macrosys.instruments.actions import (
    PRICE_DISCREPANCY_FLAG,
    CorporateAction,
    compare_with_vendor_adjusted,
    total_return_series,
)
from macrosys.instruments.align import (
    PREMIUM_DISCOUNT_FLAG,
    AlignedReturns,
    aligned_returns,
    premium_discount,
)
from macrosys.instruments.cost import round_trip_cost_bp
from macrosys.instruments.exposure import (
    ExposureVector,
    apply_currency_hedge,
    exposure_from_holdings,
    net_currency_exposure,
    portfolio_exposure,
)
from macrosys.instruments.fx import (
    FxConversionRule,
    compose_base_return,
    convert_series,
    decompose_base_return,
)
from macrosys.instruments.holdings import (
    HoldingRow,
    WeightCheck,
    check_weights,
    normalize_ccy,
    normalize_country,
)
from macrosys.instruments.master import (
    DeclaredExposure,
    Instrument,
    MandateExclusionError,
    MandateExclusions,
    OpenFigiClient,
    load_universe,
)

__all__ = [
    "PRICE_DISCREPANCY_FLAG",
    "PREMIUM_DISCOUNT_FLAG",
    "AlignedReturns",
    "CorporateAction",
    "DeclaredExposure",
    "ExposureVector",
    "FxConversionRule",
    "HoldingRow",
    "Instrument",
    "MandateExclusionError",
    "MandateExclusions",
    "OpenFigiClient",
    "WeightCheck",
    "aligned_returns",
    "apply_currency_hedge",
    "check_weights",
    "compare_with_vendor_adjusted",
    "compose_base_return",
    "convert_series",
    "decompose_base_return",
    "exposure_from_holdings",
    "load_universe",
    "net_currency_exposure",
    "normalize_ccy",
    "normalize_country",
    "portfolio_exposure",
    "premium_discount",
    "round_trip_cost_bp",
    "total_return_series",
]
