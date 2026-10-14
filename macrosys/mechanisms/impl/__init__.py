"""Importing this package registers every mechanism implementation (spec §3.3)."""

from macrosys.mechanisms.impl import behavioral, empirical, identities, pricing

__all__ = ["behavioral", "empirical", "identities", "pricing"]
