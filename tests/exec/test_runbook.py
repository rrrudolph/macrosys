"""Runbook hash: a mismatch blocks the weekly job until it is acknowledged (spec §11.7)."""

from __future__ import annotations

import hashlib

import pytest

from macrosys.exec.runbook import RunbookBlockedError, check_runbook, record_runbook_hash


def test_hash_mismatch_blocks_until_acknowledged(tmp_path) -> None:
    path = tmp_path / "runbook.md"
    path.write_text("version one\n", encoding="utf-8", newline="\n")
    recorded = tmp_path / "runbook.sha256"
    digest = record_runbook_hash(path, recorded)
    assert digest == hashlib.sha256(b"version one\n").hexdigest()
    assert check_runbook(path, recorded) == digest

    path.write_text("version two\n", encoding="utf-8", newline="\n")
    with pytest.raises(RunbookBlockedError):
        check_runbook(path, recorded)
    acknowledged = check_runbook(path, recorded, acknowledge=True)
    assert acknowledged == hashlib.sha256(b"version two\n").hexdigest()
    with pytest.raises(RunbookBlockedError):
        check_runbook(path, recorded)
