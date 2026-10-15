"""Evaluation layer: frozen forecast ledger, experiment log, scores, and baselines (spec §10)."""

from macrosys.eval.baselines import (
    BacktestResult,
    backtest,
    equal_weight,
    inverse_vol,
    sixty_forty,
    tsmom_12m,
)
from macrosys.eval.experiment_log import (
    ExperimentIntegrityError,
    ExperimentLog,
    ExperimentRecord,
)
from macrosys.eval.ledger import (
    ForecastLedger,
    LedgerEntry,
    LedgerIntegrityError,
    Outcome,
)
from macrosys.eval.promotion import ResearchCounts, ResearchGateResult, research_to_paper
from macrosys.eval.scoring import (
    brier,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    interval_coverage,
    log_score,
    probabilistic_sharpe_ratio,
    purged_walk_forward_splits,
    rank_correlation,
    reliability_table,
    sign_accuracy,
)

__all__ = [
    "BacktestResult",
    "ExperimentIntegrityError",
    "ExperimentLog",
    "ExperimentRecord",
    "ForecastLedger",
    "LedgerEntry",
    "LedgerIntegrityError",
    "Outcome",
    "ResearchCounts",
    "ResearchGateResult",
    "backtest",
    "brier",
    "deflated_sharpe_ratio",
    "equal_weight",
    "expected_max_sharpe",
    "interval_coverage",
    "inverse_vol",
    "log_score",
    "probabilistic_sharpe_ratio",
    "purged_walk_forward_splits",
    "rank_correlation",
    "reliability_table",
    "research_to_paper",
    "sign_accuracy",
    "sixty_forty",
    "tsmom_12m",
]
