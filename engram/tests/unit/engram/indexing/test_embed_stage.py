"""Unit tests for the shared embed stage (`src/engram/indexing/service/embed_stage.py`).

`EmbedStage` is the hash-gated embed stage BOTH indexing paths run: the full path in
`service/index.py` and the incremental path in `service/incremental.py` delegate to it. These pin the
surface the two paths share:

* `pending()` materialises the four `embedding IS NULL` queues inside ONE short read UoW,
  the page / section / claim ids plus the `(chunk id, source_id)` pairs, so only plain
  values (never session-bound ORM rows) cross the UoW boundary;
* `run()` embeds the pending units in `(pages, sections, claims, chunks)` order, batch by
  batch, writes the embed-meta contract row, and returns `(embedded, reused)`;
* a run with nothing pending records the meta row and reuses every loaded input.

Everything runs over :class:`FakeKnowledgeBaseUnitOfWork` (recording repos) + :class:`FakeEmbedding`,
so there is no database and no network.
"""

from __future__ import annotations

from engram.indexing.read_models import PendingEmbedChunk, PendingEmbedRow
from engram.indexing.service.embed_stage import EmbedStage, PendingEmbeds
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork


class TestPending:
    async def test_materialises_ids_across_all_four_aggregates(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork(
            pages_needing=[PendingEmbedRow("paper.a"), PendingEmbedRow("paper.b")],
            sections_needing=[PendingEmbedRow("paper.a#tl_dr")],
            claims_needing=[PendingEmbedRow("paper.a#c1")],
            chunks_needing=[PendingEmbedChunk("KEY:0", "KEY")],
        )
        stage = EmbedStage(lambda: uow, FakeEmbedding())

        pending = await stage.pending(collection="research")

        assert isinstance(pending, PendingEmbeds)
        assert pending.page_ids == ("paper.a", "paper.b")
        assert pending.section_ids == ("paper.a#tl_dr",)
        assert pending.claim_ids == ("paper.a#c1",)
        assert pending.chunks == (("KEY:0", "KEY"),)

    async def test_chunks_carry_the_row_id_and_source_id_pair(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork(chunks_needing=[PendingEmbedChunk("A:0", "A"), PendingEmbedChunk("B:1", "B")])
        stage = EmbedStage(lambda: uow, FakeEmbedding())

        pending = await stage.pending(collection="research")

        assert pending.chunks == (("A:0", "A"), ("B:1", "B"))

    async def test_returns_plain_materialised_values_not_orm_rows(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow("paper.a")], chunks_needing=[PendingEmbedChunk("KEY:0", "KEY")])
        stage = EmbedStage(lambda: uow, FakeEmbedding())

        pending = await stage.pending(collection="research")

        # Only plain strings / (str, str) pairs cross the UoW boundary, no session-bound ORM row leaks.
        assert all(isinstance(page_id, str) for page_id in pending.page_ids)
        assert all(isinstance(chunk_id, str) and isinstance(key, str) for chunk_id, key in pending.chunks)

    async def test_empty_queues_materialise_to_empty_tuples(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()
        stage = EmbedStage(lambda: uow, FakeEmbedding())

        pending = await stage.pending(collection="research")

        assert pending == PendingEmbeds(page_ids=(), section_ids=(), claim_ids=(), chunks=())


class TestRun:
    async def test_embeds_pending_units_in_aggregate_order_and_counts(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork(
            pages_needing=[PendingEmbedRow("paper.a")],
            sections_needing=[PendingEmbedRow("paper.a#tl_dr")],
            claims_needing=[PendingEmbedRow("paper.a#c1")],
            chunks_needing=[PendingEmbedChunk("KEY:0", "KEY")],
        )
        embedder = FakeEmbedding()
        embed_inputs = {"paper.a": "page text", "paper.a#tl_dr": "section text", "paper.a#c1": "claim text", "KEY:0": "chunk text"}
        stage = EmbedStage(lambda: uow, embedder)

        outcome = await stage.run(embed_inputs, collection="research")

        assert (outcome.embedded, outcome.reused) == (4, 0)
        assert outcome.input_tokens == 8  # the fake counts words: two per input
        # Units embed in (pages, sections, claims, chunks) order, the same order the per-batch write groups by.
        assert embedder.texts == ["page text", "section text", "claim text", "chunk text"]
        assert [pair[0] for pair in uow.pages.embedded] == ["paper.a"]
        assert [pair[0] for pair in uow.sources.embedded_chunks] == ["KEY:0"]
        assert uow.meta.embed_meta == (embedder.model_id, embedder.dimensions)

    async def test_write_embeddings_carry_the_compile_s_collection(self) -> None:
        """The composite `(collection, id)` primary key means every embed write must name its collection."""
        uow = FakeKnowledgeBaseUnitOfWork(
            pages_needing=[PendingEmbedRow("paper.a")],
            sections_needing=[PendingEmbedRow("paper.a#tl_dr")],
            claims_needing=[PendingEmbedRow("paper.a#c1")],
            chunks_needing=[PendingEmbedChunk("KEY:0", "KEY")],
        )
        embed_inputs = {"paper.a": "page text", "paper.a#tl_dr": "section text", "paper.a#c1": "claim text", "KEY:0": "chunk text"}
        stage = EmbedStage(lambda: uow, FakeEmbedding())

        await stage.run(embed_inputs, collection="beta")

        assert uow.pages.embedded_collections == ["beta"]
        assert uow.sections.embedded_collections == ["beta"]
        assert uow.claims.embedded_collections == ["beta"]
        assert uow.sources.embedded_chunks_collections == ["beta"]

    async def test_nothing_pending_records_meta_and_reuses_all(self) -> None:
        uow = FakeKnowledgeBaseUnitOfWork()  # empty needing-set: nothing changed, nothing re-embeds
        embedder = FakeEmbedding()
        stage = EmbedStage(lambda: uow, embedder)

        outcome = await stage.run({"paper.a": "page text", "paper.b": "text"}, collection="research")

        assert outcome.embedded == 0
        assert outcome.reused == 2  # every loaded input was reused, nothing re-embedded
        assert outcome.input_tokens == 0
        assert embedder.texts == []
        assert uow.meta.embed_meta == (embedder.model_id, embedder.dimensions)
