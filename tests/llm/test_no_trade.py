"""This package has no trade authority (spec §8.3 guardrail c)."""

from __future__ import annotations

import ast
from pathlib import Path

import macrosys.llm as llm

_BANNED = ("macrosys.decide.portfolio", "macrosys.exec")


def test_llm_never_imports_portfolio_or_exec() -> None:
    root = Path(__file__).resolve().parents[2] / "macrosys" / "llm"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules = [node.module]
                if node.module == "macrosys.decide":
                    imported = {alias.name for alias in node.names}
                    assert "portfolio" not in imported
            for module in modules:
                for banned in _BANNED:
                    assert module != banned and not module.startswith(banned + ".")
    assert "OrderTicket" not in llm.__all__
    assert not hasattr(llm, "OrderTicket")
