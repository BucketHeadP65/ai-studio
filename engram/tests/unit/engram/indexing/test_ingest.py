"""Unit tests for the written-collection write path (`src/engram/indexing/service/ingest.py`).

`IngestPage` is the second way a page gets into `kb.*`: a client hands over a structured
`AuthoringPayload` and the page lands as data, with no file on disk and no index. These specs
drive the handler entirely through `FakeKnowledgeBaseUnitOfWork` + `FakeEmbedding`, so there is no database
and no network. They pin:

* the happy path: the page, its sections and its claims are upserted into the named collection,
  through the same `RowBuilder` the indexer uses, and the load transaction commits;
* the invariant that makes the direct write safe — a collection with a `index_manifest` is
  refused, because index would prune a page it has no file for, and nothing is written;
* resolution against the collection being joined: a link to an already-stored page becomes a real
  `kb.edges` row (and re-projects the graph), a link to nothing becomes a dangling `link_refs`
  row instead;
* embed scoping: only this write's rows are embedded, so an unrelated `embedding IS NULL` row left
  by an earlier `--no-embed` index neither gets claimed nor fails the ingest;
* `embed=False`: the rows land and the embedder is never called;
* the embeddings switch (`RetrievalSpec.mode`): a fixed-mode spec embeds whenever asked and decides
  nothing, an auto-mode write decides and stores the switches from the collection's signals and
  embeds only when the decided switch is on, and `embed=False` still vetoes either way.
* the `PageIngested` event the bus drains.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from engram.indexing.contracts import (
    CollectionIsIndexedError,
    IngestPage,
    IngestPageHandler,
    IngestResult,
    IngestValidationError,
    ManifestEntry,
    PageKeyRowResult,
    RetrievalSwitches,
    Signals,
)
from engram.indexing.domain.manifest import FileType
from engram.indexing.domain.models import PageIngested
from engram.indexing.read_models import PendingEmbedRow
from engram.kb.contracts import AuthoringPayload, parse_vocabulary
from engram.kb.domain.payload import AuthoringClaim, AuthoringSection
from engram.kb.domain.schema import RetrievalSpec
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork
from engram.testing.knowledge_base_builder import GOLDEN_MINI_VOCABULARIES
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pydantic import JsonValue

    from engram.kb.contracts import KnowledgeBaseSchema, VocabularyIndex

WRITTEN = "workflows"
"""A collection no knowledge base indexes — the only page_type `IngestPage` accepts."""

PAGE_ID = "ent.evidence-audit"

_BIG_SIGNALS = Signals(notes=500, token_estimate=300_000, page_types=1, edges_per_note=0.0, giant_component=0.0)
"""Past the embeddings on-threshold and below the SQL and graph off-thresholds, so a decision turns embeddings alone on."""

_BAND_SIGNALS = Signals(notes=150, token_estimate=1_000, page_types=1, edges_per_note=0.0, giant_component=0.0)
"""Inside the embeddings band, so a decision carries the stored embeddings switch through unchanged."""



def _payload(
    *,
    page_id: str = PAGE_ID,
    is_a: tuple[str, ...] = (),
    claims: tuple[AuthoringClaim, ...] = (),
    spec: KnowledgeBaseSchema | None = None,
) -> AuthoringPayload:
    """A well-formed entity payload: two canonical sections, optional `is_a` links and claims.

    `spec` is the payload's own carried spec, which `IngestPageHandler.handle` uses in place of
    the handler's own when set (see its docstring) — pass the same spec a test gives `_handler`
    when the test means to vary retrieval-mode behaviour, since the payload's spec wins.
    """
    frontmatter: dict[str, JsonValue] = {
        "id": page_id,
        "type": "entity",
        "title": "Evidence audit",
        "summary": "A multi-pass audit over a contested claim.",
        "status": "draft",
        "entity_type": "workflow",
    }
    if is_a:
        frontmatter["is_a"] = list(is_a)
    return AuthoringPayload(
        frontmatter=frontmatter,
        sections=(
            AuthoringSection(heading="Definition", body="Audit a contested claim across several retrieval strategies."),
            AuthoringSection(heading="Mechanism", body="Search, fetch, weigh the contradictions, publish behind a gate."),
        ),
        claims=claims,
        knowledge_base_schema=spec if spec is not None else research_spec(),
    )


def _handler(
    uow: FakeKnowledgeBaseUnitOfWork,
    embedder: FakeEmbedding | None = None,
    *,
    vocabulary: VocabularyIndex | None = None,
    spec: KnowledgeBaseSchema | None = None,
) -> IngestPageHandler:
    """Build the handler over the recording UoW; one factory serves the load and the embed batches.

    `vocabulary` is `None` by default — the vocabulary gate is opt-in, so every test that does not
    pass one exercises the handler exactly as it behaved before the gate existed. `spec` governs
    ingest only when the payload carries none of its own; `_payload()` always carries the research
    spec (`mode="fixed"`) unless given a different one, so a test that means to vary retrieval-mode
    behaviour must pass the same spec to both `_handler` and `_payload`.
    """
    return IngestPageHandler(lambda: uow, embedder or FakeEmbedding(), vocabulary=vocabulary, spec=spec if spec is not None else research_spec())


def _spec(*, mode: Literal["fixed", "auto"]) -> KnowledgeBaseSchema:
    """The bundled research default with only `retrieval.mode` overridden."""
    base = research_spec()
    return base.model_copy(update={"retrieval": RetrievalSpec(mode=mode)})


def _golden_registry() -> VocabularyIndex:
    """The real mini-vocabularies (entity types, relations, facets) `IngestPage` would refuse against."""
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(GOLDEN_MINI_VOCABULARIES.glob("*.md"))]
    return parse_vocabulary(docs).index


def _indexed_manifest() -> dict[str, ManifestEntry]:
    """A prior index's manifest — one file is enough to make the collection knowledge_base-backed."""
    return {
        "Memory/Entities/evidence-audit.md": ManifestEntry(
            path="Memory/Entities/evidence-audit.md",
            kind=FileType.NOTE,
            content_hash="abcd",
            identity_hash="ef01",
            page_id=PAGE_ID,
            source_key=None,
        )
    }


