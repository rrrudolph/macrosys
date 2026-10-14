"""CLI smoke: a fake parser wires every exec subcommand (spec §11.7)."""

from __future__ import annotations

import argparse

from macrosys.exec.cli_commands import register_cli
from macrosys.exec.runbook import record_runbook_hash


def test_cli_smoke(tmp_path) -> None:
    parser = argparse.ArgumentParser()
    register_cli(parser.add_subparsers(dest="cmd"))
    db = tmp_path / "oms.sqlite"
    flag = tmp_path / "KILL"
    common = ["--db", str(db), "--kill-flag", str(flag)]
    commands = [
        ["exec", "submit", "--tickets", str(tmp_path / "tickets.json"), *common],
        ["exec", "poll", *common],
        ["exec", "reconcile", "--tol", "0.01", *common],
        ["exec", "kill", "--reason", "drill", *common],
        ["exec", "unkill", "--ack", "checked the book", *common],
        ["exec", "status", *common],
        ["exec", "runbook-check", "--runbook", str(tmp_path / "runbook.md"), "--hash-file", str(tmp_path / "hash.txt")],
    ]
    for argv in commands:
        args = parser.parse_args(argv)
        assert callable(args.func)

    status = parser.parse_args(["exec", "status", *common])
    assert status.func(status) == 0

    runbook = tmp_path / "runbook.md"
    runbook.write_text("procedure\n", encoding="utf-8")
    recorded = tmp_path / "hash.txt"
    record_runbook_hash(runbook, recorded)
    ok = parser.parse_args(["exec", "runbook-check", "--runbook", str(runbook), "--hash-file", str(recorded)])
    assert ok.func(ok) == 0
    runbook.write_text("edited\n", encoding="utf-8")
    blocked = parser.parse_args(["exec", "runbook-check", "--runbook", str(runbook), "--hash-file", str(recorded)])
    assert blocked.func(blocked) == 2
    acked = parser.parse_args(
        [
            "exec",
            "runbook-check",
            "--runbook",
            str(runbook),
            "--hash-file",
            str(recorded),
            "--acknowledge-runbook-change",
        ]
    )
    assert acked.func(acked) == 0
