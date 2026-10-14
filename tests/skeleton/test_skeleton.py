"""Walking skeleton: one decision pass, then the same pass on three shocks (spec §13 S0.1, S0.2)."""

from __future__ import annotations

import ast
import inspect
from datetime import date
from pathlib import Path

import pytest

from macrosys.cli import main
from macrosys.exec.runbook import RunbookBlockedError
from macrosys.pricing.priced_in import PricedIn as MarketPricedIn
from macrosys.skeleton import DEMAND, run_demo, run_once
from macrosys.skeleton.demos import DEMO_SHOCKS
from macrosys.skeleton.run import run_once as run_once_fn

AS_OF = date(2026, 10, 14)


def test_run_once_is_no_trade_and_writes_the_artefacts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = run_once(DEMAND, AS_OF, ledger_path=out / "ledger.jsonl", out_dir=out)
    assert result.decision.action == "no_trade"
    assert result.ledger_entry.entry_hash
    assert result.manifest.digest
    assert result.report_path.is_file()
    assert result.manifest.tables["observation"].rows == 14
    assert result.manifest.tables["world_state"].rows == 5
    text = result.report_path.read_text(encoding="utf-8")
    assert "S-stub-demand" in text
    assert "A04_supply_use" in text
    assert "Sources:" in text
    assert "Confidence: high" in text
    assert "C07" in text
    assert result.ledger_entry.entry_hash in text
    assert result.manifest.digest in text
    assert "quality estimated" in text
    assert abs(result.identity_checks["A04_supply_use"]) < 1e-6
    assert result.thesis.priced_in.source
    assert result.thesis.priced_in.confidence == "high"
    assert any(item.startswith("CME:HG:") for item in result.thesis.priced_in.source)
    market = MarketPricedIn(
        implied_change=result.thesis.priced_in.implied_change,
        sources=list(result.thesis.priced_in.source),
        method_notes=result.thesis.priced_in.method_notes,
        confidence="high",
    )
    assert result.thesis.gap == market.to_gap(result.thesis.expected_effect.change)
    assert result.ledger_entry.thesis().thesis_id == result.thesis.thesis_id


def test_same_inputs_reproduce_the_digest(tmp_path: Path) -> None:
    first = run_once(DEMAND, AS_OF, ledger_path=tmp_path / "a" / "ledger.jsonl", out_dir=tmp_path / "a")
    second = run_once(DEMAND, AS_OF, ledger_path=tmp_path / "b" / "ledger.jsonl", out_dir=tmp_path / "b")
    assert first.digest == second.digest
    assert first.manifest.digest == second.manifest.digest
    assert first.ledger_entry.entry_hash == second.ledger_entry.entry_hash
    assert len(first.digest) == 64
    assert first.report_path.read_bytes() == second.report_path.read_bytes()


def test_three_demos_share_one_path_and_fx_blocks_on_b16(tmp_path: Path) -> None:
    results = run_demo(tmp_path / "demo")
    assert [result.shock_id for result in results] == ["S-stub-demand", "S-stub-fx", "S-stub-transport"]
    assert [result.mechanism_ids for result in results] == [
        ["A04_supply_use", "B03_bottleneck_pricing"],
        ["B16_fx_passthrough"],
        ["A04_supply_use", "B04_inventory_cycle", "B03_bottleneck_pricing"],
    ]
    for result in results:
        assert result.decision.action == "no_trade"
        assert result.ledger_entry.entry_hash
        assert (tmp_path / "demo" / "index.md").is_file()
    fx = results[1]
    assert fx.decision.failing_gate == "mechanism"
    assert fx.missing_parameter == "pt_import"
    blocking = [item for item in fx.thesis.missing_evidence if item.blocking]
    assert any("B16" in item.description and "pt_import" in item.description for item in blocking)
    transport = results[2]
    transport_block = [item for item in transport.thesis.missing_evidence if item.blocking]
    assert any("NoPricedInError" in item.description and "freight" in item.description for item in transport_block)
    assert results[0].thesis.priced_in.source
    assert abs(transport.identity_checks["A04_supply_use"]) < 1e-6


