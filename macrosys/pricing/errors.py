"""Errors for market-implied paths (spec §7.3, §9.8)."""

from __future__ import annotations


class CurveError(ValueError):
    """A curve cannot be built or evaluated (missing short end, extrapolation, bad discount factor)."""


class NoPricedInError(LookupError):
    """No market-implied quantity for this entity and variable (spec §7.3, §9.8).

    The message names what is missing so a thesis can record it as blocking missing evidence.
    """