class TestTheWrite:
    async def test_the_page_and_its_sections_are_upserted_into_the_named_collection(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert isinstance(result, IngestResult)
        assert [row["id"] for row in uow.pages.upserted_rows] == [PAGE_ID]
        assert {row["collection"] for row in uow.pages.upserted_rows} == {WRITTEN}
        assert {str(row["path"]) for row in uow.sections.upserted_rows} == {"definition", "mechanism"}
        assert result.sections == 2
        assert uow.committed is True

    async def test_sections_carry_a_derived_summary(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        summaries = {str(row["path"]): row["summary"] for row in uow.sections.upserted_rows}
        assert summaries == {
            "definition": "Audit a contested claim across several retrieval strategies.",
            "mechanism": "Search, fetch, weigh the contradictions, publish behind a gate.",
        }

    async def test_the_page_row_carries_the_compilers_card_text_and_body_hash(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        page = uow.pages.upserted_rows[0]
        assert "Evidence audit" in str(page["card_text"])
        # The hash gate the repos re-embed on: a page written as data must carry it, or its vector
        # would never be invalidated when the payload changes.
        assert page["body_hash"]

    async def test_the_slug_is_the_page_id_so_the_page_resolves_under_it(self):
        # There is no file, so the indexer's "slug = filename stem" rule needs a stand-in; naming
        # the synthetic path after the id makes the id one of the page's resolution keys.
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert uow.pages.upserted_rows[0]["slug"] == PAGE_ID

    async def test_a_nested_mapping_in_frontmatter_lands_verbatim_in_the_pages_jsonb(self):
        # The reason a machine-written artifact can live in its page rather than beside it: the
        # long-tail projection carries a plain nested mapping into `kb.pages.frontmatter`, so a
        # client's structured payload (a spec, a config block) round-trips as data — rendered to
        # YAML on the way in, parsed back, and projected into jsonb without a lossy flattening.
        artifact = {"name": "evidence-audit", "version": 2, "nodes": [{"page_type": "run", "id": "orient"}]}
        payload = _payload()
        payload = AuthoringPayload(frontmatter={**payload.frontmatter, "artifact": artifact}, sections=payload.sections, knowledge_base_schema=research_spec())
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=payload))

        frontmatter = uow.pages.upserted_rows[0]["frontmatter"]
        assert isinstance(frontmatter, dict)
        assert frontmatter["artifact"] == artifact

    async def test_claims_land_as_rows(self):
        claim = AuthoringClaim(text="Hybrid retrieval beats lexical on this shelf.", loc="§2", kind="benchmark-result")
        uow = FakeKnowledgeBaseUnitOfWork()

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload(claims=(claim,))))

        assert result.claims == 1
        assert uow.claims.upserted_rows[0]["page_id"] == PAGE_ID

    async def test_claim_qualifiers_land_as_rows_ordered_by_multilink_target(self):
        # Two `on_dataset` targets plus one `on_task` and one `metric` — the ingest path must write
        # the same qualifier rows an index would, not silently drop them (index/ingest.py wires
        # `uow.claim_qualifiers.replace_for_pages` alongside `uow.claims.upsert_many`).
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.pages.key_row_results = [
            PageKeyRowResult(id="ent.dataset-a", slug="dataset-a", title="Dataset A", aliases=(), source_id=None),
            PageKeyRowResult(id="ent.dataset-b", slug="dataset-b", title="Dataset B", aliases=(), source_id=None),
            PageKeyRowResult(id="ent.task-x", slug="task-x", title="Task X", aliases=(), source_id=None),
            PageKeyRowResult(id="ent.metric-y", slug="metric-y", title="Metric Y", aliases=(), source_id=None),
        ]
        claim = AuthoringClaim(
            text="Hybrid retrieval beats lexical on both benchmarks.",
            loc="§2",
            kind="benchmark-result",
            links={
                "on_dataset": ("[[ent.dataset-a]]", "[[ent.dataset-b]]"),
                "on_task": ("[[ent.task-x]]",),
                "metric": ("[[ent.metric-y]]",),
            },
        )

        await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload(claims=(claim,))))

        (batch,) = uow.claim_qualifiers.qualifier_rows
        dataset_rows = [row for row in batch if row["qualifier"] == "on_dataset"]
        assert [(row["ord"], row["target_id"]) for row in dataset_rows] == [(0, "ent.dataset-a"), (1, "ent.dataset-b")]
        assert [(row["qualifier"], row["ord"], row["target_id"]) for row in batch if row["qualifier"] == "on_task"] == [
            ("on_task", 0, "ent.task-x")
        ]
        assert [(row["qualifier"], row["ord"], row["target_id"]) for row in batch if row["qualifier"] == "metric"] == [
            ("metric", 0, "ent.metric-y")
        ]
        assert uow.claim_qualifiers.replaced_page_ids == [{PAGE_ID}]
        assert uow.claim_qualifiers.replaced_collections == [WRITTEN]


