"""Append-only hashed forecast ledger (spec §10.2 item 7).

Each freeze writes one JSON line: the thesis in canonical form (sorted keys), the UTC timestamp, the
previous line's hash, and ``entry_hash = sha256(prev_hash + canonical_json)``. ``verify`` recomputes
that chain. Editing a stored thesis without rebuilding the hash makes ``verify`` return false. Scores
on this ledger — sign, interval coverage, and Brier where scenario weights were stated — are the record
used for promotion (spec §10.3). Nothing here fetches a price or calls a broker.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, ConfigDict

from macrosys.decide.thesis import Thesis


class LedgerIntegrityError(RuntimeError):
    """Raised when a freeze is asked to append onto a chain that does not verify."""


class Outcome(BaseModel):
    """A resolved forecast. ``realized_change`` is in the same unit as ``expected_effect.change``.

    ``scenario`` is the name of the scenario that occurred, used for the Brier score when every
    scenario on the thesis carried a weight.
    """

    model_config = ConfigDict(frozen=True)

    realized_change: float
    scenario: str | None = None


class LedgerEntry(BaseModel):
    """One frozen thesis and its place in the hash chain (spec §10.2)."""

    model_config = ConfigDict(frozen=True)

    thesis_id: str
    canonical: str
    frozen_at: datetime
    prev_hash: str
    entry_hash: str

    def thesis(self) -> Thesis:
        """Rebuild the thesis from the canonical JSON stored on the line."""
        return Thesis.model_validate_json(self.canonical)


class ForecastLedger:
    """JSONL file of frozen theses. The file is only ever appended to."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def freeze(self, thesis: Thesis, *, frozen_at: datetime | None = None) -> LedgerEntry:
        """Append ``thesis`` and return the new chain entry (spec §10.2, §8.5 ForecastLedger.freeze).

        Refuses to append when the file already exists and ``verify`` fails. ``frozen_at`` defaults to
        the current UTC time; tests pass an explicit timestamp. The hash covers the canonical thesis
        and the previous hash, which is the formula in the ticket: editing the thesis breaks the line.
        """
        if self.path.exists() and not self.verify():
            raise LedgerIntegrityError("refusing to append to a ledger that does not verify")
        stamp = datetime.now(UTC) if frozen_at is None else frozen_at
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("frozen_at must be timezone-aware (UTC)")
        stamp = stamp.astimezone(UTC)
        canonical = canonical_thesis(thesis)
        prev = self._last_hash()
        digest = entry_hash(prev, canonical)
        entry = LedgerEntry(
            thesis_id=thesis.thesis_id,
            canonical=canonical,
            frozen_at=stamp,
            prev_hash=prev,
            entry_hash=digest,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_record(entry), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return entry

    def verify(self) -> bool:
        """Recompute the chain. Return false if any line was edited, reordered, or truncated mid-hash.

        An empty ledger verifies. A JSON line that does not parse verifies as false rather than raising,
        so a tampered file is a failed check (spec §10.2, ticket E4.1).
        """
        if not self.path.exists():
            return True
        prev = ""
        for line in self._lines():
            try:
                obj = json.loads(line)
                canonical = obj["canonical"]
                claimed_prev = obj["prev_hash"]
                claimed = obj["entry_hash"]
            except (KeyError, TypeError, json.JSONDecodeError):
                return False
            if not isinstance(canonical, str) or not isinstance(claimed_prev, str) or not isinstance(claimed, str):
                return False
            if claimed_prev != prev or claimed != entry_hash(claimed_prev, canonical):
                return False
            prev = claimed
        return True

    def entries(self, as_of: date | datetime) -> list[LedgerEntry]:
        """Entries whose ``frozen_at`` is at or before ``as_of`` (a UTC datetime, or the end of a date)."""
        cutoff = _cutoff(as_of)
        return [entry for entry in self._all_entries() if entry.frozen_at <= cutoff]

    def score(self, as_of: date | datetime, outcomes: dict[str, Outcome]) -> pd.DataFrame:
        """Score frozen theses that have a resolved outcome (spec §10.2 item 7, §10.1 item 5).

        Each row is one ledger entry visible at ``as_of``. ``sign_correct`` compares the sign of the
        expected-effect midpoint with the sign of ``realized_change``. ``interval_covered`` is whether
        the realization fell inside ``expected_effect.change``. ``brier`` is the multi-category Brier
        score when every scenario weight was set and the outcome names one of those scenarios;
        otherwise it is missing. Entries with no outcome are omitted.
        """
        columns = [
            "thesis_id",
            "frozen_at",
            "sign_correct",
            "interval_covered",
            "interval_level",
            "brier",
        ]
        rows: list[dict[str, object]] = []
        for entry in self.entries(as_of):
            outcome = outcomes.get(entry.thesis_id)
            if outcome is None:
                continue
            thesis = entry.thesis()
            rows.append(_score_row(entry, thesis, outcome))
        return pd.DataFrame(rows, columns=columns)

    def _last_hash(self) -> str:
        last = ""
        for line in self._lines():
            obj = json.loads(line)
            last = obj["entry_hash"]
        return last

    def _lines(self) -> list[str]:
        if not self.path.exists():
            return []
        text = self.path.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]

    def _all_entries(self) -> list[LedgerEntry]:
        loaded: list[LedgerEntry] = []
        for line in self._lines():
            obj = json.loads(line)
            loaded.append(
                LedgerEntry(
                    thesis_id=obj["thesis_id"],
                    canonical=obj["canonical"],
                    frozen_at=datetime.fromisoformat(obj["frozen_at"]),
                    prev_hash=obj["prev_hash"],
                    entry_hash=obj["entry_hash"],
                )
            )
        return loaded


