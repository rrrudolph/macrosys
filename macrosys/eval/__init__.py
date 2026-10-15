"""Evaluation layer: frozen forecast ledger, experiment log, scores, baselines, and walk-forward (spec §10)."""

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

# Walk-forward imports the weekly decision, which imports ``skeleton.run`` for the decision
# clock. ``skeleton.run`` imports the ledger from this package. Loading walk-forward here would
# cycle. The names stay public; they are imported on first use.
_WALKFORWARD = frozenset(
    {
        "Approval",
        "BookScore",
        "RefuseApprover",
        "WalkForwardReport",
        "WalkForwardResult",
        "run",
    }
)


def __getattr__(name: str) -> object:
    """Load walk-forward names on first use so the ledger import does not cycle (spec §10.2)."""
    if name in _WALKFORWARD:
        from macrosys.eval import walkforward

        return getattr(walkforward, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Approval",
    "BacktestResult",
    "BookScore",
    "ExperimentIntegrityError",
    "ExperimentLog",
    "ExperimentRecord",
    "ForecastLedger",
    "LedgerEntry",
    "LedgerIntegrityError",
    "Outcome",
    "RefuseApprover",
    "ResearchCounts",
    "ResearchGateResult",
    "WalkForwardReport",
    "WalkForwardResult",
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
    "run",
    "sign_accuracy",
    "sixty_forty",
    "tsmom_12m",
]
