"""Circular block index draws shared by the factor and correlation bootstraps."""

from __future__ import annotations

import numpy as np


def circular_block_indices(n: int, block_len: int, rng: np.random.Generator) -> np.ndarray:
    """Indices of one circular block-bootstrap sample (Politis & Romano 1992).

    Blocks wrap around the end of the sample and are concatenated until length ``n``.
    """
    if block_len < 1:
        raise ValueError("block_len must be positive")
    if block_len > n:
        raise ValueError("block_len cannot exceed the sample length")
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, n, size=n_blocks)
    offsets = np.arange(block_len)
    idx = ((starts[:, None] + offsets[None, :]) % n).ravel()[:n]
    return idx
