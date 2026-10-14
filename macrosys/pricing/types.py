"""Shared result types for market-implied paths (spec §8.5)."""

from __future__ import annotations

from macrosys.core.quantity import Quantity

# Horizon is in quarters. The economic value is always a Quantity (spec §4.7, §8.5).
PathWithRange = list[tuple[float, Quantity]]
Range = Quantity
