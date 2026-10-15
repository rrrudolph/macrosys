"""Append-only experiment log (spec §10.2 item 5).

Separate from the forecast ledger. Every backtest variant is one JSON line. The line's
``entry_hash`` is ``sha256(prev_hash + canonical record)``, the same chain rule the forecast
ledger uses for theses; this module does not change that ledger's hash format. The canonical
record includes ``trial_count``, so editing a stored trial count without rebuilding the chain
makes ``verify`` raise. The deflated Sharpe is ``macrosys.eval.scoring.deflated_sharpe_ratio``
(Bailey & López de Prado 2014). This module does not define another deflator. The trial count
passed into that function is the number of variants of the same hypothesis already on the log,
including the line being appended.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from macrosys.core.quantity import Quantity
from macrosys.eval.scoring import deflated_sharpe_ratio


class ExperimentIntegrityError(RuntimeError):
    """Raised when the experiment log's hash chain does not match the stored lines."""


class ExperimentRecord(BaseModel):
    """One logged backtest variant and its place in the hash chain (spec §10.2 item 5).

    ``sharpe`` and ``deflated_sharpe`` are dimensionless ratios (``macrosys.core.units`` registers
    ``ratio``). ``deflated_sharpe.value`` is the float returned by ``deflated_sharpe_ratio``; that
    function still takes and returns a bare float, and the quantity records the unit around it.
    ``trial_count`` is the integer ``n_trials`` argument, a count of logged variants of this
    hypothesis, not an economic magnitude.
    """

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    hypothesis: str
    variant: str
    trial_count: int
    sharpe: Quantity
    deflated_sharpe: Quantity
    code_commit: str
    parameter_set: str
    prev_hash: str
    entry_hash: str


