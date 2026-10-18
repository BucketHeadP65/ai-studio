"""In-memory stand-ins for the recommendation loop's unit tests.

`FakeKnowledgeBaseGateway` implements the recommendation loop's knowledge base ports and answers canned results.
`FakeRecommendationProposer` implements `RecommendationProposer` and returns a scripted `DraftRecommendation`.
Both record every call.

Assign a `*_result` field to drive the next call, and read the `*_calls` lists to assert what the caller passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engram.evals.recommendation.contracts import KnowledgeBaseStamp

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from engram.evals.recommendation.contracts import (
        CandidateEdge,
        DraftRecommendation,
        MinedLink,
        OversizedSection,
        SectionStat,
        StructuralDiagnosis,
    )
    from engram.kb.contracts import KnowledgeBaseSchema


@dataclass(frozen=True)
class RecordedSectionsQuery:
    """One captured `sections_over_budget` call."""

    page_ids: tuple[str, ...]
    token_budget: int
    collection: str


@dataclass(frozen=True)
class RecordedReachabilityQuery:
    """One captured `reachable_pairs` call, including any injected candidate edges."""

    pairs: tuple[tuple[str, str], ...]
    max_hops: int
    collection: str
    extra_edges: tuple[CandidateEdge, ...]


@dataclass(frozen=True)
class RecordedPageTypesQuery:
    """One captured `page_types` call."""

    page_ids: tuple[str, ...]
    collection: str


@dataclass(frozen=True)
class RecordedChangedPathsQuery:
    """One captured `changed_paths` call."""

    since: datetime
    collection: str


@dataclass(frozen=True)
class RecordedSnippetRanksQuery:
    """One captured `snippet_ranks` call."""

    query: str
    snippets: tuple[str, ...]
    collection: str


@dataclass
class FakeKnowledgeBaseGateway:
    """Records the recommendation loop's knowledge base port calls and returns the scripted results."""

    sections_over_budget_result: Sequence[SectionStat] = ()
    page_types_result: Mapping[str, str] = field(default_factory=dict)
    reachable_pairs_result: Sequence[bool] = ()
    unlifted_links_result: Sequence[MinedLink] = ()
    stamp_result: KnowledgeBaseStamp = field(
        default_factory=lambda: KnowledgeBaseStamp(collection="minimal", spec_hash="", manifest_digest="", n_pages=0)
    )
    changed_paths_result: Sequence[str] = ()
    oversized_section_bodies_result: Sequence[OversizedSection] = ()
    snippet_ranks_result: Sequence[int] | None = None
    sections_over_budget_calls: list[RecordedSectionsQuery] = field(default_factory=list)
    page_types_calls: list[RecordedPageTypesQuery] = field(default_factory=list)
    reachable_pairs_calls: list[RecordedReachabilityQuery] = field(default_factory=list)
    unlifted_links_calls: list[str] = field(default_factory=list)
    stamp_calls: list[str] = field(default_factory=list)
    changed_paths_calls: list[RecordedChangedPathsQuery] = field(default_factory=list)
    oversized_section_bodies_calls: list[RecordedSectionsQuery] = field(default_factory=list)
    snippet_ranks_calls: list[RecordedSnippetRanksQuery] = field(default_factory=list)

    async def sections_over_budget(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[SectionStat]:
        """Record the call and return `sections_over_budget_result`.

        Args:
            page_ids: The pages to inspect.
            token_budget: The section size limit in tokens.
            collection: The collection to read.

        Returns:
            The scripted section stats.
        """
        self.sections_over_budget_calls.append(RecordedSectionsQuery(tuple(page_ids), token_budget, collection))
        return self.sections_over_budget_result

    async def page_types(self, page_ids: Sequence[str], *, collection: str) -> Mapping[str, str]:
        """Record the call and return `page_types_result`.

        Args:
            page_ids: The pages to type.
            collection: The collection to read.

        Returns:
            The scripted page type by page id.
        """
        self.page_types_calls.append(RecordedPageTypesQuery(tuple(page_ids), collection))
        return self.page_types_result

    async def reachable_pairs(
        self, pairs: Sequence[tuple[str, str]], *, max_hops: int, collection: str, extra_edges: Sequence[CandidateEdge] = ()
    ) -> Sequence[bool]:
        """Record the call and return `reachable_pairs_result`.

        Args:
            pairs: The page pairs to test.
            max_hops: The longest path allowed.
            collection: The collection to read.
            extra_edges: Candidate edges added to the graph for this call.

        Returns:
            The scripted reachability, one flag per pair.
        """
        self.reachable_pairs_calls.append(RecordedReachabilityQuery(tuple(pairs), max_hops, collection, tuple(extra_edges)))
        return self.reachable_pairs_result

    async def unlifted_links(self, *, collection: str) -> Sequence[MinedLink]:
        """Record the collection and return `unlifted_links_result`.

        Args:
            collection: The collection to read.

        Returns:
            The scripted links.
        """
        self.unlifted_links_calls.append(collection)
        return self.unlifted_links_result

    async def stamp(self, *, collection: str) -> KnowledgeBaseStamp:
        """Record the collection and return `stamp_result`.

        Args:
            collection: The collection to stamp.

        Returns:
            The scripted stamp.
        """
        self.stamp_calls.append(collection)
        return self.stamp_result

    async def changed_paths(self, since: datetime, *, collection: str) -> Sequence[str]:
        """Record the call and return `changed_paths_result`.

        Args:
            since: The time after which a change counts.
            collection: The collection to read.

        Returns:
            The scripted paths.
        """
        self.changed_paths_calls.append(RecordedChangedPathsQuery(since, collection))
        return self.changed_paths_result

    async def oversized_section_bodies(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[OversizedSection]:
        """Record the call and return `oversized_section_bodies_result`.

        Args:
            page_ids: The pages to inspect.
            token_budget: The section size limit in tokens.
            collection: The collection to read.

        Returns:
            The scripted oversized sections.
        """
        self.oversized_section_bodies_calls.append(RecordedSectionsQuery(tuple(page_ids), token_budget, collection))
        return self.oversized_section_bodies_result

    async def snippet_ranks(self, query: str, snippets: Sequence[str], *, collection: str) -> Sequence[int]:
        """Record the call and return `snippet_ranks_result`, or 1-based positions when it is unscripted.

        Args:
            query: The query to rank against.
            snippets: The snippets to rank.
            collection: The collection to read.

        Returns:
            One rank per snippet.
        """
        self.snippet_ranks_calls.append(RecordedSnippetRanksQuery(query, tuple(snippets), collection))
        if self.snippet_ranks_result is not None:
            return self.snippet_ranks_result
        return list(range(1, len(snippets) + 1))


@dataclass
class FakeRecommendationProposer:
    """Records the diagnosis and schema it received and returns a scripted `DraftRecommendation`.

    `received` holds every `(diagnosis, spec)` pair in call order.
    """

    draft: DraftRecommendation
    received: list[tuple[StructuralDiagnosis, KnowledgeBaseSchema]] = field(default_factory=list)

    async def propose(self, diagnosis: StructuralDiagnosis, spec: KnowledgeBaseSchema) -> DraftRecommendation:
        """Record the pair and return `draft`.

        Args:
            diagnosis: The structural diagnosis.
            spec: The knowledge base schema.

        Returns:
            The scripted draft.
        """
        self.received.append((diagnosis, spec))
        return self.draft
