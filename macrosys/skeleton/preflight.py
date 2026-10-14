"""Runbook gate for a skeleton decision run (spec §11.7).

The first run records the hash of ``docs/runbook.md``. A later run whose file no longer matches
that hash stops before propagation, unless the caller passes the review flag. Acknowledging
does not rewrite the recorded hash.
"""

from __future__ import annotations

from pathlib import Path

from macrosys.exec.runbook import check_runbook, record_runbook_hash


def ensure_runbook(path: str | Path, hash_file: str | Path, *, acknowledge: bool = False) -> str:
    """Record the runbook hash on first use, or check it (spec §11.7).

    A missing hash file is the first run: the current digest is stored and returned. When a
    hash is already stored, ``check_runbook`` raises ``RunbookBlockedError`` if the file
    changed and ``acknowledge`` is false. Acknowledgement lets this call pass and leaves the
    stored hash as it was.
    """
    book = Path(path)
    recorded = Path(hash_file)
    if not recorded.exists():
        return record_runbook_hash(book, recorded)
    return check_runbook(book, recorded, acknowledge=acknowledge)
