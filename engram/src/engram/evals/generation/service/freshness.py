"""The freshness check: score a dataset version's cases against the live knowledge base."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.generation.domain.freshness import case_freshness_refs, evaluate_freshness
from engram.evals.generation.domain.mirror import mirror_hash

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.evals.commands import FreshnessReader
    from engram.evals.generation.domain.datasets import EvaluationCase
    from engram.evals.generation.domain.freshness import CaseFreshness, CaseFreshnessRef


async def freshness_verdicts(reader: FreshnessReader, cases: Sequence[EvaluationCase], *, collection: str) -> tuple[CaseFreshness, ...]:
    """One freshness verdict per named case of a dataset version, scored against the live knowledge base.

    Args:
        reader: Reads the live page state and the drafting text sources.
        cases: The cases of the dataset version.
        collection: The knowledge base the cases were drafted from.

    Returns:
        The verdicts, one for each named case.
    """
    refs = case_freshness_refs(cases)
    mirror_refs = [ref for ref in refs if ref.mirror_hash is not None]
    current_hashes = await reader.page_state(collection=collection, page_ids=_referenced_page_ids(refs))
    sources = await reader.mirror_sources(refs=mirror_refs)
    recomputed = {ref.case_name: (mirror_hash(sources[ref.case_name]) if ref.case_name in sources else None) for ref in mirror_refs}
    return evaluate_freshness(refs, current_ids=frozenset(current_hashes), current_hashes=current_hashes, recomputed=recomputed)


def _referenced_page_ids(refs: Sequence[CaseFreshnessRef]) -> frozenset[str]:
    """The union of every case's anchored and gold page ids, the one `page_state` read the sweep needs."""
    ids: set[str] = set()
    for ref in refs:
        ids.update(ref.gold_ids)
        ids.update(anchor.unit_id for anchor in ref.anchors)
    return frozenset(ids)