class TestACompiledCollectionIsRefused:
    async def test_a_collection_with_a_manifest_is_refused(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.manifest.stored = _indexed_manifest()

        with pytest.raises(CollectionIsIndexedError, match="indexed from a knowledge base"):
            await _handler(uow).handle(IngestPage(collection="research", payload=_payload()))

    async def test_the_refusal_writes_nothing_and_does_not_commit(self):
        # The whole point of the check: a write that lands and is then pruned by the next index
        # looks like it worked. Refusing has to happen before any upsert, inside the transaction.
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.manifest.stored = _indexed_manifest()

        with pytest.raises(CollectionIsIndexedError):
            await _handler(uow).handle(IngestPage(collection="research", payload=_payload()))

        assert uow.pages.upserts == []
        assert uow.sections.upserts == []
        assert uow.committed is False
        assert uow.rolled_back is True

    async def test_an_empty_manifest_is_a_written_collection_and_is_accepted(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert uow.committed is True


class TestTheVocabularyGate:
    """A page whose vocabulary the vocabulary does not recognise is refused before any write.

    `_payload()`'s `entity_type="workflow"` is not in the golden mini-vocabularies (`technique`,
    `task`, `dataset`, `benchmark`, `metric`, `model`), so it stands in for an agent-authored value
    the knowledge base has never seen.
    """

    async def test_unknown_entity_type_is_refused_with_the_named_finding(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        with pytest.raises(IngestValidationError) as exc_info:
            await _handler(uow, vocabulary=_golden_registry()).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        (finding,) = exc_info.value.findings
        assert finding.rule == "unknown-entity-type"
        assert "workflow" in finding.message
        assert finding.page_id == PAGE_ID

    async def test_the_refusal_writes_nothing_and_does_not_commit(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        with pytest.raises(IngestValidationError):
            await _handler(uow, vocabulary=_golden_registry()).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert uow.pages.upserts == []
        assert uow.committed is False
        assert uow.rolled_back is False  # never entered: the gate runs before the UoW opens

    async def test_a_refused_write_emits_nothing(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(uow, vocabulary=_golden_registry())

        with pytest.raises(IngestValidationError):
            await handler.handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert handler.events == []

    async def test_a_registered_entity_type_is_accepted(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        payload = AuthoringPayload(frontmatter={**_payload().frontmatter, "entity_type": "technique"}, knowledge_base_schema=research_spec())

        result = await _handler(uow, vocabulary=_golden_registry()).handle(IngestPage(collection=WRITTEN, payload=payload))

        assert isinstance(result, IngestResult)
        assert uow.committed is True

    async def test_no_registry_configured_skips_the_gate(self):
        # The default `_handler(uow)` (no `vocabulary=`) never runs the check, so the same
        # unregistered `entity_type` that test_unknown_entity_type_is_refused_with_the_named_finding
        # refuses lands cleanly here — pinning the gate as opt-in, not always-on.
        uow = FakeKnowledgeBaseUnitOfWork()

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert isinstance(result, IngestResult)
        assert uow.committed is True


class TestResolutionAgainstTheStoredCollection:
    async def test_a_link_to_a_stored_page_becomes_an_edge_and_reprojects_the_graph(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.pages.key_row_results = [
            PageKeyRowResult(id="ent.audit-pattern", slug="audit-pattern", title="Audit pattern", aliases=(), source_id=None)
        ]

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload(is_a=("[[audit-pattern]]",))))

        assert result.edges == 1
        assert uow.edges.replaced_src_ids == [{PAGE_ID}]
        assert uow.edges.replaced_for_sources_collections == [WRITTEN]
        assert uow.edges.refreshed == 1

    async def test_a_link_to_nothing_is_recorded_as_a_dangling_ref_not_an_edge(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload(is_a=("[[not-here]]",))))

        assert result.edges == 0
        assert [str(row["raw_target"]) for row in uow.link_refs.link_ref_rows[0]] == ["not-here"]
        assert uow.link_refs.replaced_collections == [WRITTEN]

    async def test_a_page_with_no_links_neither_writes_edges_nor_reprojects(self):
        uow = FakeKnowledgeBaseUnitOfWork()

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert result.edges == 0
        assert uow.edges.refreshed == 0


class TestEmbedding:
    async def test_only_this_writes_rows_are_embedded(self):
        # The reason `run_scoped` exists: the knowledge-base-wide `run` would claim the foreign NULL row,
        # find no embed input for it, and refuse — failing an ingest over another writer's backlog.
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID), PendingEmbedRow("paper.left-over")])

        result = await _handler(uow).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert [row_id for row_id, _vector in uow.pages.embedded] == [PAGE_ID]
        assert result.embedded == 1

    async def test_a_row_whose_embed_input_is_unchanged_is_reused_not_re_embedded(self):
        # An empty NULL queue is the hash gate saying nothing changed: the repos NULL a vector
        # exactly when its embed input does.
        uow = FakeKnowledgeBaseUnitOfWork()
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder).handle(IngestPage(collection=WRITTEN, payload=_payload()))

        assert result.embedded == 0
        assert result.reused == 3  # the page + its two sections
        assert embedder.texts == []

    async def test_no_embed_lands_the_rows_and_never_calls_the_embedder(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder).handle(IngestPage(collection=WRITTEN, payload=_payload(), embed=False))

        assert uow.pages.upserted_rows[0]["id"] == PAGE_ID
        assert embedder.texts == []
        assert (result.embedded, result.reused) == (0, 0)

    async def test_a_fixed_mode_spec_embeds_regardless_of_the_stored_switches(self):
        # Fixed-mode invariance: the stored switches default to all-off, and a fixed-mode spec
        # must ignore that entirely -- `embed=True` is still the whole decision, byte-identical to
        # the handler's behaviour before switches existed.
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder, spec=_spec(mode="fixed")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="fixed")))
        )

        assert [row_id for row_id, _vector in uow.pages.embedded] == [PAGE_ID]
        assert result.embedded == 1

    async def test_an_auto_mode_spec_with_the_switch_off_does_not_call_the_embedder(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder, spec=_spec(mode="auto")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="auto")))
        )

        assert embedder.texts == []
        assert (result.embedded, result.reused) == (0, 0)

    async def test_an_auto_mode_spec_with_the_switch_on_calls_the_embedder(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        uow.fingerprint.stored_switches = RetrievalSwitches(embeddings=True)
        uow.signals.signals = _BAND_SIGNALS
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder, spec=_spec(mode="auto")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="auto")))
        )

        assert [row_id for row_id, _vector in uow.pages.embedded] == [PAGE_ID]
        assert result.embedded == 1

    async def test_embed_false_vetoes_an_auto_mode_spec_even_with_the_switch_on(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        uow.fingerprint.stored_switches = RetrievalSwitches(embeddings=True)
        uow.signals.signals = _BAND_SIGNALS
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder, spec=_spec(mode="auto")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="auto")), embed=False)
        )

        assert embedder.texts == []
        assert (result.embedded, result.reused) == (0, 0)


