"""Load and cross-check the YAML mechanism library (spec §3.1, §3.2, §3.4 rule 3)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from macrosys.mechanisms.registry import registered_codes
from macrosys.mechanisms.spec import MechanismSpec


class Library:
    """The validated set of mechanism records and a content hash of the files they came from.

    ``version`` is the sha256 of the YAML bytes in filename order, with each filename included, so two
    libraries with the same texts and names compare equal (spec §4.4 run tuple).
    """

    def __init__(self, specs: dict[str, MechanismSpec], version: str, root: Path) -> None:
        self._specs = dict(specs)
        self.version = version
        self.root = root

    def get(self, mechanism_id: str) -> MechanismSpec:
        """Return one record or raise ``KeyError`` (spec §3.1)."""
        try:
            return self._specs[mechanism_id]
        except KeyError:
            raise KeyError(mechanism_id) from None

    def ids(self) -> list[str]:
        """Sorted mechanism ids (spec §3.1)."""
        return sorted(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


def _version(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda p: p.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\n")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return digest.hexdigest()


def _class_rules(spec: MechanismSpec) -> None:
    """Identities declare ``tolerance``; every empirical or pricing parameter has a source (spec §3.2)."""
    if spec.class_ == "identity":
        tol = next((p for p in spec.parameters if p.name == "tolerance"), None)
        if tol is None:
            raise ValueError(f"{spec.id}: identity mechanisms must declare a tolerance parameter")
        residual = next((o for o in spec.outputs if o.name == "residual"), None)
        if residual is None:
            raise ValueError(f"{spec.id}: identity mechanisms must output residual")
        if tol.unit != residual.unit:
            raise ValueError(
                f"{spec.id}: tolerance unit {tol.unit} does not match residual unit {residual.unit}"
            )
        return
    for p in spec.parameters:
        if not p.source:
            raise ValueError(f"{spec.id}: {spec.class_} parameter {p.name} requires source")


def load_library(directory: str | Path) -> Library:
    """Load every ``*.yaml`` file in ``directory``, then cross-check the set (spec §3.1, §3.4).

    Each file is validated as a ``MechanismSpec``. After all files parse, every ``competing`` id must
    name another loaded record, and every record must have a registered implementation. Identities
    must declare ``tolerance``. A bad file raises; nothing is silently skipped.
    """
    root = Path(directory)
    files = sorted(root.glob("*.yaml"), key=lambda p: p.name)
    if not files:
        raise ValueError(f"no mechanism yaml files in {root}")

    specs: dict[str, MechanismSpec] = {}
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name}: yaml root must be a mapping")
        spec = MechanismSpec.model_validate(raw)
        if path.name != f"{spec.id}.yaml":
            raise ValueError(f"{path.name}: file name must be {spec.id}.yaml")
        if spec.id in specs:
            raise ValueError(f"duplicate mechanism id {spec.id}")
        _class_rules(spec)
        specs[spec.id] = spec

    unresolved: list[str] = []
    for spec in specs.values():
        for cid in spec.competing:
            if cid not in specs:
                unresolved.append(f"{spec.id} -> {cid}")
    if unresolved:
        raise ValueError("unresolved competing ids: " + "; ".join(unresolved))

    codes = registered_codes()
    missing_impl = [spec.id for spec in specs.values() if spec.code not in codes]
    if missing_impl:
        raise ValueError("no registered implementation for: " + ", ".join(missing_impl))

    return Library(specs, _version(files), root)
