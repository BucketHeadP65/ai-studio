"""Score a dataset file with search alone, a bounded number of cases at a time."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from engram.evals.generation.contracts import load_dataset
from engram.evals.scoring.domain import (
    DEFAULT_SEARCH_SETUP,
    CaseScore,
    DatasetScore,
    HitRef,
    SearchSetup,
    SourceGold,
    dataset_score,
    page_ids_of,
    ranked_sources,
    score_case,
    search_setup,
    source_gold,
)
from engram.evals.shared.contracts import Search

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from engram.evals.generation.contracts import EvaluationCase
    from engram.evals.scoring.internal_contracts import HitSources, Searcher

SEARCH_K = 50
"""How many hits each case asks for, the most `api.search` returns."""

_CONCURRENCY = 4


async def score_dataset(
    dataset: Path, *, collection: str, search: Searcher, sources: HitSources, setup_name: str = DEFAULT_SEARCH_SETUP
) -> DatasetScore:
    """Run one search per case of `dataset` under one search setup and score each against its gold ids and judgments.

    A gold or judged id that names a page is scored as the source document its page names. A case left with no gold
    id runs no search and is only counted. An unknown setup name is refused before the dataset is read.

    Args:
        dataset: The dataset file.
        collection: The knowledge base searched.
        search: The search every case runs through.
        sources: The page-to-source lookup.
        setup_name: The name of the search setup every search runs under.

    Returns:
        Every scored case's score, in dataset order, the means, the setup name, and the counts of gold that named no source.

    Raises:
        ValueError: When `setup_name` names no search setup. The message names every search setup.
    """
    setup = search_setup(setup_name)
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def scored(case: EvaluationCase, gold: SourceGold) -> CaseScore:
        async with gate:
            return await _score_one(case, gold, collection=collection, setup=setup, search=search, sources=sources)

    cases = load_dataset(dataset).cases
    golds = await _source_golds(cases, collection=collection, sources=sources)
    kept = [(case, gold) for case, gold in zip(cases, golds, strict=True) if gold.ids]
    scores = await asyncio.gather(*(scored(case, gold) for case, gold in kept))
    return dataset_score(
        scores, setup=setup_name, unmappable_gold=sum(gold.unmappable for gold in golds), cases_without_gold=len(cases) - len(kept)
    )


async def _source_golds(cases: Sequence[EvaluationCase], *, collection: str, sources: HitSources) -> list[SourceGold]:
    """Every case's gold ids and judgments as source documents, looked up once for the whole dataset."""
    judged = [(_gold_of(case), case.metadata.judgments if case.metadata is not None else {}) for case in cases]
    ids = list(dict.fromkeys(identifier for gold, judgments in judged for identifier in (*gold, *judgments)))
    pages: Collection[str] = await sources.page_ids(collection, ids) if ids else frozenset()
    page_sources = await sources.source_ids(collection, sorted(pages)) if pages else {}
    return [source_gold(gold, judgments, pages=pages, page_sources=page_sources) for gold, judgments in judged]


def _gold_of(case: EvaluationCase) -> tuple[str, ...]:
    return tuple(case.expected_output.cited_source_ids) if case.expected_output is not None else ()


async def _score_one(  # noqa: PLR0913 (the case, its gold, the collection, the search setup, the search and the source lookup are distinct inputs)
    case: EvaluationCase, gold: SourceGold, *, collection: str, setup: SearchSetup, search: Searcher, sources: HitSources
) -> CaseScore:
    """Search one case's question under the search setup, map its hits to documents and score them against its source gold."""
    query = Search(query=case.inputs.question, k=SEARCH_K, collection=collection, hybrid=setup.hybrid, level=setup.level)
    result = await search(query)
    hits = [HitRef.model_validate(row) for row in result.rows]
    page_ids = page_ids_of(hits)
    page_sources = await sources.source_ids(collection, page_ids) if page_ids else {}
    ranked, unmapped = ranked_sources(hits, page_sources)
    score = score_case(ranked=ranked, gold=gold.ids, judgments=gold.judgments)
    return score.model_copy(update={"name": case.name or "", "unmapped_hits": unmapped, "hits": len(result.rows)})
