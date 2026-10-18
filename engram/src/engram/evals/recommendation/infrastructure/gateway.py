"""The recommendation's knowledge base reads, as SQL over the owner session.

`SqlKnowledgeBaseGateway` implements every read port the recommendation depends on. It needs the owner
`AsyncSession`, because a `kb_reader` session has no grant on the `kb` tables.
`reachable_pairs` walks `kb.edges` plus any candidate edges with a recursive CTE, whatever graph backend serves search.
`snippet_ranks` scores with `ts_rank_cd`, a length-normalised lexical score that stands in for BM25 search.
"""

from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.orm import aliased

from engram.evals.recommendation.domain.detectors import SectionStat
from engram.evals.recommendation.domain.recommendation import KnowledgeBaseStamp, MinedLink, OversizedSection
from engram.indexing.contracts import EdgeORM, IndexFingerprintORM, IndexManifestORM, LinkRefORM, PageORM, SectionORM, token_count

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from engram.evals.recommendation.domain.recommendation import CandidateEdge

# The reachability walk over kb.edges plus the candidate edges, which bind as arrays and are never
# interpolated. It follows edges and counts hops the way api.graph_paths does. `path` holds the pages a
# walk has visited, so it never visits one twice, and the depth is clamped into [1, 8]. It returns every
# distinct (seed, reached page) pair at one hop or more.
_REACHABILITY_SQL = """
WITH RECURSIVE all_edges(src_id, dst_id) AS (
    SELECT e.src_id, e.dst_id
    FROM kb.edges e
    JOIN kb.pages sp ON sp.id = e.src_id
    JOIN kb.pages dp ON dp.id = e.dst_id
    WHERE sp.collection = :collection AND dp.collection = :collection
    UNION ALL
    SELECT ce.src_id, ce.dst_id
    FROM unnest(
             coalesce(CAST(:edge_srcs AS text[]), '{}'::text[]),
             coalesce(CAST(:edge_dsts AS text[]), '{}'::text[])
         ) AS ce(src_id, dst_id)
),
walk(seed, node, hop, path) AS (
    SELECT s.seed, s.seed, 0, ARRAY[s.seed]
    FROM unnest(CAST(:seeds AS text[])) AS s(seed)
    UNION ALL
    SELECT w.seed, e.dst_id, w.hop + 1, w.path || e.dst_id
    FROM walk w
    JOIN all_edges e ON e.src_id = w.node
    WHERE w.hop < least(greatest(:max_hops, 1), 8)
      AND NOT e.dst_id = ANY(w.path)
)
SELECT DISTINCT seed, node FROM walk WHERE hop > 0
"""


# The snippets bind as a text array and are never interpolated. Every snippet and every stored section
# body in the collection is scored with ts_rank_cd flag 1, which divides by 1 + log(document length).
# A snippet's rank is one plus the number of stored sections scoring strictly higher. The LEFT JOIN
# keeps every snippet when the collection is empty, and each one then ranks 1.
_SNIPPET_RANK_SQL = """
WITH tsq AS (
    SELECT plainto_tsquery('english', :query) AS q
),
cand AS (
    SELECT s.ord AS ord, ts_rank_cd(to_tsvector('english', s.snippet), tsq.q, 1) AS score
    FROM unnest(CAST(:snippets AS text[])) WITH ORDINALITY AS s(snippet, ord), tsq
),
knowledge_base AS (
    SELECT ts_rank_cd(to_tsvector('english', sec.body), tsq.q, 1) AS score
    FROM kb.sections sec
    JOIN kb.pages p ON p.id = sec.page_id, tsq
    WHERE p.collection = :collection
)
SELECT cand.ord AS ord,
       1 + count(*) FILTER (WHERE knowledge_base.score > cand.score) AS rank
FROM cand
LEFT JOIN knowledge_base ON true
GROUP BY cand.ord
ORDER BY cand.ord
"""