def canonical_thesis(thesis: Thesis) -> str:
    """JSON object with sorted keys, no insignificant whitespace. This string is what the hash covers."""
    payload = thesis.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev_hash: str, canonical_json: str) -> str:
    """``sha256(prev_hash + canonical_json)`` as a hex digest (spec §10.2)."""
    return hashlib.sha256((prev_hash + canonical_json).encode("utf-8")).hexdigest()


def _record(entry: LedgerEntry) -> dict[str, str]:
    return {
        "thesis_id": entry.thesis_id,
        "canonical": entry.canonical,
        "frozen_at": entry.frozen_at.isoformat(),
        "prev_hash": entry.prev_hash,
        "entry_hash": entry.entry_hash,
    }


def _cutoff(as_of: date | datetime) -> datetime:
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of.astimezone(UTC)
    if isinstance(as_of, date):
        return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59, 999999, tzinfo=UTC)
    raise TypeError("as_of must be a date or a datetime")


def _score_row(entry: LedgerEntry, thesis: Thesis, outcome: Outcome) -> dict[str, object]:
    change = thesis.expected_effect.change
    low = change.value if change.low is None else change.low
    high = change.value if change.high is None else change.high
    if low is None or high is None:
        raise ValueError(f"thesis {thesis.thesis_id} expected_effect.change has no edges")
    predicted = float(_sign(change.mid))
    realized = float(_sign(outcome.realized_change))
    return {
        "thesis_id": thesis.thesis_id,
        "frozen_at": entry.frozen_at,
        "sign_correct": predicted == realized,
        "interval_covered": float(low) <= outcome.realized_change <= float(high),
        "interval_level": thesis.forecast_precision.interval_level,
        "brier": _scenario_brier(thesis, outcome),
    }


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _scenario_brier(thesis: Thesis, outcome: Outcome) -> float:
    weights = [scenario.weight for scenario in thesis.scenarios]
    if any(weight is None for weight in weights) or outcome.scenario is None:
        return float("nan")
    names = [scenario.name for scenario in thesis.scenarios]
    if outcome.scenario not in names:
        return float("nan")
    probability = [float(weight) for weight in weights if weight is not None]
    realized = [1.0 if name == outcome.scenario else 0.0 for name in names]
    return float(sum((p - y) ** 2 for p, y in zip(probability, realized, strict=True)))
