r"""The recommendation loop's `SqlKnowledgeBaseGateway` over a real, migrated database.

Exercises :class:`~engram.evals.recommendation.infrastructure.gateway.SqlKnowledgeBaseGateway` — the four tight knowledge base ports the
recommendation reads through — against a freshly-migrated database inside the shared container (the `migrated`
fixture, the local Postgres by default, ParadeDB under `KNOWLEDGE_BASE_TEST_DIALECT=pg_search`). It connects as
the **owner/primary** `memory` role (the `migrated` URL's admin credentials), never `kb_reader` — 0003
walls the reader off `kb.*`, and the gateway reads those tables directly.

Rather than index a knowledge base, the module inserts the minimal `kb.*` rows directly over the admin
session — the sanctioned fallback the sibling seed-repository test uses — so each port's behaviour is
asserted against a hand-built, fully-known knowledge_base. The whole module skips when Docker is unavailable
(the `pg` fixture, via `migrated`, raises ``pytest.skip``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from engram.evals.recommendation.contracts import CandidateEdge
from engram.evals.recommendation.infrastructure.gateway import SqlKnowledgeBaseGateway
from engram.indexing.contracts import EdgeORM, IndexFingerprintORM, IndexManifestORM, LinkRefORM, PageORM, SectionORM

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine

_COLLECTION = "research"
_OTHER_COLLECTION = "cooking"


@pytest.fixture
async def engine(migrated: str) -> AsyncIterator[AsyncEngine]:
    """A throwaway admin engine over the freshly-migrated DB (owner role, `NullPool`)."""
    eng = create_async_engine(migrated, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


def _page(page_id: str, *, page_type: str = "paper", collection: str = _COLLECTION) -> PageORM:
    """A minimal `kb.pages` row."""
    return PageORM(
        id=page_id,
        collection=collection,
        page_type=page_type,
        slug=page_id.split(".", 1)[-1],
        title=page_id,
        status="synthesized",
        card_text=f"card for {page_id}",
        body_hash=f"hash-{page_id}",
    )


def _section(page_id: str, canon: str, tokens: int, *, ord_: int, collection: str = _COLLECTION) -> SectionORM:
    """A minimal `kb.sections` row with a canonical slug and a token count."""
    section_id = f"{page_id}::{canon}"
    return SectionORM(
        id=section_id,
        collection=collection,
        page_id=page_id,
        canon=canon,
        heading=canon.title(),
        path=f"s{ord_}",
        ord=ord_,
        body=f"section body {section_id}",
        tokens=tokens,
        body_hash=f"sec-hash-{section_id}",
    )


def _edge(src_id: str, rel: str, dst_id: str, *, src_kind: str = "paper", src_collection: str = _COLLECTION, dst_collection: str = _COLLECTION) -> EdgeORM:
    """A minimal resolved `kb.edges` row."""
    return EdgeORM(
        src_collection=src_collection, src_id=src_id, src_kind=src_kind, rel=rel, dst_collection=dst_collection, dst_id=dst_id, dst_kind="paper"
    )


def _link_ref(
    src_page_id: str,
    rel: str,
    raw_target: str,
    *,
    dst_id: str | None = None,
    dst_collection: str | None = None,
    src_kind: str = "paper",
    collection: str = _COLLECTION,
) -> LinkRefORM:
    """A minimal `kb.link_refs` row (a dangler when `dst_id` is None).

    `dst_collection` defaults to `collection` (a same-collection resolution) when `dst_id` is
    given; pass it explicitly for a cross-collection citation.
    """
    resolved_dst_collection = (dst_collection or collection) if dst_id is not None else None
    return LinkRefORM(
        collection=collection,
        src_page_id=src_page_id,
        src_kind=src_kind,
        rel=rel,
        raw_target=raw_target,
        dst_id=dst_id,
        dst_collection=resolved_dst_collection,
    )


def _manifest(path: str, content_hash: str, *, collection: str = _COLLECTION) -> IndexManifestORM:
    """A minimal `kb.index_manifest` row."""
    return IndexManifestORM(collection=collection, path=path, kind="paper", content_hash=content_hash, identity_hash=f"id-{content_hash}")


def _fingerprint(*, collection: str = _COLLECTION, spec_hash: str = "spec-abc") -> IndexFingerprintORM:
    """A minimal `kb.index_fingerprint` row."""
    return IndexFingerprintORM(
        collection=collection, indexer_version="1.0.0", spec_hash=spec_hash, vocabulary_hash="reg-1", embed_model="embed-1"
    )


async def _seed_knowledge_base(session: AsyncSession) -> None:
    """Insert a hand-built knowledge base exercising every gateway port.

    The shape:

    * pages `p.a`, `p.b`, `p.c`, `p.d` in research, plus an off-collection `p.other` in cooking.
    * sections: `p.a` is over budget on `findings` (900) and under on `methods` (100), `p.b` is over on
      `findings` (800), and `p.other` is over on `findings` (999) but off-collection.
    * edges: a `builds_on` chain `p.a -> p.b -> p.c` (so `p.a` reaches `p.c` in two hops but never `p.d`).
    * link_refs: a lifted `p.a builds_on p.b` (matches the edge, excluded), an unlifted resolved
      `p.a mentions p.d`, a dangler `p.a mentions Ghost`, and an off-collection link (excluded).
    * a manifest and fingerprint for the stamp.
    """
    session.add_all([_page("p.a"), _page("p.b"), _page("p.c"), _page("p.d"), _page("p.other", collection=_OTHER_COLLECTION)])
    # Flush the pages before the FK-bearing children (sections / link_refs FK kb.pages.id).
    await session.flush()
    session.add_all(
        [
            _section("p.a", "findings", 900, ord_=0),
            _section("p.a", "methods", 100, ord_=1),
            _section("p.b", "findings", 800, ord_=0),
            _section("p.other", "findings", 999, ord_=0, collection=_OTHER_COLLECTION),
        ]
    )
    session.add_all([_edge("p.a", "builds_on", "p.b"), _edge("p.b", "builds_on", "p.c")])
    session.add_all(
        [
            _link_ref("p.a", "builds_on", "P B", dst_id="p.b"),
            _link_ref("p.a", "mentions", "P D", dst_id="p.d"),
            _link_ref("p.a", "mentions", "Ghost"),
            _link_ref("p.other", "mentions", "P A", dst_id="p.a", dst_collection=_COLLECTION, collection=_OTHER_COLLECTION),
        ]
    )
    session.add_all(
        [
            _manifest("papers/a.md", "content-a"),
            _manifest("papers/b.md", "content-b"),
            _manifest("cooking/x.md", "content-x", collection=_OTHER_COLLECTION),
        ]
    )
    session.add(_fingerprint())
    await session.commit()


@pytest.fixture
async def seeded(engine: AsyncEngine) -> AsyncEngine:
    """The migrated engine after the hand-built knowledge base is inserted."""
    async with AsyncSession(engine, expire_on_commit=False) as session:
        await _seed_knowledge_base(session)
    return engine


class TestSectionsOverBudget:
    async def test_returns_only_over_budget_sections_on_requested_pages(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            stats = await SqlKnowledgeBaseGateway(session).sections_over_budget(("p.a", "p.b", "p.d"), token_budget=600, collection=_COLLECTION)

        # p.a/methods (100) is under budget, p.d has no sections, so only the two over-budget rows land.
        assert {(s.page_id, s.canon, s.tokens) for s in stats} == {("p.a", "findings", 900), ("p.b", "findings", 800)}

    async def test_excludes_off_collection_pages(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            stats = await SqlKnowledgeBaseGateway(session).sections_over_budget(("p.other",), token_budget=600, collection=_COLLECTION)

        # p.other's section is over budget but lives in cooking, so the research-scoped call excludes it.
        assert list(stats) == []

    async def test_empty_page_ids_returns_nothing(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            stats = await SqlKnowledgeBaseGateway(session).sections_over_budget((), token_budget=600, collection=_COLLECTION)

        assert list(stats) == []


class TestPageTypes:
    async def test_returns_kinds_for_requested_research_pages(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            kinds = await SqlKnowledgeBaseGateway(session).page_types(("p.a", "p.b", "p.other"), collection=_COLLECTION)

        # p.a/p.b are research papers; p.other is cooking, so the research-scoped call omits it.
        assert dict(kinds) == {"p.a": "paper", "p.b": "paper"}

    async def test_empty_page_ids_returns_nothing(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            kinds = await SqlKnowledgeBaseGateway(session).page_types((), collection=_COLLECTION)

        assert dict(kinds) == {}


class TestUnliftedLinks:
    async def test_returns_unlifted_and_dangling_links_only(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            links = await SqlKnowledgeBaseGateway(session).unlifted_links(collection=_COLLECTION)

        by_target = {link.raw_target: link for link in links}
        # The lifted builds_on->p.b link and the off-collection link are both excluded.
        assert set(by_target) == {"P D", "Ghost"}

    async def test_resolved_link_carries_destination_kind(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            links = await SqlKnowledgeBaseGateway(session).unlifted_links(collection=_COLLECTION)

        resolved = next(link for link in links if link.raw_target == "P D")
        assert resolved.src_page_id == "p.a"
        assert resolved.src_type == "paper"
        assert resolved.rel == "mentions"
        assert resolved.dst_page_id == "p.d"
        assert resolved.dst_type == "paper"

    async def test_dangler_has_no_destination(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            links = await SqlKnowledgeBaseGateway(session).unlifted_links(collection=_COLLECTION)

        dangler = next(link for link in links if link.raw_target == "Ghost")
        assert dangler.dst_page_id is None
        assert dangler.dst_type is None
        assert dangler.rel == "mentions"


class TestReachablePairs:
    async def test_reflects_the_real_edge_chain(self, seeded: AsyncEngine):
        pairs = [("p.a", "p.b"), ("p.a", "p.c"), ("p.a", "p.d")]
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            result = await SqlKnowledgeBaseGateway(session).reachable_pairs(pairs, max_hops=3, collection=_COLLECTION)

        # p.a -> p.b (1 hop) and p.a -> p.c (2 hops) reachable; p.d is unreachable over the real graph.
        assert list(result) == [True, True, False]

    async def test_respects_the_hop_bound(self, seeded: AsyncEngine):
        pairs = [("p.a", "p.b"), ("p.a", "p.c")]
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            result = await SqlKnowledgeBaseGateway(session).reachable_pairs(pairs, max_hops=1, collection=_COLLECTION)

        # At one hop only p.b is reachable from p.a; p.c is two hops away.
        assert list(result) == [True, False]

    async def test_candidate_edge_makes_an_unreachable_pair_reachable(self, seeded: AsyncEngine):
        pairs = [("p.a", "p.d")]
        candidate = [CandidateEdge(src_id="p.a", dst_id="p.d", rel="mentions")]
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            gateway = SqlKnowledgeBaseGateway(session)
            before = await gateway.reachable_pairs(pairs, max_hops=3, collection=_COLLECTION)
            after = await gateway.reachable_pairs(pairs, max_hops=3, collection=_COLLECTION, extra_edges=candidate)

        assert list(before) == [False]
        assert list(after) == [True]

    async def test_result_order_matches_input_order(self, seeded: AsyncEngine):
        pairs = [("p.a", "p.d"), ("p.a", "p.c"), ("p.a", "p.b")]
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            result = await SqlKnowledgeBaseGateway(session).reachable_pairs(pairs, max_hops=3, collection=_COLLECTION)

        assert list(result) == [False, True, True]

    async def test_empty_pairs_returns_nothing(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            result = await SqlKnowledgeBaseGateway(session).reachable_pairs([], max_hops=3, collection=_COLLECTION)

        assert list(result) == []


class TestStamp:
    async def test_reports_knowledge_base_identity_and_is_stable(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            gateway = SqlKnowledgeBaseGateway(session)
            first = await gateway.stamp(collection=_COLLECTION)
            second = await gateway.stamp(collection=_COLLECTION)

        assert first == second
        assert first.collection == _COLLECTION
        assert first.spec_hash == "spec-abc"
        # p.a/p.b/p.c/p.d are research; p.other is cooking and excluded.
        assert first.n_pages == 4
        assert first.manifest_digest

    async def test_digest_changes_when_a_manifest_row_changes(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            gateway = SqlKnowledgeBaseGateway(session)
            before = await gateway.stamp(collection=_COLLECTION)
            row = await session.get(IndexManifestORM, {"collection": _COLLECTION, "path": "papers/a.md"})
            assert row is not None
            row.content_hash = "content-a-CHANGED"
            await session.commit()
            after = await gateway.stamp(collection=_COLLECTION)

        assert after.manifest_digest != before.manifest_digest
        assert after.n_pages == before.n_pages == 4


class TestSectionsOverBudgetNullTokens:
    async def test_null_stored_tokens_fall_back_to_a_read_time_count(self, seeded: AsyncEngine):
        # The production indexer stores `kb.sections.tokens` as NULL, so the gateway must count the
        # body at read time: a big NULL-tokens body offends, a small one does not.
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            session.add(_page("p.null"))
            await session.flush()
            big = _section("p.null", "findings", 0, ord_=0)
            big.tokens = None
            big.body = "lorem ipsum dolor sit amet consectetur " * 200
            small = _section("p.null", "methods", 0, ord_=1)
            small.tokens = None
            small.body = "a short methods note"
            session.add_all([big, small])
            await session.commit()

            stats = await SqlKnowledgeBaseGateway(session).sections_over_budget(("p.null",), token_budget=600, collection=_COLLECTION)

        assert [(stat.page_id, stat.canon) for stat in stats] == [("p.null", "findings")]
        assert stats[0].tokens > 600


class TestOversizedSectionBodies:
    async def test_returns_over_budget_sections_with_their_bodies(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            rows = await SqlKnowledgeBaseGateway(session).oversized_section_bodies(("p.a", "p.b"), token_budget=600, collection=_COLLECTION)

        assert [(row.page_id, row.canon, row.tokens) for row in rows] == [("p.a", "findings", 900), ("p.b", "findings", 800)]
        assert rows[0].body == "section body p.a::findings"

    async def test_excludes_off_collection_and_under_budget_sections(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            rows = await SqlKnowledgeBaseGateway(session).oversized_section_bodies(("p.a", "p.other"), token_budget=600, collection=_COLLECTION)

        assert [(row.page_id, row.canon) for row in rows] == [("p.a", "findings")]

    async def test_empty_page_ids_returns_nothing(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            rows = await SqlKnowledgeBaseGateway(session).oversized_section_bodies((), token_budget=600, collection=_COLLECTION)

        assert list(rows) == []


class TestSnippetRanks:
    async def test_a_focused_part_ranks_at_least_as_well_as_the_diluted_whole(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            session.add_all([_page("p.q1", collection="ranktest"), _page("p.q2", collection="ranktest")])
            await session.flush()
            relevant = _section("p.q1", "findings", 50, ord_=0, collection="ranktest")
            relevant.body = "quantum entanglement experiments show strong correlations across separated particles"
            filler = _section("p.q2", "findings", 50, ord_=0, collection="ranktest")
            filler.body = "cooking pasta requires salted boiling water and a little patience"
            session.add_all([relevant, filler])
            await session.commit()
            diluted = ("various unrelated words about geology rivers mountains sediment erosion " * 20) + "quantum entanglement"
            focused = "quantum entanglement correlations measured in experiments"

            ranks = await SqlKnowledgeBaseGateway(session).snippet_ranks("quantum entanglement", (diluted, focused), collection="ranktest")

        assert len(ranks) == 2
        assert all(rank >= 1 for rank in ranks)
        # The log-length normalisation must not leave the focused part behind the diluted whole.
        assert ranks[1] <= ranks[0]

    async def test_an_empty_collection_ranks_every_snippet_first(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            ranks = await SqlKnowledgeBaseGateway(session).snippet_ranks("anything at all", ("alpha", "beta"), collection="nowhere")

        assert list(ranks) == [1, 1]

    async def test_empty_snippets_returns_nothing(self, seeded: AsyncEngine):
        async with AsyncSession(seeded, expire_on_commit=False) as session:
            ranks = await SqlKnowledgeBaseGateway(session).snippet_ranks("anything", (), collection=_COLLECTION)

        assert list(ranks) == []