class SqlKnowledgeBaseGateway:
    """The recommendation's knowledge base reads over the owner `AsyncSession`.

    It satisfies every recommendation read port by shape, the simulation's two included. It only reads.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Hold the session every read runs on.

        Args:
            session: The owner `AsyncSession`. A `kb_reader` session has no grant on the `kb` tables.
        """
        self._session = session

    async def sections_over_budget(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[SectionStat]:
        """Read the sections, counting a section's tokens from its body with `token_count` when the stored count is NULL.

        Args:
            page_ids: The pages to inspect, an empty sequence returns nothing.
            token_budget: The retrieval-unit token budget a section must exceed to be an offender.
            collection: The collection the owning page must belong to.

        Returns:
            One `SectionStat` per over-budget canonical section, ordered by page id then canonical slug.
        """
        sections = await self._sections_with_counts(page_ids, collection=collection)
        return [
            SectionStat(page_id=section.page_id, canon=section.canon, tokens=section.tokens)
            for section in sections
            if section.tokens > token_budget
        ]

    async def oversized_section_bodies(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[OversizedSection]:
        """Read the same sections as `sections_over_budget`, with the same token count, and keep each body.

        Args:
            page_ids: The pages to inspect, an empty sequence returns nothing.
            token_budget: The retrieval-unit token budget a section must exceed to be an offender.
            collection: The collection the owning page must belong to.

        Returns:
            One `OversizedSection` per over-budget canonical section, ordered by page id then slug.
        """
        sections = await self._sections_with_counts(page_ids, collection=collection)
        return [section for section in sections if section.tokens > token_budget]

    async def _sections_with_counts(self, page_ids: Sequence[str], *, collection: str) -> list[OversizedSection]:
        """Fetch the canonical sections on `page_ids` with a token count resolved for every row."""
        if not page_ids:
            return []
        statement = (
            select(SectionORM.page_id, SectionORM.canon, SectionORM.tokens, SectionORM.body)
            .join(PageORM, PageORM.id == SectionORM.page_id)
            .where(PageORM.collection == collection)
            .where(SectionORM.page_id.in_(list(page_ids)))
            .where(SectionORM.canon.is_not(None))
            .order_by(SectionORM.page_id, SectionORM.canon)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            OversizedSection(
                page_id=row.page_id, canon=row.canon, tokens=row.tokens if row.tokens is not None else token_count(row.body), body=row.body
            )
            for row in rows
            if row.canon is not None
        ]

    async def snippet_ranks(self, query: str, snippets: Sequence[str], *, collection: str) -> Sequence[int]:
        """Rank each snippet in one query as one plus the number of stored sections with a higher `ts_rank_cd` score.

        Args:
            query: The failing case's original question text.
            snippets: The texts to rank, an empty sequence returns nothing.
            collection: The collection whose sections are the competitor set.

        Returns:
            The 1-based ranks, in `snippets` order.
        """
        if not snippets:
            return []
        result = await self._session.execute(
            text(_SNIPPET_RANK_SQL), {"query": query, "snippets": list(snippets), "collection": collection}
        )
        rank_by_ord = {row.ord: row.rank for row in result}
        return [int(rank_by_ord[position]) for position in range(1, len(snippets) + 1)]

    async def page_types(self, page_ids: Sequence[str], *, collection: str) -> Mapping[str, str]:
        """Read each requested page's page type from `kb.pages`.

        Args:
            page_ids: The pages whose page types to read, an empty sequence returns an empty mapping.
            collection: The collection the pages must belong to.

        Returns:
            A mapping from page id to page type for the requested pages found in `collection`.
        """
        if not page_ids:
            return {}
        statement = select(PageORM.id, PageORM.page_type).where(PageORM.collection == collection).where(PageORM.id.in_(list(page_ids)))
        rows = (await self._session.execute(statement)).all()
        return {row.id: row.page_type for row in rows}

    async def reachable_pairs(
        self, pairs: Sequence[tuple[str, str]], *, max_hops: int, collection: str, extra_edges: Sequence[CandidateEdge] = ()
    ) -> Sequence[bool]:
        """Walk `kb.edges` plus `extra_edges` forward with a recursive CTE, whatever graph backend serves search.

        Args:
            pairs: The `(source, destination)` gold pairs to test, an empty sequence returns nothing.
            max_hops: The reachability horizon, clamped into `[1, 8]` inside the walk.
            collection: The collection whose edges the walk runs over.
            extra_edges: The candidate edges to inject for a counterfactual, empty for the real graph.

        Returns:
            One boolean per input pair, in the same order, True when the destination is reachable.
        """
        if not pairs:
            return []
        seeds = list(dict.fromkeys(src for src, _dst in pairs))
        edge_srcs = [edge.src_id for edge in extra_edges]
        edge_dsts = [edge.dst_id for edge in extra_edges]
        params: dict[str, str | int | list[str] | None] = {
            "collection": collection,
            "max_hops": max_hops,
            "seeds": seeds,
            "edge_srcs": edge_srcs or None,
            "edge_dsts": edge_dsts or None,
        }
        rows = (await self._session.execute(text(_REACHABILITY_SQL), params)).all()
        reachable = {(row[0], row[1]) for row in rows}
        return [(src, dst) in reachable for src, dst in pairs]

    async def unlifted_links(self, *, collection: str) -> Sequence[MinedLink]:
        """Read the `kb.link_refs` rows that no `kb.edges` row matches on source page, relation and destination.

        Args:
            collection: The collection whose link references to read.

        Returns:
            One `MinedLink` per link with no edge, ordered by the `link_refs` row id.
        """
        dst_page = aliased(PageORM)
        lifted = (
            select(EdgeORM.id)
            .where(EdgeORM.src_id == LinkRefORM.src_page_id)
            .where(EdgeORM.rel == LinkRefORM.rel)
            .where(EdgeORM.dst_id == LinkRefORM.dst_id)
            .exists()
        )
        statement = (
            select(
                LinkRefORM.src_page_id,
                LinkRefORM.src_kind,
                LinkRefORM.raw_target,
                LinkRefORM.dst_id,
                LinkRefORM.rel,
                dst_page.page_type.label("dst_type"),
            )
            .outerjoin(dst_page, dst_page.id == LinkRefORM.dst_id)
            .where(LinkRefORM.collection == collection)
            .where(~lifted)
            .order_by(LinkRefORM.id)
        )
        rows = (await self._session.execute(statement)).all()
        return [
            MinedLink(
                src_page_id=row.src_page_id,
                src_type=row.src_kind,
                raw_target=row.raw_target,
                dst_page_id=row.dst_id,
                dst_type=row.dst_type,
                rel=row.rel,
            )
            for row in rows
        ]

    async def stamp(self, *, collection: str) -> KnowledgeBaseStamp:
        """Read the fingerprint from `kb.index_fingerprint` and hash the sorted `kb.index_manifest` rows with sha256.

        Args:
            collection: The collection to stamp.

        Returns:
            The knowledge base stamp for `collection`.
        """
        spec_hash = (
            await self._session.execute(select(IndexFingerprintORM.spec_hash).where(IndexFingerprintORM.collection == collection))
        ).scalar_one_or_none()
        manifest_rows = (
            await self._session.execute(
                select(IndexManifestORM.path, IndexManifestORM.content_hash).where(IndexManifestORM.collection == collection)
            )
        ).all()
        n_pages = (
            await self._session.execute(select(func.count()).select_from(PageORM).where(PageORM.collection == collection))
        ).scalar_one()
        return KnowledgeBaseStamp(
            collection=collection,
            spec_hash=spec_hash or "",
            manifest_digest=_manifest_digest([(row.path, row.content_hash) for row in manifest_rows]),
            n_pages=n_pages,
        )

    async def changed_paths(self, since: datetime, *, collection: str) -> Sequence[str]:
        """Read the `kb.index_manifest` paths whose `indexed_at` is after `since`.

        A full rebuild stamps every row, so after one every path counts as changed.

        Args:
            since: The exclusive lower bound, typically the recommendation's `created_at`.
            collection: The collection whose manifest to read.

        Returns:
            The changed paths, ordered by path.
        """
        statement = (
            select(IndexManifestORM.path)
            .where(IndexManifestORM.collection == collection)
            .where(IndexManifestORM.indexed_at > since)
            .order_by(IndexManifestORM.path)
        )
        return list((await self._session.execute(statement)).scalars().all())


def _manifest_digest(rows: Sequence[tuple[str, str]]) -> str:
    """The sha256 of the sorted `(path, content_hash)` rows, which changes on any edit."""
    digest = hashlib.sha256()
    for path, content_hash in sorted(rows):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(content_hash.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
