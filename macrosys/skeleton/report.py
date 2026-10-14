"""One-page skeleton report (spec §13 S0.1).

The page names the shock, the path with units and horizons, identity residuals, the gap, the
gate that failed, and the two hashes a replay is checked against. It does not choose a path.
"""

from __future__ import annotations

from pathlib import Path

from macrosys.core.quantity import Quantity
from macrosys.core.time import Horizon
from macrosys.decide.gates import Decision
from macrosys.decide.shock import Shock
from macrosys.mechanisms.library import Library
from macrosys.skeleton.stubs import STUB_PROVENANCE, StubReading


def render_report(
    *,
    shock: Shock,
    mechanism_ids: list[str],
    library: Library,
    horizon: Horizon,
    identity_checks: dict[str, float],
    expected: Quantity,
    priced: Quantity,
    gap: Quantity,
    decision: Decision,
    world: tuple[StubReading, ...] | list[StubReading],
    missing_parameter: str | None,
    entry_hash: str,
    manifest_digest: str,
    digest: str,
    priced_sources: list[str],
    priced_notes: str,
    priced_confidence: str | None,
) -> str:
    """Render the one-page decision note for one skeleton run (spec §1.1, §13 S0.1)."""
    lines: list[str] = [
        "# Skeleton decision",
        "",
        "Numbers below are stubs (provenance "
        + STUB_PROVENANCE
        + "). They are not forecasts and not a market reading.",
        "",
        "## Shock",
        "",
        (
            f"{shock.id}: type {shock.type}, subtype {shock.subtype}, stage {shock.stage}, "
            f"origin {shock.origin.entity_type}:{shock.origin.entity_id}."
        ),
        f"Size: {_qty(shock.size)} (provenance {shock.size.provenance or 'none'}).",
        f"Information cutoff on the shock record: {shock.information_cutoff.isoformat()}.",
        "",
        "## World used",
        "",
        "Point-in-time snapshot of estimated stub series (spec §4.3, §9.2):",
    ]
    for row in world:
        lines.append(
            f"- {row.state_var}: {row.value:g} {row.unit} "
            f"({row.series_id}, quality estimated, {STUB_PROVENANCE})"
        )
    lines.extend(["", "## Path", ""])
    for mechanism_id in mechanism_ids:
        lines.append("- " + _step_line(library, mechanism_id))
    lines.append(f"Composed horizon: {_horizon(horizon)}.")
    if missing_parameter is not None:
        lines.append(
            f"Propagation stopped: parameter {missing_parameter} is TODO_estimate "
            "(spec §3.1). No output was produced."
        )
    lines.extend(["", "## Identity residuals", ""])
    if not identity_checks:
        lines.append("Not computed. The path did not finish, so no identity was re-checked.")
    else:
        for mechanism_id, residual in sorted(identity_checks.items()):
            unit = _residual_unit(library, mechanism_id)
            lines.append(f"- {mechanism_id}: {residual:.6g} {unit}".rstrip())
    lines.extend(
        [
            "",
            "## Gap",
            "",
            f"Expected {expected.provenance or 'propagation'}: {_qty(expected)}.",
            _priced_block(priced_sources, priced_notes, priced_confidence, priced),
            f"Gap (expected minus priced): {_qty(gap)}.",
            "",
            "## Gates",
            "",
        ]
    )
    for gate in decision.gates:
        mark = "pass" if gate.passed else "FAIL"
        lines.append(f"- {gate.gate}: {mark} — {gate.reason}")
    lines.extend(["", f"Decision: {decision.action}."])
    if decision.failing_gate is not None:
        lines.append(f"Failing gate: {decision.failing_gate}. {decision.reason}")
    else:
        lines.append("Failing gate: none. All four gates passed.")
    lines.extend(
        [
            "",
            "## Reproducibility",
            "",
            f"Ledger entry hash: {entry_hash}",
            f"Manifest digest: {manifest_digest}",
            f"Run digest: {digest}",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(path: Path, text: str) -> None:
    """Write ``text`` as UTF-8 with ``\\n`` line endings (spec §4.4, byte-stable on one machine)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8", newline="\n")


def _priced_block(sources: list[str], notes: str, confidence: str | None, priced: Quantity) -> str:
    implied = f"Implied change: {_qty(priced)}."
    if confidence is None:
        return f"Priced in blocked. {notes}\n{implied}"
    joined = ", ".join(sources)
    return f"Sources: {joined}.\nMethod: {notes}\nConfidence: {confidence}.\n{implied}"


def _qty(q: Quantity) -> str:
    if q.low is not None and q.high is not None:
        if q.value is None:
            return f"{q.low:g} to {q.high:g} {q.unit}"
        return f"{q.value:g} {q.unit} (range {q.low:g} to {q.high:g})"
    if q.value is None:
        return f"no point {q.unit}"
    return f"{q.value:g} {q.unit}"


def _horizon(horizon: Horizon) -> str:
    decay = "none" if horizon.decay_q is None else f"{horizon.decay_q:g}q"
    return f"impact {horizon.impact_q:g}q, peak {horizon.peak_q:g}q, decay {decay}"


def _step_line(library: Library, mechanism_id: str) -> str:
    spec = library.get(mechanism_id)
    inputs = ", ".join(f"{port.name} ({port.unit})" for port in spec.inputs)
    outputs = ", ".join(f"{port.name} ({port.unit})" for port in spec.outputs)
    decay = "none" if spec.horizon.decay is None else f"{spec.horizon.decay:g}q"
    return (
        f"{spec.id}: inputs {inputs}; outputs {outputs}; "
        f"horizon impact {spec.horizon.impact:g}q, peak {spec.horizon.peak:g}q, decay {decay}"
    )


def _residual_unit(library: Library, mechanism_id: str) -> str:
    spec = library.get(mechanism_id)
    for port in spec.outputs:
        if port.name == "residual":
            return port.unit
    return ""