class ExperimentLog:
    """JSONL file of backtest variants. The file is only ever appended to."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        experiment_id: str,
        hypothesis: str,
        variant: str,
        sharpe: Quantity,
        code_commit: str,
        parameter_set: str,
        n: int,
        skew: float,
        kurt: float,
        var_sr_trials: float,
    ) -> ExperimentRecord:
        """Append one variant and return the new chain entry (spec §10.2 item 5).

        Refuses to append when the file already exists and ``verify`` fails. ``n``, ``skew``,
        ``kurt``, and ``var_sr_trials`` are the arguments ``deflated_sharpe_ratio`` already takes.
        They are not economic magnitudes, this log does not estimate them, and it does not default
        them. ``n_trials`` is not one of those caller arguments: it is one plus the number of
        variants of ``hypothesis`` already stored, so a second variant of the same hypothesis is
        deflated with a larger trial count. A different hypothesis starts again at one. With one
        trial, or with ``var_sr_trials`` of zero, the existing function's benchmark is zero; this
        log does not override that.
        """
        if self.path.exists():
            self.verify()
        experiment_id = _one_line("experiment_id", experiment_id)
        hypothesis = _one_line("hypothesis", hypothesis)
        variant = _one_line("variant", variant)
        code_commit = _one_line("code_commit", code_commit)
        parameter_set = _one_line("parameter_set", parameter_set)
        observed = _ratio_value("sharpe", sharpe)
        existing = self._load()
        if any(record.experiment_id == experiment_id for record in existing):
            raise ValueError(f"experiment_id {experiment_id!r} is already in the log")
        trial_count = 1 + sum(record.hypothesis == hypothesis for record in existing)
        deflated = deflated_sharpe_ratio(
            observed,
            n_trials=trial_count,
            var_sr_trials=var_sr_trials,
            n=n,
            skew=skew,
            kurt=kurt,
        )
        deflated_sharpe = Quantity(
            value=deflated,
            unit="ratio",
            provenance="Bailey & López de Prado 2014; macrosys.eval.scoring.deflated_sharpe_ratio",
        )
        canonical = canonical_experiment(
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            variant=variant,
            trial_count=trial_count,
            sharpe=sharpe,
            deflated_sharpe=deflated_sharpe,
            code_commit=code_commit,
            parameter_set=parameter_set,
        )
        prev = existing[-1].entry_hash if existing else ""
        digest = _chain_hash(prev, canonical)
        record = ExperimentRecord(
            experiment_id=experiment_id,
            hypothesis=hypothesis,
            variant=variant,
            trial_count=trial_count,
            sharpe=sharpe,
            deflated_sharpe=deflated_sharpe,
            code_commit=code_commit,
            parameter_set=parameter_set,
            prev_hash=prev,
            entry_hash=digest,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_line(record, canonical), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return record

    def verify(self) -> bool:
        """Recompute the hash chain (spec §10.2 item 5).

        An empty or missing file returns True. A line that does not parse, a ``prev_hash`` that
        does not continue the previous line, or an ``entry_hash`` that does not equal
        ``sha256(prev_hash + canonical)`` raises ``ExperimentIntegrityError`` and names the line.
        Editing ``trial_count`` inside the stored canonical record without rebuilding the chain
        fails here. Returns True only when every line matches.
        """
        if not self.path.exists():
            return True
        prev = ""
        for index, line in enumerate(self._lines(), start=1):
            try:
                obj = json.loads(line)
                canonical = obj["canonical"]
                claimed_prev = obj["prev_hash"]
                claimed = obj["entry_hash"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ExperimentIntegrityError(
                    f"experiment log hash chain broken at line {index}: line is not a valid record"
                ) from exc
            if not isinstance(canonical, str) or not isinstance(claimed_prev, str) or not isinstance(claimed, str):
                raise ExperimentIntegrityError(
                    f"experiment log hash chain broken at line {index}: line is not a valid record"
                )
            if claimed_prev != prev:
                raise ExperimentIntegrityError(
                    f"experiment log hash chain broken at line {index}: prev_hash does not continue the chain"
                )
            if claimed != _chain_hash(claimed_prev, canonical):
                raise ExperimentIntegrityError(
                    f"experiment log hash chain broken at line {index}: "
                    "stored record does not match entry_hash (trial_count and the other canonical "
                    "fields are covered; rebuild the chain after an edit)"
                )
            prev = claimed
        return True

    def records(self) -> list[ExperimentRecord]:
        """Return every stored variant in file order (spec §10.2 item 5).

        Raises ``ExperimentIntegrityError`` when the hash chain does not verify, so a tampered
        trial count is not returned as data.
        """
        self.verify()
        return self._load()

    def _load(self) -> list[ExperimentRecord]:
        loaded: list[ExperimentRecord] = []
        for line in self._lines():
            obj = json.loads(line)
            loaded.append(_record_from_line(obj))
        return loaded

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]


def canonical_experiment(
    *,
    experiment_id: str,
    hypothesis: str,
    variant: str,
    trial_count: int,
    sharpe: Quantity,
    deflated_sharpe: Quantity,
    code_commit: str,
    parameter_set: str,
) -> str:
    """JSON object with sorted keys and no insignificant whitespace (spec §10.2 item 5).

    This string is what the experiment-log hash covers, including ``trial_count``. The digest is
    ``sha256(prev_hash + canonical)``, the same formula as the forecast ledger's entry hash. The
    ledger's own hash format is unchanged.
    """
    payload = {
        "code_commit": code_commit,
        "deflated_sharpe": deflated_sharpe.model_dump(mode="json"),
        "experiment_id": experiment_id,
        "hypothesis": hypothesis,
        "parameter_set": parameter_set,
        "sharpe": sharpe.model_dump(mode="json"),
        "trial_count": trial_count,
        "variant": variant,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chain_hash(prev_hash: str, canonical_json: str) -> str:
    """``sha256(prev_hash + canonical_json)`` as a hex digest (spec §10.2)."""
    return hashlib.sha256((prev_hash + canonical_json).encode("utf-8")).hexdigest()


def _line(record: ExperimentRecord, canonical: str) -> dict[str, str]:
    return {
        "canonical": canonical,
        "prev_hash": record.prev_hash,
        "entry_hash": record.entry_hash,
    }


def _record_from_line(obj: dict[str, object]) -> ExperimentRecord:
    canonical = obj["canonical"]
    if not isinstance(canonical, str):
        raise ExperimentIntegrityError("experiment log line has a non-string canonical record")
    payload = json.loads(canonical)
    return ExperimentRecord(
        experiment_id=payload["experiment_id"],
        hypothesis=payload["hypothesis"],
        variant=payload["variant"],
        trial_count=payload["trial_count"],
        sharpe=Quantity.model_validate(payload["sharpe"]),
        deflated_sharpe=Quantity.model_validate(payload["deflated_sharpe"]),
        code_commit=payload["code_commit"],
        parameter_set=payload["parameter_set"],
        prev_hash=str(obj["prev_hash"]),
        entry_hash=str(obj["entry_hash"]),
    )


def _one_line(label: str, value: str) -> str:
    if not isinstance(value, str) or value == "" or "\n" in value or "\r" in value:
        raise ValueError(f"{label} must be a non-empty single line")
    return value


def _ratio_value(label: str, quantity: Quantity) -> float:
    if not isinstance(quantity, Quantity):
        raise TypeError(f"{label} must be a Quantity with unit 'ratio'")
    if quantity.unit != "ratio":
        raise ValueError(f"{label} must use unit 'ratio', got {quantity.unit!r}")
    if quantity.value is None:
        raise ValueError(f"{label} needs a point value; deflated_sharpe_ratio takes a float")
    return quantity.value
