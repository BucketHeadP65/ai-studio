"""The search setups, hits as ranked source documents, and one case's scores against its relevance judgments.

Every metric is per document. A raw chunk's document is its id up to the last `:`, and a page's document is the
source its page record names. A document keeps its first rank. A gold or judged id that names a page is read as that
page's source document too.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.outcomes.contracts import ndcg_at_k, ranked_metrics

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

SCORE_KS: tuple[int, ...] = (1, 5, 10)
"""The recall cutoffs a score reports. Recall past 10 is left out, since 50 chunk hits can be fewer documents."""

NDCG_K = 10
"""The nDCG cutoff."""


class SearchSetup(BaseModel):
    """The two search knobs a search setup sets: hybrid or keyword-only ranking, and one hit level or every level."""

    model_config = ConfigDict(frozen=True)

    hybrid: bool
    """True ranks by keywords and embeddings together, False by keywords alone."""
    level: str | None
    """The one hit level searched, or None for every level."""


SEARCH_SETUPS: Mapping[str, SearchSetup] = MappingProxyType(
    {
        "hybrid": SearchSetup(hybrid=True, level=None),
        "keyword": SearchSetup(hybrid=False, level=None),
        "hybrid-raw": SearchSetup(hybrid=True, level="raw"),
        "keyword-raw": SearchSetup(hybrid=False, level="raw"),
        "hybrid-card": SearchSetup(hybrid=True, level="card"),
        "hybrid-section": SearchSetup(hybrid=True, level="section"),
        "hybrid-claim": SearchSetup(hybrid=True, level="claim"),
    }
)
"""Every search setup by name, in a fixed order."""

DEFAULT_SEARCH_SETUP = "hybrid"
"""The search setup a dataset is scored under when none is named. Its report keeps the file name that carries no setup."""


def search_setup(name: str) -> SearchSetup:
    """The search setup named `name`.

    Args:
        name: A key of `SEARCH_SETUPS`.

    Returns:
        Its two search knobs.

    Raises:
        ValueError: When `name` names no search setup. The message names every search setup.
    """
    if name not in SEARCH_SETUPS:
        msg = f"unknown search setup {name!r}, choose one of {', '.join(SEARCH_SETUPS)}"
        raise ValueError(msg)
    return SEARCH_SETUPS[name]


class HitRef(BaseModel):
    """The three columns of one search hit the scorer reads."""

    model_config = ConfigDict(frozen=True)

    id: str
    hit_level: str
    page_id: str | None


class CaseScore(BaseModel):
    """One case's scores: nDCG@10 from its grades, recall at each cutoff and reciprocal rank from its gold ids."""

    model_config = ConfigDict(frozen=True)

    name: str = ""
    ndcg_at_10: float
    recall: dict[int, float]
    reciprocal_rank: float
    ranked: tuple[str, ...] = ()
    """The first `NDCG_K` source documents the search ranked, the ones nDCG@10 reads, not every document it returned."""
    unmapped_hits: int = 0
    hits: int = 0
    """How many hits the case's search returned, 0 when it returned none at all."""


def page_ids_of(hits: Sequence[HitRef]) -> list[str]:
    """The distinct page ids among `hits`, in rank order, for the one source lookup a case needs.

    Args:
        hits: The case's search hits, in rank order.

    Returns:
        Each page id once.
    """
    return list(dict.fromkeys(hit.page_id or hit.id for hit in hits if hit.hit_level != "raw"))


def ranked_sources(hits: Sequence[HitRef], page_sources: Mapping[str, str]) -> tuple[list[str], int]:
    """The source documents `hits` came from, each once at its first rank, and how many hits named no source.

    Args:
        hits: The search hits, in rank order.
        page_sources: The source document of each page among the hits that names one.

    Returns:
        The ranked documents and the count of hits that mapped to none.
    """
    ranked: dict[str, None] = {}
    unmapped = 0
    for hit in hits:
        source = hit.id.rsplit(":", 1)[0] if hit.hit_level == "raw" else page_sources.get(hit.page_id or hit.id)
        if source is None:
            unmapped += 1
            continue
        ranked.setdefault(source, None)
    return list(ranked), unmapped


class SourceGold(BaseModel):
    """One case's gold ids and relevance judgments as source documents, and how many gold ids could not be mapped."""

    model_config = ConfigDict(frozen=True)

    ids: tuple[str, ...]
    """The gold source documents, each once, in gold order."""
    judgments: dict[str, int]
    """The relevance judgment of each judged source document."""
    unmappable: int
    """How many gold ids named a page with no source document."""


