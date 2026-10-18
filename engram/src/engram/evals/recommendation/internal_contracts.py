"""The ports the recommendation depends on: the proposer that drafts it and the knowledge base reads behind its evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from engram.evals.recommendation.domain.detectors import SectionStat, StructuralDiagnosis
    from engram.evals.recommendation.domain.recommendation import (
        CandidateEdge,
        DraftRecommendation,
        KnowledgeBaseStamp,
        MinedLink,
        OversizedSection,
    )
    from engram.kb.contracts import KnowledgeBaseSchema


class RecommendationProposer(Protocol):
    """Drafts a recommendation's schema edits and findings from a structural diagnosis.

    The draft carries no measured evidence. The gate composes the evidence and checks every edit.
    """

    async def propose(self, diagnosis: StructuralDiagnosis, spec: KnowledgeBaseSchema) -> DraftRecommendation:
        """Draft a recommendation from `diagnosis` against the knowledge base's current schema.

        Args:
            diagnosis: The structural diagnosis to draft against.
            spec: The knowledge base's current schema, which the edits apply to.

        Returns:
            The draft recommendation, which `gate_recommendation` must still check.
        """
        ...


class SectionStatQueries(Protocol):
    """Reads the sections over the token budget, the evidence behind a `restructure_sections` proposal."""

    async def sections_over_budget(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[SectionStat]:
        """Return the canonical sections on `page_ids` whose token count exceeds `token_budget`.

        Args:
            page_ids: The pages to read. An empty sequence returns nothing.
            token_budget: The token count a section must exceed.
            collection: The collection the pages must belong to.

        Returns:
            One `SectionStat` per section over the budget, ordered by page id then canonical slug.
        """
        ...


class PageTypeQueries(Protocol):
    """Reads the page type of each page, which the section-split detector groups its sections by."""

    async def page_types(self, page_ids: Sequence[str], *, collection: str) -> Mapping[str, str]:
        """Return the page type of each page in `page_ids` that belongs to `collection`.

        A page outside `collection` is left out of the result.

        Args:
            page_ids: The pages to read. An empty sequence returns an empty mapping.
            collection: The collection the pages must belong to.

        Returns:
            The page type of each page found, keyed by page id.
        """
        ...


class GraphQueries(Protocol):
    """Reads whether gold pages reach each other in the graph, with or without added candidate edges."""

    async def reachable_pairs(
        self, pairs: Sequence[tuple[str, str]], *, max_hops: int, collection: str, extra_edges: Sequence[CandidateEdge] = ()
    ) -> Sequence[bool]:
        """Return, in input order, whether each pair's destination is reachable from its source.

        The walk follows the collection's edges plus `extra_edges`, up to `max_hops` hops.

        Args:
            pairs: The `(source, destination)` page id pairs to test. An empty sequence returns nothing.
            max_hops: The most hops a path may take.
            collection: The collection whose edges the walk follows.
            extra_edges: The candidate edges to add for this read only. Empty reads the graph as it is.

        Returns:
            One flag per pair, true when the destination is reachable.
        """
        ...


class LinkMineQueries(Protocol):
    """Reads the prose links no typed edge carries yet, the evidence behind an `add_relation` proposal."""

    async def unlifted_links(self, *, collection: str) -> Sequence[MinedLink]:
        """Return the collection's prose links that no typed edge carries.

        A link whose target does not resolve is always returned, with no destination page.

        Args:
            collection: The collection whose links to read.

        Returns:
            One `MinedLink` per link no typed edge carries.
        """
        ...


class KnowledgeBaseStampQueries(Protocol):
    """Reads the identity of an indexed knowledge base, which the baseline and the verify run record."""

    async def stamp(self, *, collection: str) -> KnowledgeBaseStamp:
        """Return the identity of the knowledge base indexed under `collection`.

        Two identical indexed knowledge bases return equal stamps.

        Args:
            collection: The collection to read.

        Returns:
            The collection's schema fingerprint, manifest digest and page count.
        """
        ...


class ChangedPathQueries(Protocol):
    """Reads the files indexed since a moment, which the verify run attributes knowledge base drift with."""

    async def changed_paths(self, since: datetime, *, collection: str) -> Sequence[str]:
        """Return the manifest paths indexed after `since`, ordered by path.

        Args:
            since: The moment to read from, itself not included.
            collection: The collection whose manifest to read.

        Returns:
            The paths indexed after `since`.
        """
        ...


class SectionBodyQueries(Protocol):
    """Reads the sections over the token budget with their bodies, the input of the split simulation."""

    async def oversized_section_bodies(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[OversizedSection]:
        """Return the canonical sections on `page_ids` over `token_budget`, with their bodies.

        Args:
            page_ids: The pages to read. An empty sequence returns nothing.
            token_budget: The token count a section must exceed.
            collection: The collection the pages must belong to.

        Returns:
            One `OversizedSection` per section over the budget, ordered by page id then canonical slug.
        """
        ...


class SnippetRankQueries(Protocol):
    """Reads where a text would rank among the collection's sections for a query."""

    async def snippet_ranks(self, query: str, snippets: Sequence[str], *, collection: str) -> Sequence[int]:
        """Return each snippet's rank among the collection's sections for `query`, in input order.

        Args:
            query: The query to rank against.
            snippets: The texts to rank. An empty sequence returns nothing.
            collection: The collection whose sections the snippets are ranked among.

        Returns:
            One rank per snippet, starting at 1 for the best, in the order of `snippets`.
        """
        ...


class SimulationGateway(SectionBodyQueries, SnippetRankQueries, Protocol):
    """The two knowledge base reads the split simulation needs, as one port."""
