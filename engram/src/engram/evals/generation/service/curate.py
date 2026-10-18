"""Curation of a raw pool of generated cases into a balanced dataset with no near duplicates.

`CaseCurator.dedup` drops each case whose question is too close to one already kept, in input order.
`CaseCurator.stratify_cells` takes up to each `(archetype, difficulty)` cell's quota, in input order.
A case with no archetype, or one outside the quotas, is left out, and the result never exceeds the total.
The curator does no IO beyond one batched embedding call.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from engram.evals.generation.domain.allocation import QuotaAllocation
    from engram.evals.generation.domain.datasets import AuthoredCase
    from engram.evals.generation.internal_contracts import SupportsEmbedBatch

# A vector whose norm is below this counts as zero, so it is similar to nothing and never a duplicate.
_ZERO_NORM_EPSILON = 1e-12


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors, `0.0` when either norm is below `_ZERO_NORM_EPSILON`."""
    norm_left = math.sqrt(sum(component * component for component in left))
    norm_right = math.sqrt(sum(component * component for component in right))
    if norm_left < _ZERO_NORM_EPSILON or norm_right < _ZERO_NORM_EPSILON:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (norm_left * norm_right)


class CaseCurator:
    """Trims a raw case pool by dropping near duplicates and sampling to quotas, with no randomness."""

    def __init__(self, embedder: SupportsEmbedBatch, *, dedup_threshold: float) -> None:
        """Bind the embedder and the near-duplicate cosine cutoff.

        Args:
            embedder: The embedding port whose `embed_batch` produces one vector per question.
            dedup_threshold: The cosine similarity at or above which two questions are near duplicates.
        """
        self._embedder = embedder
        self._dedup_threshold = dedup_threshold

    async def dedup(self, cases: list[AuthoredCase]) -> list[AuthoredCase]:
        """Drop near-duplicate cases by the cosine similarity of their question embeddings.

        A case is kept when its similarity to every case already kept is below `dedup_threshold`, so
        the first of a group of near duplicates survives. Every question is embedded in one batched call.

        Args:
            cases: The raw pool of authored cases, in the order they should be considered.

        Returns:
            The kept cases (no two within `dedup_threshold` cosine of each other), in input order.
        """
        if not cases:
            return []
        vectors = (await self._embedder.embed_batch([case.question.question for case in cases])).vectors
        kept: list[AuthoredCase] = []
        kept_vectors: list[list[float]] = []
        for case, vector in zip(cases, vectors, strict=True):
            if all(_cosine(vector, seen) < self._dedup_threshold for seen in kept_vectors):
                kept.append(case)
                kept_vectors.append(vector)
        return kept

    def stratify_cells(self, cases: list[AuthoredCase], allocation: QuotaAllocation) -> list[AuthoredCase]:
        """Sample cases to a coverage allocation, capping each `(archetype, difficulty)` cell at its quota.

        Cells that differ only in anchor page type pool their quotas, and a short cell never borrows
        from another. A case with no archetype, or whose stratum the allocation lacks, is left out, and
        the result never exceeds the allocation's total.

        Args:
            cases: The pool to sample from, in the order to draw within each cell bucket.
            allocation: The per-cell case quotas from `allocate`.

        Returns:
            The sampled cases, at most the allocation's grand total, in cell-then-input order.
        """
        quota_by_key: dict[str, int] = {}
        for cell in allocation.cells:
            key = f"{cell.stratum.archetype.value}/{cell.stratum.difficulty}"
            quota_by_key[key] = quota_by_key.get(key, 0) + cell.quota
        return self._sample(cases, quota_by_key, key_of=_cell_key, total=sum(quota_by_key.values()))


    def _sample(
        self, cases: list[AuthoredCase], quota_by_key: Mapping[str, int], *, key_of: Callable[[AuthoredCase], str | None], total: int
    ) -> list[AuthoredCase]:
        """Take up to each key's quota of `cases` in input order, key by key, then cap the result at `total`."""
        sampled: list[AuthoredCase] = []
        for key, quota in quota_by_key.items():
            bucket = [case for case in cases if key_of(case) == key]
            sampled.extend(bucket[:quota])
        return sampled[:total]


def _cell_key(case: AuthoredCase) -> str | None:
    """The case's `archetype/difficulty` stratum key, or `None` when it has no archetype (excluded)."""
    if case.meta.archetype is None:
        return None
    return f"{case.meta.archetype.value}/{case.meta.difficulty}"