def source_gold(
    gold: Sequence[str], judgments: Mapping[str, int], *, pages: Collection[str], page_sources: Mapping[str, str]
) -> SourceGold:
    """`gold` and `judgments` with every page id replaced by the source document its page names.

    An id that names no page is kept as it is. A page with no source is dropped, and each such gold id is counted.
    Ids that land on one document become one, which keeps the higher grade.

    Args:
        gold: The case's gold ids.
        judgments: The relevance judgment of each judged id.
        pages: The ids among them that name a page.
        page_sources: The source document of each page that names one.

    Returns:
        The case's gold ids and judgments as source documents, and the count of gold ids left unmapped.
    """
    documents: dict[str, None] = {}
    unmappable = 0
    for identifier in dict.fromkeys(gold):
        document = _document_of(identifier, pages, page_sources)
        if document is None:
            unmappable += 1
        else:
            documents.setdefault(document, None)
    grades: dict[str, int] = {}
    for identifier, grade in judgments.items():
        document = _document_of(identifier, pages, page_sources)
        if document is not None:
            grades[document] = max(grade, grades.get(document, grade))
    return SourceGold(ids=tuple(documents), judgments=grades, unmappable=unmappable)


def _document_of(identifier: str, pages: Collection[str], page_sources: Mapping[str, str]) -> str | None:
    """The source document `identifier` stands for, None for a page with no source."""
    if identifier in page_sources:
        return page_sources[identifier]
    return None if identifier in pages else identifier


def score_case(*, ranked: Sequence[str], gold: Sequence[str], judgments: Mapping[str, int]) -> CaseScore:
    """One case's scores against its gold ids and relevance judgments.

    A case with no judgments, a generated one, scores every gold id as grade 1.

    Args:
        ranked: The ranked source documents.
        gold: The case's gold ids.
        judgments: The relevance judgment of each judged document, empty for a generated case.

    Returns:
        The case's nDCG@10, recall at each cutoff in `SCORE_KS` and reciprocal rank.
    """
    grades = dict(judgments) if judgments else dict.fromkeys(gold, 1)
    metrics = ranked_metrics(ranked, gold, ks=SCORE_KS)
    return CaseScore(
        ndcg_at_10=ndcg_at_k(ranked, grades, NDCG_K),
        recall=metrics.recall,
        reciprocal_rank=metrics.reciprocal_rank,
        ranked=tuple(ranked[:NDCG_K]),
    )


class DatasetScore(BaseModel):
    """A dataset's scores under one search setup: one per scored case, the means over them, and what named no source document."""

    model_config = ConfigDict(frozen=True)

    setup: str
    """The name of the search setup every case's search ran under."""
    cases: tuple[CaseScore, ...]
    means: dict[str, float]
    unmapped_hits: int
    unmappable_gold: int
    """How many gold ids named a page with no source document."""
    cases_without_gold: int
    """How many cases were left out of the scores and the means because no gold id named a source document."""
    cases_without_hits: int
    """How many scored cases' searches returned no hits at all."""


def dataset_score(cases: Sequence[CaseScore], *, setup: str, unmappable_gold: int, cases_without_gold: int) -> DatasetScore:
    """The means of every metric over `cases`, all zero for an empty dataset, and how many cases' searches returned no hits.

    Args:
        cases: The scored cases.
        setup: The name of the search setup the cases were searched under.
        unmappable_gold: How many gold ids named a page with no source document.
        cases_without_gold: How many cases were left out because no gold id named a source document.

    Returns:
        The dataset's score.
    """
    count = max(1, len(cases))
    means = {f"ndcg@{NDCG_K}": sum(case.ndcg_at_10 for case in cases) / count}
    for k in SCORE_KS:
        means[f"recall@{k}"] = sum(case.recall[k] for case in cases) / count
    means["mrr"] = sum(case.reciprocal_rank for case in cases) / count
    return DatasetScore(
        setup=setup,
        cases=tuple(cases),
        means=means,
        unmapped_hits=sum(case.unmapped_hits for case in cases),
        unmappable_gold=unmappable_gold,
        cases_without_gold=cases_without_gold,
        cases_without_hits=sum(case.hits == 0 for case in cases),
    )


def score_markdown(score: DatasetScore, *, dataset: str, collection: str) -> str:
    """The markdown summary of a dataset's score: its setup, the means, what named no source, and the cases that found nothing.

    It counts the cases whose search returned no hits only when there are any, and names the cases that ranked no gold
    document.

    Args:
        score: The dataset's score.
        dataset: The dataset file's name.
        collection: The knowledge base searched.

    Returns:
        The summary.
    """
    lines = [
        f"# Search score: {dataset} on {collection}",
        "",
        f"{len(score.cases)} case(s), scored per document with search alone at k = 50 under the {score.setup} search setup.",
        "",
        "| Metric | Mean |",
        "|---|---|",
        *(f"| {name} | {value:.4f} |" for name, value in score.means.items()),
        "",
        f"Hits that named no source document: {score.unmapped_hits}.",
        f"Gold ids that name a page with no source document: {score.unmappable_gold}.",
        f"Cases left out because no gold id names a source document: {score.cases_without_gold}.",
        "Recall past 10 is not reported, because 50 chunk hits can collapse to fewer than 50 documents.",
    ]
    if score.cases_without_hits:
        lines.append(f"Cases whose search returned no hits at all: {score.cases_without_hits} of {len(score.cases)}.")
    missed = [case.name for case in score.cases if case.reciprocal_rank == 0.0]
    if missed:
        lines += ["", f"Cases that ranked no gold document ({len(missed)}): {', '.join(missed[:50])}"]
    return "\n".join(lines) + "\n"
