"""Writes one authored page straight into `kb.*`, with no knowledge base file and no index run.

An ingested page ends at the same rows as an indexed one: the same page, section and claim rows, the same
embed inputs and the same search answers. Only its source differs, a client's payload in place of a file.

A collection is either indexed from a knowledge base or written by clients, never both. `IngestPageHandler`
refuses a collection that has an index manifest, because the next index would prune a page written without a file.

The payload is rendered and parsed with the note grammar before any transaction opens, so a malformed claim
or link fails first. When a vocabulary is configured, the page's entity type, facet values, claim types and
relations are checked against it too. Checks that need the whole knowledge base, such as duplicate ids and the
quote check, run only in `studio index`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.domain.models import IngestPage, IngestResult, PageIngested
from engram.indexing.domain.resolver import build_link_index_from_pages, resolve_with_index
from engram.indexing.domain.validator import validate_vocabulary
from engram.indexing.exceptions import CollectionIsIndexedError, IngestValidationError
from engram.indexing.service.embed_stage import EmbedStage
from engram.indexing.service.row_builder import RowBuilder, link_ref_rows
from engram.indexing.service.switch_stage import SwitchStage
from engram.kb.contracts import parse as parse_note
from engram.kb.contracts import render

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.ai.embedding import BatchEmbedder
    from core.cqrs.contracts import Command, Event

    from engram.indexing.internal_contracts import IngestUnitOfWork, ReaderScopes
    from engram.kb.contracts import KnowledgeBaseSchema, VocabularyIndex


class IngestPageHandler:
    """Handles `IngestPage`: renders, parses, resolves, upserts and embeds one authored page.

    Under a `mode: "auto"` spec every write decides and stores the collection's retrieval switches.
    """

    def __init__(
        self,
        uow_factory: Callable[[], IngestUnitOfWork],
        embedder: BatchEmbedder,
        *,
        spec: KnowledgeBaseSchema,
        vocabulary: VocabularyIndex | None = None,
        reader_scopes: ReaderScopes | None = None,
    ) -> None:
        """Create the handler over its unit of work factory, its embedder and the knowledge base's grammar.

        Args:
            uow_factory: Builds a fresh `IngestUnitOfWork` per transaction: one for the load, two for a `mode: "auto"`
                switch decision and one per embed batch.
            embedder: Embeds the page's rows and names the model recorded in `kb.embed_meta`.
            spec: The schema that governs render and parse. A payload carrying its own schema overrides it.
            vocabulary: The knowledge base's indexed vocabulary. When given, a page whose entity type, facet
                values, claim types or relations it does not recognise is refused before any write.
                `None` skips the check.
            reader_scopes: Gives a private knowledge base its reader role after a write. `None` leaves roles alone.
        """
        self._uow_factory = uow_factory
        self._embed_stage = EmbedStage(uow_factory, embedder)
        self._switch_stage = SwitchStage(uow_factory, reader_scopes)
        self._spec = spec
        self._vocabulary = vocabulary
        # The bus drains this after the handler returns.
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> IngestResult:
        """Handle a command the bus routed here, which must be an `IngestPage`.

        Args:
            command: The command to handle.

        Returns:
            What `handle` returns.

        Raises:
            TypeError: If `command` is not an `IngestPage`.
        """
        if not isinstance(command, IngestPage):
            msg = f"{type(self).__name__} handles IngestPage, got {type(command).__name__}"
            raise TypeError(msg)
        return await self.handle(command)

    async def handle(self, command: IngestPage) -> IngestResult:
        """Write one authored page into a written collection and embed what changed.

        Under a `mode: "auto"` spec the write then decides and stores the collection's switches from its
        signals, and embeds only when the decided embeddings switch is on. Under either mode it stores the collection's
        `retrieval.private` flag, so a private written collection is hidden from other sessions.

        Args:
            command: The page payload, its collection and whether to embed.

        Returns:
            The page id and the counts of rows written and embedded.

        Raises:
            IngestValidationError: If a vocabulary is configured and the page's entity type, facet
                values, claim types or relations do not match it.
            CollectionIsIndexedError: If a knowledge base indexes `command.collection` (it has a manifest),
                so the next index would prune the page this would write.
        """
        spec = command.payload.knowledge_base_schema if command.payload.knowledge_base_schema is not None else self._spec
        page_id = command.payload.validated_frontmatter().id
        # No file exists, so the path is a label, not a location: `parse` threads it onto the note
        # for validation messages, and `RowBuilder` takes the page's `slug` from its stem. Naming it
        # after the id makes the slug the id, which is one more key the page resolves under.
        note = parse_note(render(command.payload, spec=spec), source_path=f"{page_id}.md", spec=spec)

        if self._vocabulary is not None:
            validation = validate_vocabulary(note, self._vocabulary, spec=spec)
            if validation.n_errors:
                raise IngestValidationError(validation.errors)

        async with self._uow_factory() as uow:
            if manifest := await uow.manifest.load(command.collection):
                msg = (
                    f"collection {command.collection!r} is indexed from a knowledge base "
                    f"({len(manifest)} file(s) in its manifest): the next index would prune a page "
                    f"written without a file. Ingest into a collection of its own."
                )
                raise CollectionIsIndexedError(msg)

            index = build_link_index_from_pages(await uow.pages.key_rows(command.collection))
            resolved = resolve_with_index([note], index, relations=spec.relations)
            builder = RowBuilder(
                command.collection,
                section_summary_spec=spec.section_summaries,
                facets=spec.facets,
                claim_grammar=spec.claims,
                source_key_field=spec.sources.key_field,
                digest_link_field=spec.sources.digest_link_field,
                source_hash_field=spec.sources.hash_field,
                date_field=spec.retrieval.date_field,
            )
            for resolved_note in resolved.notes:
                builder.add_note(resolved_note)

            await uow.pages.upsert_many(builder.pages)
            await uow.sections.upsert_many(builder.sections)
            await uow.claims.upsert_many(builder.claims)
            await uow.claim_qualifiers.replace_for_pages(builder.claim_qualifiers, {page_id}, collection=command.collection)
            edge_delta = await uow.edges.replace_for_sources(builder.edges, {page_id}, collection=command.collection)
            await uow.link_refs.replace_for_sources(link_ref_rows(resolved, command.collection), {page_id}, collection=command.collection)
            if edge_delta:
                await uow.edges.refresh_graph()

            await uow.commit()

        # A `mode: "fixed"` spec leaves `command.embed` as the whole decision. A `mode: "auto"` spec decides
        # the switches from the collection as this write left it, and needs embeddings on, so `embed=True`
        # cannot force it.
        should_embed = command.embed
        decided = await self._switch_stage.run(collection=command.collection, spec=spec)
        if spec.retrieval.mode == "auto":
            should_embed = should_embed and decided is not None and decided.embeddings

        # Outside the load transaction, and scoped to this page's rows: `run` would claim every
        # `embedding IS NULL` row in the database, including ones this write knows no input for.
        outcome = await self._embed_stage.run_scoped(builder.embed_inputs, collection=command.collection) if should_embed else None
        self.events.append(
            PageIngested(page_id=page_id, collection=command.collection, sections=len(builder.sections), claims=len(builder.claims))
        )
        return IngestResult(
            page_id=page_id,
            collection=command.collection,
            sections=len(builder.sections),
            claims=len(builder.claims),
            edges=len(builder.edges),
            embedded=outcome.embedded if outcome else 0,
            reused=outcome.reused if outcome else 0,
        )
