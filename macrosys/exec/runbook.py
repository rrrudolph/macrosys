"""Runbook hash gate for the weekly decision job (spec §11.7).

The weekly job must refuse to run when ``docs/runbook.md`` no longer matches the hash recorded
after the last review. ``--acknowledge-runbook-change`` is the review flag: it lets this check
pass once without rewriting the recorded hash. ``record_runbook_hash`` is how a reviewed edit
becomes the new baseline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class RunbookBlockedError(RuntimeError):
    """The weekly decision job must not run: the runbook changed without a review flag (spec §11.7)."""


def runbook_digest(path: Path) -> str:
    """SHA-256 hex digest of the runbook file bytes (spec §11.7)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_runbook_hash(path: Path, expected_hash_file: Path) -> str:
    """Write the current runbook digest to ``expected_hash_file`` and return the hex digest.

    Call this after a human has reviewed an edit. Until then, ``check_runbook`` keeps blocking
    the weekly decision job (spec §11.7).
    """
    digest = runbook_digest(path)
    destination = Path(expected_hash_file)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(digest + "\n", encoding="utf-8")
    return digest


def check_runbook(path: Path, expected_hash_file: Path, *, acknowledge: bool = False) -> str:
    """Return the runbook digest, or raise ``RunbookBlockedError`` when it differs from the recorded hash.

    ``acknowledge=True`` is the ``--acknowledge-runbook-change`` review flag. It lets this call
    pass and does not update the recorded hash; the next unacknowledged check still blocks until
    ``record_runbook_hash`` is run (spec §11.7). A missing hash file is a mismatch.
    """
    digest = runbook_digest(path)
    recorded_path = Path(expected_hash_file)
    recorded = recorded_path.read_text(encoding="utf-8").strip() if recorded_path.exists() else ""
    if digest != recorded and not acknowledge:
        shown = recorded or "(missing)"
        raise RunbookBlockedError(f"runbook hash {digest} does not match recorded {shown}")
    return digest
