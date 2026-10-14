"""Errors raised by the mechanism loader, propagator, and counterfactual harness (spec §3.1, §3.4)."""

from __future__ import annotations


class MissingParameterError(RuntimeError):
    """A required parameter is still ``TODO_estimate``. The mechanism refuses to run (spec §3.1)."""

    def __init__(self, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(
            f"parameter {parameter!r} is TODO_estimate; the mechanism refuses to run and will not default it"
        )


class UnitContinuityError(ValueError):
    """Step k's output unit does not match step k+1 and no converter was declared (spec §3.4 rule 1)."""

    def __init__(self, source: str, dest: str, from_unit: str, to_unit: str) -> None:
        self.source = source
        self.dest = dest
        self.from_unit = from_unit
        self.to_unit = to_unit
        super().__init__(
            f"unit continuity failed: {source} outputs {from_unit}, {dest} consumes {to_unit}, "
            "and no converter is declared"
        )


class CompetingClosureError(ValueError):
    """A step's competing mechanism is neither a later step nor an accepted risk (spec §3.4 rule 3)."""

    def __init__(self, mechanism_id: str, missing: list[str]) -> None:
        self.mechanism_id = mechanism_id
        self.missing = missing
        super().__init__(f"{mechanism_id} omits competing effects: {', '.join(missing)}")


class IdentityViolationError(RuntimeError):
    """An accounting identity residual exceeds its tolerance after propagation (spec §3.4 rule 7)."""

    def __init__(self, mechanism_id: str, residual: float, tolerance: float) -> None:
        self.mechanism_id = mechanism_id
        self.residual = residual
        self.tolerance = tolerance
        super().__init__(
            f"{mechanism_id} residual {residual} exceeds tolerance {tolerance}; identities are checks, not forecasts"
        )
