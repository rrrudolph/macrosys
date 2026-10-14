"""File-backed kill switch (spec §11.7).

``engage`` writes the flag before it cancels, so a crash still leaves submission disabled.
``OrderManager.submit`` refuses while the flag exists. ``disengage`` will not clear the flag
without a non-empty acknowledgement, and that acknowledgement is stored in ``incidents``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class _SwitchBook(Protocol):
    def cancel_all(self) -> list[object]: ...

    def record_incident(self, failure_class: str, kind: str, detail: str, *, resolved: bool = False) -> int: ...

    def resolve_incidents(self, kind: str, *, note: str) -> None: ...


class KillSwitch:
    """A flag file that cancels working orders and blocks new submits (spec §11.7).

    The path is the flag. Present means engaged. The monthly paper test engages it, checks that
    working orders cancel and a new submit is refused, then disengages with a written
    acknowledgement.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def is_engaged(self) -> bool:
        """True when the flag file exists."""
        return self.path.exists()

    def engage(self, reason: str, oms: _SwitchBook) -> None:
        """Write the flag, cancel working orders, and record an unresolved class-A incident.

        A second engage while the flag is already up cancels again and does not add another
        incident. ``reason`` is stored as the incident detail.
        """
        if not reason or not reason.strip():
            raise ValueError("engage requires a reason")
        fresh = not self.is_engaged()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + ".tmp")
        tmp.write_text(reason.strip() + "\n", encoding="utf-8")
        tmp.replace(self.path)
        oms.cancel_all()
        if fresh:
            oms.record_incident("A", "kill_switch", reason.strip())

    def disengage(self, ack: str, oms: _SwitchBook) -> None:
        """Clear the flag after ``ack`` is stored. An empty acknowledgement is refused (spec §11.7)."""
        if not ack or not ack.strip():
            raise ValueError("disengage requires a non-empty acknowledgement")
        if not self.is_engaged():
            raise RuntimeError("kill switch is not engaged")
        oms.resolve_incidents("kill_switch", note=ack)
        self.path.unlink()