def test_run_once_does_not_branch_on_shock_type() -> None:
    root = Path(__file__).resolve().parents[2] / "macrosys" / "skeleton"
    for path in sorted(root.glob("*.py")):
        if path.name == "routing.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.Compare)):
                text = ast.unparse(node.test if isinstance(node, ast.If) else node)
                assert "shock.type" not in text, f"{path.name} branches on shock.type: {text}"
    assert "shock.type" not in inspect.getsource(run_once_fn)
    assert len(DEMO_SHOCKS) == 3


def test_cli_smoke(tmp_path: Path, capsys) -> None:
    out = tmp_path / "run"
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.0.1"

    assert main(["library", "list"]) == 0
    listed = capsys.readouterr().out
    assert "A04_supply_use\tidentity\tM" in listed
    assert "B03_bottleneck_pricing\tempirical\tE" in listed
    assert "B16_fx_passthrough\tempirical\tE" in listed

    assert main(["library", "validate"]) == 0
    assert capsys.readouterr().out.startswith("ok ")

    assert main(["skeleton", "run", "--shock", "demand", "--as-of", "2026-10-14", "--out", str(out)]) == 0
    assert "no_trade" in capsys.readouterr().out
    assert (out / "report.md").is_file()
    assert (out / "manifest.json").is_file()
    ledger = out / "ledger.jsonl"
    assert main(["ledger", "verify", str(ledger)]) == 0
    assert capsys.readouterr().out.strip() == "ok"

    raw = bytearray(ledger.read_bytes())
    raw[min(20, len(raw) - 1)] ^= 0x01
    ledger.write_bytes(raw)
    assert main(["ledger", "verify", str(ledger)]) == 1
    assert capsys.readouterr().out.strip() == "failed"

    demo = tmp_path / "demo"
    assert main(["skeleton", "demo", "--out", str(demo)]) == 0
    capsys.readouterr()
    assert (demo / "index.md").is_file()
    for name, _shock in DEMO_SHOCKS:
        assert (demo / name / "report.md").is_file()
        assert (demo / name / "ledger.jsonl").is_file()

    assert main(["skeleton"]) == 2
    assert main([]) == 0


def test_refresh_feeds_b03_from_the_capacity_recipe(tmp_path: Path) -> None:
    plain = run_once(DEMAND, AS_OF, ledger_path=tmp_path / "plain" / "ledger.jsonl", out_dir=tmp_path / "plain")
    refreshed = run_once(
        DEMAND,
        AS_OF,
        ledger_path=tmp_path / "refresh" / "ledger.jsonl",
        out_dir=tmp_path / "refresh",
        refresh_world=True,
    )
    assert plain.utilization_provenance == "STUB:UTILIZATION"
    assert plain.gate_reasons["B03_bottleneck_pricing"].startswith("capacity_utilization 0.80")
    assert refreshed.utilization_provenance is not None
    assert refreshed.utilization_provenance.startswith("recipe:usa_capacity_utilization")
    assert refreshed.gate_reasons["B03_bottleneck_pricing"].startswith("capacity_utilization 0.85")


def test_runbook_mismatch_blocks_until_acknowledged(tmp_path: Path) -> None:
    book = tmp_path / "runbook.md"
    book.write_text("reviewed text\n", encoding="utf-8")
    recorded = tmp_path / "runbook.sha256"
    out = tmp_path / "out"
    run_once(
        DEMAND,
        AS_OF,
        ledger_path=out / "ledger.jsonl",
        out_dir=out,
        runbook_path=book,
        runbook_hash_path=recorded,
    )
    book.write_text("unreviewed edit\n", encoding="utf-8")
    with pytest.raises(RunbookBlockedError):
        run_once(
            DEMAND,
            AS_OF,
            ledger_path=tmp_path / "blocked" / "ledger.jsonl",
            out_dir=tmp_path / "blocked",
            runbook_path=book,
            runbook_hash_path=recorded,
        )
    acknowledged = run_once(
        DEMAND,
        AS_OF,
        ledger_path=tmp_path / "acked" / "ledger.jsonl",
        out_dir=tmp_path / "acked",
        runbook_path=book,
        runbook_hash_path=recorded,
        acknowledge_runbook_change=True,
    )
    assert acknowledged.decision.action == "no_trade"