class TestAnAutoModeWriteDecidesTheSwitches:
    async def test_the_write_decides_the_switches_from_the_collections_signals_and_stores_them(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        uow.signals.signals = _BIG_SIGNALS
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder, spec=_spec(mode="auto")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="auto")))
        )

        assert uow.signals.measured_collections == [WRITTEN]
        assert uow.fingerprint.set_switches_calls == [(WRITTEN, RetrievalSwitches(embeddings=True))]
        assert result.embedded == 1

    async def test_a_decision_below_the_thresholds_turns_a_stored_switch_off_and_skips_the_embed(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        uow.fingerprint.stored_switches = RetrievalSwitches(embeddings=True)
        embedder = FakeEmbedding()

        result = await _handler(uow, embedder, spec=_spec(mode="auto")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="auto")))
        )

        assert uow.fingerprint.set_switches_calls == [(WRITTEN, RetrievalSwitches())]
        assert embedder.texts == []
        assert result.embedded == 0

    async def test_embed_false_still_decides_the_switches(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        uow.signals.signals = _BIG_SIGNALS
        embedder = FakeEmbedding()

        await _handler(uow, embedder, spec=_spec(mode="auto")).handle(
            IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="auto")), embed=False)
        )

        assert uow.fingerprint.set_switches_calls == [(WRITTEN, RetrievalSwitches(embeddings=True))]
        assert embedder.texts == []

    async def test_a_fixed_mode_write_decides_nothing(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        uow.signals.signals = _BIG_SIGNALS

        await _handler(uow, spec=_spec(mode="fixed")).handle(IngestPage(collection=WRITTEN, payload=_payload(spec=_spec(mode="fixed"))))

        assert uow.signals.measured_collections == []
        assert uow.fingerprint.set_switches_calls == []


    async def test_a_fixed_mode_write_stores_a_private_collection_s_flag(self):
        uow = FakeKnowledgeBaseUnitOfWork(pages_needing=[PendingEmbedRow(PAGE_ID)])
        private = research_spec().model_copy(update={"retrieval": RetrievalSpec(mode="fixed", private=True)})

        await _handler(uow, spec=private).handle(IngestPage(collection=WRITTEN, payload=_payload(spec=private)))

        assert uow.signals.measured_collections == []
        assert uow.fingerprint.set_switches_calls == [(WRITTEN, RetrievalSwitches(private=True))]

class TestTheBusContract:
    async def test_emits_page_ingested_with_the_row_counts(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        handler = _handler(uow)

        await handler.handle(IngestPage(collection=WRITTEN, payload=_payload()))

        ingested = [event for event in handler.events if isinstance(event, PageIngested)]
        assert len(ingested) == 1
        assert (ingested[0].page_id, ingested[0].collection, ingested[0].sections) == (PAGE_ID, WRITTEN, 2)

    async def test_a_refused_write_emits_nothing(self):
        uow = FakeKnowledgeBaseUnitOfWork()
        uow.manifest.stored = _indexed_manifest()
        handler = _handler(uow)

        with pytest.raises(CollectionIsIndexedError):
            await handler.handle(IngestPage(collection="research", payload=_payload()))

        assert handler.events == []

    async def test_the_bus_entry_point_refuses_a_command_it_does_not_handle(self):
        from engram.indexing.contracts import IndexKnowledgeBase

        handler = _handler(FakeKnowledgeBaseUnitOfWork())

        with pytest.raises(TypeError, match="handles IngestPage"):
            await handler(IndexKnowledgeBase(collection="research"))


class TestThePayloadIsTheGrammarGate:
    async def test_malformed_frontmatter_is_rejected_before_any_transaction(self):
        # Not the handler's doing — `AuthoringPayload` validates through `parse_frontmatter` on
        # construction, so a bad page cannot even be packaged into the command.
        with pytest.raises(ValueError, match="entity_type"):
            AuthoringPayload(frontmatter={"id": PAGE_ID, "type": "entity", "title": "No entity_type", "status": "draft"}, knowledge_base_schema=research_spec())

    async def test_an_id_without_its_types_prefix_is_rejected(self):
        with pytest.raises(ValueError, match="ent\\."):
            AuthoringPayload(
                frontmatter={
                    "id": "workflow.evidence-audit",
                    "type": "entity",
                    "title": "Wrong prefix",
                    "status": "draft",
                    "entity_type": "workflow",
                },
                knowledge_base_schema=research_spec(),
            )
