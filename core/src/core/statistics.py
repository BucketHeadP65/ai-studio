"""A seeded percentile bootstrap over paired deltas, written with the standard library only."""

from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


# The byte width of the sha256 slice each resample draw folds into a case index. Eight bytes span the
# whole 64-bit space, so the modulo bias against any realistic sample size is negligible.
_RESAMPLE_INDEX_BYTES = 8


def _percentile(sorted_values: list[float], quantile: float) -> float:
    """A quantile of an already sorted, nonempty sample, by linear interpolation between neighbours."""
    if not sorted_values:
        msg = "cannot take a percentile of an empty sample"
        raise ValueError(msg)

    position = (len(sorted_values) - 1) * quantile
    lower_index = int(position)
    if lower_index + 1 >= len(sorted_values):
        return sorted_values[lower_index]

    fraction = position - lower_index
    return sorted_values[lower_index] + fraction * (sorted_values[lower_index + 1] - sorted_values[lower_index])


def paired_bootstrap_lower_bound(deltas: Sequence[float], *, n_resamples: int, confidence: float, seed: int) -> float:
    """The lower confidence bound of the mean of paired deltas, by a seeded percentile bootstrap.

    The same deltas, resample count, confidence and seed always give the same bound. No clock and no
    global random state is read. A constant delta vector returns exactly that constant.

    Args:
        deltas: The per-case deltas (candidate minus incumbent), at least one.
        n_resamples: How many bootstrap resamples to draw.
        confidence: The two-sided confidence level, a fraction strictly between 0 and 1.
        seed: The seed every resample draw is derived from.

    Returns:
        The `(1 - confidence) / 2` quantile of the resampled means.

    Raises:
        ValueError: `deltas` is empty.
    """
    values = list(deltas)
    if not values:
        msg = "paired_bootstrap_lower_bound requires at least one delta"
        raise ValueError(msg)

    size = len(values)
    means: list[float] = []
    for draw in range(n_resamples):
        total = 0.0
        for position in range(size):
            digest = hashlib.sha256(f"{seed}:{draw}:{position}".encode()).digest()
            index = int.from_bytes(digest[:_RESAMPLE_INDEX_BYTES], "big") % size
            total += values[index]
        means.append(total / size)

    means.sort()
    return _percentile(means, (1.0 - confidence) / 2.0)
