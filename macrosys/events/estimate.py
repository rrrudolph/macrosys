"""Write estimated response coefficients as a parameter file with provenance (spec §8.4 "estimated", §14).

``parameters/event_response.yaml`` is the first parameter family the system estimates from data
rather than curates. Each entry carries the slope, its HC1 interval, the sample, the window, the
forecaster that defined the surprise, and the sign test, so a reader can judge it without rerunning
the study. Entries below the regression minimum are omitted; nothing is written for a pair the
store cannot yet test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from macrosys.events.schema import ResponseStudy


def default_parameters_path() -> Path:
    """Repository ``parameters/event_response.yaml``."""
    return Path(__file__).resolve().parents[2] / "parameters" / "event_response.yaml"


def parameter_entries(results: list[ResponseStudy]) -> list[dict[str, object]]:
    """Serialisable entries for every result that produced a coefficient."""
    entries: list[dict[str, object]] = []
    for item in results:
        if item.beta is None:
            continue
        entries.append(
            {
                "name": f"{item.release_id}->{item.market_id}@{item.horizon_days}d",
                "release": item.release_id,
                "market": item.market_id,
                "horizon_days": item.horizon_days,
                "beta": {
                    "value": round(item.beta.mid, 6),
                    "low": None if item.beta.low is None else round(item.beta.low, 6),
                    "high": None if item.beta.high is None else round(item.beta.high, 6),
                    "unit": item.beta.unit,
                    "per": "1 sd of standardized surprise",
                    "source": item.beta.provenance,
                },
                "t_stat": None if item.t_stat is None else round(item.t_stat, 3),
                "r_squared": None if item.r_squared is None else round(item.r_squared, 4),
                "n": item.n,
                "n_effective": round(item.n_effective, 1),
                "predicted_sign": item.predicted_sign,
                "sign_accuracy": None if item.sign_accuracy is None else round(item.sign_accuracy, 4),
                "n_signed": item.n_signed,
                "p_value": None if item.p_value is None else round(item.p_value, 4),
                "oos_sign_accuracy": None if item.oos_sign_accuracy is None else round(item.oos_sign_accuracy, 4),
                "oos_n": item.oos_n,
                "beta_fold_range": None if item.beta_fold_range is None else round(item.beta_fold_range, 6),
                "mechanisms": list(item.mechanisms),
                "reference": item.reference,
                "window": None
                if item.first_event is None or item.last_event is None
                else f"{item.first_event.isoformat()}..{item.last_event.isoformat()}",
                "regimes": [
                    {
                        "regime": split.regime,
                        "state": split.state,
                        "predicted_sign": split.predicted_sign,
                        "n": split.n,
                        "n_signed": split.n_signed,
                        "sign_accuracy": None if split.sign_accuracy is None else round(split.sign_accuracy, 4),
                        "beta": None if split.beta is None else round(split.beta.mid, 6),
                        "t_stat": None if split.t_stat is None else round(split.t_stat, 3),
                    }
                    for split in item.regimes
                ],
            }
        )
    return entries


def write_parameters(results: list[ResponseStudy], path: str | Path | None = None, generated_at: datetime | None = None) -> Path:
    """Write the coefficient file and return its path. The file is regenerated whole, never edited."""
    target = Path(path) if path is not None else default_parameters_path()
    stamp = generated_at if generated_at is not None else datetime.now(UTC)
    document = {
        "generated_at": stamp.astimezone(UTC).isoformat(),
        "method": (
            "OLS of the market change on the standardized statistical surprise with an intercept; "
            "HC1 standard errors; 95% interval is beta +- 1.96 se. Sign tests count events with "
            "|surprise| >= threshold sd. Out-of-sample accuracy uses the slope sign fitted on purged "
            "walk-forward training folds (spec 10.1 item 11, 10.2 item 1)."
        ),
        "caveat": (
            "Surprises are against a statistical forecast from earlier prints, not a survey consensus "
            "(spec 9.8, P2.4). Coefficients are market responses per 1 sd surprise, not structural "
            "elasticities; they feed pricing-gap timing and B23, not the IO propagation."
        ),
        "entries": parameter_entries(results),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target
