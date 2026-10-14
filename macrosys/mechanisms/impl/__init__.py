"""Importing this package registers every mechanism implementation (spec §3.3)."""

from macrosys.mechanisms.impl import empirical, identities, pricing

__all__ = ["empirical", "identities", "pricing"]
