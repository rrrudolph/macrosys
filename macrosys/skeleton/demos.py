"""Three shocks for the transfer demo (spec §12.1, §12.3, §12.5, §13 S0.2).

The records differ. The pass they go through does not: each one is handed to ``run_once``.
Levels on the demand and transport shocks are the supply-use figures that keep A04's residual
at zero. They are stubs, not measured capex or a measured transit drop. The FX size is the
15 percent depreciation in the §12.3 sketch, labelled ``stub:S0.1``, not a live print.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from macrosys.core.contracts import EntityKey
from macrosys.core.quantity import Quantity
from macrosys.decide.shock import Shock
from macrosys.skeleton.run import SkeletonResult, run_once
from macrosys.skeleton.stubs import STUB_PROVENANCE

AS_OF = date(2026, 10, 14)
_CUTOFF = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
_START = date(2026, 10, 1)


def _shock(
    shock_id: str,
    family: str,
    subtype: str,
    origin: EntityKey,
    size: Quantity,
    stage: str,
) -> Shock:
    return Shock(
        id=shock_id,
        type=family,  # type: ignore[arg-type]
        subtype=subtype,
        origin=origin,
        size=size,
        start=_START,
        stage=stage,
        certainty=0.5,
        evidence_refs=[STUB_PROVENANCE],
        information_cutoff=_CUTOFF,
    )


DEMAND = _shock(
    "S-stub-demand",
    "demand",
    "investment",
    EntityKey(entity_type="economy", entity_id="USA"),
    Quantity(value=15.0, unit="USD_bn", provenance=STUB_PROVENANCE),
    "contracted",
)
FX = _shock(
    "S-stub-fx",
    "price",
    "fx",
    EntityKey(entity_type="currency", entity_id="BRL"),
    Quantity(value=15.0, unit="pct", provenance=STUB_PROVENANCE),
    "observed",
)
TRANSPORT = _shock(
    "S-stub-transport",
    "supply",
    "input_disruption",
    EntityKey(entity_type="commodity", entity_id="freight"),
    Quantity(value=5.0, unit="USD_bn", provenance=STUB_PROVENANCE),
    "observed",
)

# CLI names. Order is fixed so the index is reproducible.
DEMO_SHOCKS: tuple[tuple[str, Shock], ...] = (
    ("demand", DEMAND),
    ("fx", FX),
    ("transport", TRANSPORT),
)


def shock_by_name(name: str) -> Shock:
    """Return one demo shock by its CLI name (spec §13 S0.2)."""
    for key, shock in DEMO_SHOCKS:
        if key == name:
            return shock
    known = ", ".join(key for key, _shock in DEMO_SHOCKS)
    raise KeyError(f"unknown demo shock {name!r} (known: {known})")


def run_demo(
    out_dir: str | Path,
    *,
    as_of: date | datetime = AS_OF,
    library_dir: str | Path = "mechanisms",
    mandate_path: str | Path = "config/mandate.example.yaml",
) -> list[SkeletonResult]:
    """Run the three demo shocks through ``run_once`` and write an index (spec §12, §13 S0.2).

    Each shock gets its own directory and its own ledger. The index lists the decision and the
    run digest. ``as_of`` defaults to 2026-10-14.
    """
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    results: list[SkeletonResult] = []
    lines = [
        "# Skeleton transfer demo",
        "",
        "Same decision loop, three shocks (spec §12, §13 S0.2). "
        "Figures are stubs, not forecasts.",
        "",
    ]
    for name, shock in DEMO_SHOCKS:
        dest = root / name
        result = run_once(
            shock,
            as_of,
            ledger_path=dest / "ledger.jsonl",
            out_dir=dest,
            library_dir=library_dir,
            mandate_path=mandate_path,
        )
        results.append(result)
        gate = result.decision.failing_gate or "none"
        lines.append(
            f"- {name} ({shock.id}): {result.decision.action}, failing gate {gate}, "
            f"digest `{result.digest}`"
        )
        lines.append(f"  - report: {name}/report.md")
        lines.append(f"  - path: {', '.join(result.mechanism_ids)}")
    lines.append("")
    (root / "index.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return results
