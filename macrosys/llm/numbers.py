"""Number tokens shared by extraction and narrative checks (spec §8.3 roles 1 and 4).

Role 1 accepts a size only when the same number is in a cited quote. Role 4 accepts a narrative
number only when the same number is already in the thesis, the run, or a monitoring delta.
Both sides use this canonical form so ``40``, ``40.0``, and ``1,000`` compare as the same token.
"""

from __future__ import annotations

import math
import re

from macrosys.core.quantity import Quantity

# Comma groups, decimals, and scientific notation. The lookarounds keep ``12`` from matching inside ``12.5``.
_NUMBER = re.compile(r"(?<![\w.])(-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?)(?![\w])")


def canon_float(value: float) -> str:
    """Canonical token for a float. Integral values drop the decimal; other values use 12 significant digits."""
    if not math.isfinite(value):
        raise ValueError(f"cannot canonicalize non-finite number {value}")
    if abs(value - round(value)) < 1e-9 and abs(value) < 1e15:
        return str(int(round(value)))
    return format(value, ".12g")


def canon_number(raw: str) -> str:
    """Canonical token for a number as it appeared in text, after removing thousands separators."""
    return canon_float(float(raw.replace(",", "")))


def number_tokens(text: str) -> set[str]:
    """Every number token in ``text``, in canonical form (spec §8.3 roles 1 and 4)."""
    return {canon_number(match.group(1)) for match in _NUMBER.finditer(text)}


def quantity_tokens(quantity: Quantity) -> set[str]:
    """Tokens for the point and the range endpoints that are set on ``quantity``."""
    tokens: set[str] = set()
    for value in (quantity.value, quantity.low, quantity.high):
        if value is not None:
            tokens.add(canon_float(value))
    return tokens
