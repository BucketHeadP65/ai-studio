"""The index command handler: rebuilds a collection's `kb.*` rows from its knowledge base files.

`IndexKnowledgeBaseHandler` handles `IndexKnowledgeBase`. It parses the knowledge base, then takes one of three paths.
A dry run previews the files a real run would process and writes nothing. A full run resolves, validates, loads
and embeds every file. An incremental run does the same for the files that changed and the pages that link to them.

A note that fails to parse becomes a validation error and never aborts the run. Validation errors do not stop
the load either: the rows still load, flagged. Each load is one transaction that also prunes the rows whose files
are gone, scoped to the collection. Embedding runs only when the command asks for it, in short transactions of
its own.

After a real run, pending disclosure summaries are written when the command asks for them, the retrieval
switches are decided, and the validation report is written back. Every dependency is injected, so the handler
runs against fakes with no database, no network and no filesystem.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from core.cqrs.contracts import narrow_command
from core.observability import get_logger

from engram.indexing.domain import card
from engram.indexing.domain.manifest import FileType, ManifestEntry, changed_page_ids, identity_hash
from engram.indexing.domain.mirror_chunks import DEFAULT_CHUNK_SIZES, DEFAULT_PLANNER_CEILING_TOKENS
from engram.indexing.domain.models import (
    CitingPagesChanged,
    CutPlanCounts,
    EmbeddingsRefreshed,
    IndexingCompleted,
    IndexingStarted,
    IndexKnowledgeBase,
    IndexReport,
    MirrorDoc,
    NotesParsed,
    PagesLoaded,
    ValidationCompleted,
    ValidationIssue,
    loadable_mirrors,
)
from engram.indexing.domain.resolver import resolve
from engram.indexing.domain.validator import mirror_provider_issues, validate
from engram.indexing.exceptions import CollectionIsForeignError
from engram.indexing.read_models import CleanPage
from engram.indexing.service.embed_stage import EmbedStage
from engram.indexing.service.incremental import RunIncrementalDeps, preview_dirty_set, run_incremental
from engram.indexing.service.orientation import read_orientation
from engram.indexing.service.row_builder import (
    LoadSet,
    RowBuilder,
    digest_page_ids,
    duplicate_id_errors,
    id_str,
    link_ref_rows,
    manifest_rows,
    page_type_contract_rows,
    row_ids,
    validation_row,
)
from engram.indexing.service.switch_stage import SwitchStage
from engram.kb.contracts import KnowledgeBaseSchema, parse_vocabulary
from engram.kb.contracts import parse as parse_note

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder
    from core.ai.retrieval.contracts import Row
    from core.cqrs.contracts import Command, Event

    from engram.indexing.domain.mirror_chunks import ChunkSizes
    from engram.indexing.domain.models import ValidationReport
    from engram.indexing.domain.resolver import ResolveResult
    from engram.indexing.internal_contracts import IncrementalDeps, IndexUnitOfWork, KnowledgeBaseReader, ReaderScopes, ValidationReportSink
    from engram.indexing.read_models import StoredFingerprint
    from engram.indexing.service.cut_plan_stage import CutPlanStage
    from engram.indexing.service.summary_stage import SummaryStage
    from engram.kb.contracts import KnowledgeBaseNote, VocabularyIndex

_logger = get_logger(__name__)


INDEXER_VERSION = "8"
"""The indexer's version, stored in the index fingerprint with the chunk sizes and the chunk planner (`indexer_version`). Another stored version forces a full rebuild.

You must change it whenever a change alters what the index writes, such as a new parse rule or a new row shape.
"""


def indexer_version(chunk_sizes: ChunkSizes, *, planner: str | None = None, planner_ceiling_tokens: int = DEFAULT_PLANNER_CEILING_TOKENS) -> str:
    """The indexer version an index fingerprint stores: `INDEXER_VERSION`, the chunk sizes and the chunk planner the mirrors were cut by.

    A stored fingerprint whose version differs forces a full rebuild, so a change of chunk sizes, of chunk planner or of
    its ceiling rebuilds every mirror.

    Args:
        chunk_sizes: How many tokens a chunk of a source mirror aims for and may hold.
        planner: The cut plan identity (`cut_plan_identity`), or `None` when the index has no chunk planner.
        planner_ceiling_tokens: The most tokens of text the chunk planner is shown at once. Ignored with no chunk planner.

    Returns:
        The version, such as `7+400-800`, or `7+400-800+12000+<planner>` with a chunk planner.
    """
    version = f"{INDEXER_VERSION}+{chunk_sizes.target_tokens}-{chunk_sizes.max_tokens}"
    return version if planner is None else f"{version}+{planner_ceiling_tokens}+{planner}"


class IndexKnowledgeBaseHandler:
    """Handles `IndexKnowledgeBase`: the idempotent rebuild of a collection's rows from its knowledge base."""

    def __init__(
        self,
        kb_reader: KnowledgeBaseReader,
        embedder: BatchEmbedder,
        uow_factory: Callable[[], IndexUnitOfWork],
        validation_report_sink: ValidationReportSink,
        *,
        spec: KnowledgeBaseSchema,
        clock: Callable[[], float] = time.monotonic,
        summary_stage: SummaryStage | None = None,
        path_root: Path | None = None,
        path_root_source: str | None = None,
        reader_scopes: ReaderScopes | None = None,
        chunk_sizes: ChunkSizes = DEFAULT_CHUNK_SIZES,
        cut_plan_stage: CutPlanStage | None = None,
    ) -> None:
        """Create the handler over its reader, embedder, unit of work factory and the knowledge base's schema.

        Args:
            kb_reader: Reads the knowledge base's markdown files.
            embedder: Embeds batches of text and names the model recorded in `kb.embed_meta`.
            uow_factory: Builds a fresh `IndexUnitOfWork` per call, one for the load and one per embed batch.
            validation_report_sink: Receives the validation report after a real run.
            spec: The knowledge base's schema, which governs sections, the claim grammar and facets.
            clock: Monotonic seconds for the report's duration.
            summary_stage: Writes pending disclosure summaries when `command.summaries` is set.
                `None` makes `summaries=True` do nothing.
            path_root: The root the `dangling-code-path` check resolves the schema's `path_fields` values against.
                `None` skips that check.
            path_root_source: Where `path_root` came from, named in a `dangling-code-path` message.
                `None` leaves it out.
            reader_scopes: Gives a private knowledge base its reader role after each real run. `None` leaves roles alone.
            chunk_sizes: How many tokens a chunk of a source mirror aims for and may hold, on the full and the incremental path.
            cut_plan_stage: Gives the mirrors of each load the cut plans the chunk planner chose, on the full and the incremental
                path, when the schema stores source chunks. Its planner and ceiling enter the stored indexer version either way.
                `None` fills every heading section over the maximum chunk size.
        """
        self._kb_reader = kb_reader
        self._embedder = embedder
        self._uow_factory = uow_factory
        self._embed_stage = EmbedStage(uow_factory, embedder)
        self._switch_stage = SwitchStage(uow_factory, reader_scopes)
        self._validation_report_sink = validation_report_sink
        self._spec = spec
        self._clock = clock
        self._summary_stage = summary_stage
        self._path_root = path_root
        self._path_root_source = path_root_source
        self._chunk_sizes = chunk_sizes
        self._indexer_version = (
            indexer_version(chunk_sizes, planner=cut_plan_stage.planner, planner_ceiling_tokens=cut_plan_stage.ceiling_tokens)
            if cut_plan_stage is not None
            else indexer_version(chunk_sizes)
        )
        # A schema that stores no source chunk has nothing to cut, so it asks no planner. Its version still names the planner,
        # so it matches the version a host builds from the same settings.
        self._cut_plan_stage = cut_plan_stage if "source-chunk" in spec.active_embed_strategies() else None
        self.events: list[Event] = []
        self._clean_page_view: Callable[[], Sequence[CleanPage]] | None = None

    def set_clean_page_view(self, view: Callable[[], Sequence[CleanPage]]) -> None:
        """Replace the clean-page view the incremental path reads, which is otherwise read from the database.

        Args:
            view: Returns the clean pages the incremental path starts from.
        """
        self._clean_page_view = view

    async def __call__(self, command: Command) -> IndexReport:
        """Handle a command the bus routed here, which must be an `IndexKnowledgeBase`.

        Args:
            command: The command to handle.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, IndexKnowledgeBase, handler="IndexKnowledgeBaseHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: IndexKnowledgeBase) -> IndexReport:
        """Index the knowledge base, incrementally unless a full rebuild is needed.

        A dry run returns a preview of what a real run would process and writes nothing. A full rebuild runs
        when `command.full` is set, when the stored fingerprint differs from the current indexer version,
        schema, vocabulary or embedding model, or when the collection has no manifest yet.
        Otherwise only the changed files and the pages that link to them are indexed.

        Args:
            command: The collection to index and the run's options.

        Returns:
            The run's report. Its `mode` names the path taken.

        Raises:
            CollectionIsForeignError: If another knowledge base root indexed the collection and the command
                did not name it.
        """
        started = self._clock()
        self._emit(IndexingStarted(knowledge_base_path=self._knowledge_base_path()))

        parsed = self._parse_stage(command.collection)
        self._emit(NotesParsed(n_memory=len(parsed.notes), n_sources=len(parsed.sources)))

        spec_hash = card.content_hash(self._spec.model_dump_json())
        vocabulary_hash = card.content_hash(parsed.vocabulary_text)
        if command.dry_run:
            return await self._dry_run_path(command, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash, started=started)
        await self._refuse_foreign_collection(command)
        if not command.full and await self._can_incremental(command.collection, spec_hash, vocabulary_hash):
            return await self._incremental_path(command, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash, started=started)
        return await self._full_path(command, parsed, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash, started=started)

    async def _dry_run_path(self, command: IndexKnowledgeBase, *, spec_hash: str, vocabulary_hash: str, started: float) -> IndexReport:
        """Return the dry-run preview on a report whose `mode` is the path a real run would take, writing nothing."""
        deps = await self._incremental_deps(command.collection, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash)
        preview = await preview_dirty_set(command, deps=deps)

        self._emit(ValidationCompleted(n_errors=0, n_warnings=0))
        self._emit(PagesLoaded(collection=command.collection, pages=0, sections=0, claims=0, edges=0, pruned=0, page_ids=()))
        self._emit(CitingPagesChanged(collection=command.collection, page_ids=()))

        report = IndexReport(
            mode="full" if preview.would_full else "incremental", dry_run=preview, duration_seconds=self._clock() - started
        )
        self._emit(IndexingCompleted(report=report))
        return report

    async def _full_path(
        self, command: IndexKnowledgeBase, parsed: _ParsedKnowledgeBase, *, spec_hash: str, vocabulary_hash: str, started: float
    ) -> IndexReport:
        """Resolve, validate, load and embed every note, pruning whatever the knowledge base no longer carries."""
        resolved = resolve((note for note in parsed.notes), relations=self._spec.relations)
        validation = self._validate_stage(resolved, parsed)
        self._emit(ValidationCompleted(n_errors=validation.n_errors, n_warnings=validation.n_warnings))

        cut_plans = CutPlanCounts()
        if self._cut_plan_stage is not None:
            sources, cut_plans = await self._cut_plan_stage.run(parsed.sources, collection=command.collection, sizes=self._chunk_sizes)
            parsed = replace(parsed, sources=sources)
        load = self._build_load(resolved, parsed, validation, command.collection)
        state = self._build_state(resolved, parsed, validation, command.collection, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash)
        pruned = await self._load_stage(load, state, command.collection)

        outcome = await self._embed_stage.run(load.embed_inputs, collection=command.collection) if command.embed else None
        if outcome is not None:
            self._emit(EmbeddingsRefreshed(embedded=outcome.embedded, reused=outcome.reused, model=self._embedder.model_id))

        if command.summaries and self._summary_stage is not None:
            await self._summary_stage.run(command.collection)

        await self._switch_stage.run(collection=command.collection, spec=self._spec)

        report = IndexReport(
            pages=len(load.pages),
            sections=len(load.sections),
            claims=len(load.claims),
            edges=len(load.edges),
            pruned=pruned,
            embedded=outcome.embedded if outcome else 0,
            reused=outcome.reused if outcome else 0,
            embedding_tokens=outcome.input_tokens if outcome else 0,
            validation=validation,
            mode="full",
            duration_seconds=self._clock() - started,
            cut_plans=cut_plans,
        )
        await self._validation_report_sink.write(validation)
        self._emit(IndexingCompleted(report=report))
        return report

    async def _incremental_path(self, command: IndexKnowledgeBase, *, spec_hash: str, vocabulary_hash: str, started: float) -> IndexReport:
        """Reindex only the dirty set through `run_incremental` and emit the events its report describes."""
        deps = await self._incremental_deps(command.collection, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash)
        report = await run_incremental(command, deps=deps, chunk_sizes=self._chunk_sizes, cut_plan_stage=self._cut_plan_stage)

        self._emit(ValidationCompleted(n_errors=report.validation.n_errors, n_warnings=report.validation.n_warnings))
        if command.embed:
            self._emit(EmbeddingsRefreshed(embedded=report.embedded, reused=report.reused, model=self._embedder.model_id))

        if command.summaries and self._summary_stage is not None:
            await self._summary_stage.run(command.collection)

        await self._switch_stage.run(collection=command.collection, spec=self._spec)

        report = report.model_copy(update={"duration_seconds": self._clock() - started})
        await self._validation_report_sink.write(report.validation)
        self._emit(IndexingCompleted(report=report))
        return report

    async def _refuse_foreign_collection(self, command: IndexKnowledgeBase) -> None:
        """Refuse a collection another knowledge base root indexed, unless the command named it."""
        root = self._knowledge_base_path()
        if command.collection_named or not root:
            return
        async with self._uow_factory() as uow:
            fingerprint = await uow.fingerprint.get(command.collection)
            stored = fingerprint.knowledge_base_root if fingerprint is not None else ""
        if stored not in ("", root):
            raise CollectionIsForeignError(collection=command.collection, stored_root=stored, knowledge_base_root=root)

    async def _can_incremental(self, collection: str, spec_hash: str, vocabulary_hash: str) -> bool:
        """Whether the fingerprint matches, the knowledge base root is the same and the manifest is not empty."""
        async with self._uow_factory() as uow:
            fingerprint = await uow.fingerprint.get(collection)
            if fingerprint is None or not self._fingerprint_matches(fingerprint, spec_hash=spec_hash, vocabulary_hash=vocabulary_hash):
                return False
            if fingerprint.knowledge_base_root not in ("", self._knowledge_base_path()):
                return False
            return bool(await uow.manifest.load(collection))

    def _fingerprint_matches(self, stored: StoredFingerprint, *, spec_hash: str, vocabulary_hash: str) -> bool:
        """Whether the stored fingerprint row equals the current four inputs."""
        return (
            stored.indexer_version == self._indexer_version
            and stored.spec_hash == spec_hash
            and stored.vocabulary_hash == vocabulary_hash
            and stored.embed_model == self._embedder.model_id
        )

    async def _incremental_deps(self, collection: str, *, spec_hash: str, vocabulary_hash: str) -> IncrementalDeps:
        """Build the `IncrementalDeps` that `run_incremental` and the dry-run preview read."""
        clean = list(self._clean_page_view()) if self._clean_page_view is not None else await self._clean_pages(collection)
        return RunIncrementalDeps(
            uow_factory=self._uow_factory,
            kb_reader=self._kb_reader,
            embedder=self._embedder,
            spec=self._spec,
            indexer_version=self._indexer_version,
            spec_hash=spec_hash,
            vocabulary_hash=vocabulary_hash,
            clean_pages=tuple(clean),
            path_root=self._path_root,
            path_root_source=self._path_root_source,
        )

    async def _clean_pages(self, collection: str) -> list[CleanPage]:
        """Read each stored page's resolution keys with its manifest path, dropping pages that have no path."""
        async with self._uow_factory() as uow:
            manifest = await uow.manifest.load(collection)
            key_rows = await uow.pages.key_rows(collection)
        path_by_page = {entry.page_id: entry.path for entry in manifest.values() if entry.page_id}
        return [
            CleanPage(id=row.id, slug=row.slug, title=row.title, aliases=tuple(row.aliases), path=path, source_id=row.source_id)
            for row in key_rows
            if (path := path_by_page.get(row.id)) is not None
        ]

    def _parse_stage(self, collection: str) -> _ParsedKnowledgeBase:
        """Parse every note, turning a failure into a validation error, and read the mirrors and the vocabulary."""
        notes: list[KnowledgeBaseNote] = []
        parse_errors: list[ValidationIssue] = []
        note_texts: dict[str, str] = {}
        for document in self._kb_reader.unit_documents():
            note_texts[document.path] = document.text
            try:
                notes.append(parse_note(document.text, source_path=document.path, spec=self._spec))
            except Exception as exc:  # noqa: BLE001 (one bad note is a validation error, never an abort)
                _logger.warning("Failed to parse {path}: {reason}", path=str(document.path), reason=str(exc))
                parse_errors.append(
                    ValidationIssue(severity="error", rule="parse-error", page_id=document.path, message=f"failed to parse: {exc}")
                )

        read_mirrors = [MirrorDoc.from_text(doc.path, doc.text, collection) for doc in self._kb_reader.source_mirrors()]
        parse_errors.extend(mirror_provider_issues(read_mirrors))
        sources = loadable_mirrors(read_mirrors)
        vocabulary_docs = sorted((doc.path.rsplit("/", 1)[-1], doc.text) for doc in self._kb_reader.vocabulary_documents())
        vocabulary = parse_vocabulary(iter(vocabulary_docs)).index
        return _ParsedKnowledgeBase(
            notes=tuple(notes),
            sources=tuple(sources),
            vocabulary=vocabulary,
            parse_errors=tuple(parse_errors),
            about=self._read_about(),
            vocabulary_text="\n".join(text for _name, text in vocabulary_docs),
            note_texts=note_texts,
        )

    def _read_about(self) -> dict[str, str]:
        """One orientation text per audience from the `_meta/` orientation pages."""
        return read_orientation(self._kb_reader)

    def _validate_stage(self, resolved: ResolveResult, parsed: _ParsedKnowledgeBase) -> ValidationReport:
        """Validate the resolved notes, adding the parse failures and duplicate ids as errors."""
        mirror_texts = {src.source_key: src.text for src in parsed.sources}
        report = validate(
            (note for note in resolved.notes),
            parsed.vocabulary,
            mirror_texts=mirror_texts,
            resolve_result=resolved,
            spec=self._spec,
            path_root=self._path_root,
            path_root_source=self._path_root_source,
            knowledge_base_source_keys=frozenset(mirror_texts),
        )
        extra_errors = (*parsed.parse_errors, *duplicate_id_errors(resolved.notes))
        if not extra_errors:
            return report
        return report.model_copy(update={"errors": (*extra_errors, *report.errors)})

    def _build_load(self, resolved: ResolveResult, parsed: _ParsedKnowledgeBase, validation: ValidationReport, collection: str) -> LoadSet:
        """Every row the load upserts and every embed input, loading only the first note of a duplicate id."""
        del validation  # every parsed note loads, flagged or not
        builder = RowBuilder(
            collection,
            section_summary_spec=self._spec.section_summaries,
            active=frozenset(self._spec.active_embed_strategies()),
            facets=self._spec.facets,
            claim_grammar=self._spec.claims,
            # The full rebuild loads exactly the knowledge base's mirrors, so these are the only
            # source keys a page row may reference.
            known_source_keys=frozenset(src.source_key for src in parsed.sources),
            source_key_field=self._spec.sources.key_field,
            digest_link_field=self._spec.sources.digest_link_field,
            source_hash_field=self._spec.sources.hash_field,
            date_field=self._spec.retrieval.date_field,
            chunk_sizes=self._chunk_sizes,
        )
        seen_ids: set[str] = set()
        for resolved_note in resolved.notes:
            page_id = resolved_note.note.frontmatter.id
            if page_id in seen_ids:
                continue
            seen_ids.add(page_id)
            builder.add_note(resolved_note)
        for source in parsed.sources:
            builder.add_source(source)
        builder.add_about(parsed.about)
        return builder.build(parsed.vocabulary)

    def _build_state(
        self, resolved: ResolveResult, parsed: _ParsedKnowledgeBase, validation: ValidationReport, collection: str, *, spec_hash: str, vocabulary_hash: str
    ) -> _FullState:
        """The link refs, validation rows, manifest entries and fingerprint hashes the full load writes."""
        return _FullState(
            link_ref_rows=link_ref_rows(resolved, collection),
            validation_rows=[validation_row(issue, collection) for issue in (*validation.errors, *validation.warnings)],
            manifest_entries=_full_manifest_entries(resolved, parsed),
            spec_hash=spec_hash,
            vocabulary_hash=vocabulary_hash,
        )

    async def _load_stage(self, load: LoadSet, state: _FullState, collection: str) -> int:
        """Upsert, prune and record the load's events in one transaction scoped to `collection`, returning the rows pruned."""
        async with self._uow_factory() as uow:
            await uow.sources.upsert_sources(load.sources)
            await uow.pages.upsert_many(load.pages)
            await uow.sources.upsert_chunks(load.chunks)
            await uow.sections.upsert_many(load.sections)
            await uow.claims.upsert_many(load.claims)
            await uow.edges.replace_all(load.edges, collection=collection)
            # Re-project the derived link graph from the new edge set (AGE rebuilds kb_graph, CTE
            # does nothing).
            await uow.edges.refresh_graph()
            await uow.vocab.upsert_many(load.vocab)

            # A claim cites a page through its subject and its qualifiers with no cascade, so the kept
            # pages' qualifiers are replaced and the stale claims pruned before any page is deleted.
            page_ids = row_ids(load.pages)
            await uow.claim_qualifiers.replace_for_pages(load.claim_qualifiers, page_ids, collection=collection)

            # Prune one statement at a time on the one session and sum the rows each delete removed.
            # `edges.replace_all` is not counted.
            pruned = 0
            pruned += await uow.claims.prune_missing(row_ids(load.claims), collection=collection)
            pruned += await uow.pages.prune_missing(page_ids, collection=collection)
            pruned += await uow.sections.prune_missing(row_ids(load.sections), collection=collection)
            # A disclosure summary has no foreign key to its unit, so it is pruned against this
            # rebuild's pages and sections.
            pruned += await uow.summaries.prune_missing(row_ids(load.pages) | row_ids(load.sections), collection=collection)
            pruned += await uow.sources.prune_missing_sources({id_str(r["source_id"]) for r in load.sources}, collection=collection)
            pruned += await uow.sources.prune_missing_chunks(row_ids(load.chunks), collection=collection)
            pruned += await uow.vocab.prune_missing({(id_str(r["vocab"]), id_str(r["value"])) for r in load.vocab}, collection=collection)

            # The manifest, link refs, validation rows and fingerprint commit with the rows they
            # describe. The validation store is replaced for the whole collection, and a manifest row
            # for a path this rebuild did not read is deleted, so no stale row outlives a full rebuild.
            await uow.link_refs.replace_for_sources(state.link_ref_rows, page_ids, collection=collection)
            await uow.validation_store.replace_all(state.validation_rows, collection=collection)
            # The stored manifest is read before the upsert below. Read after it, every full index lists no changed pages.
            stored_manifest = await uow.manifest.load(collection)
            starting_manifest = stored_manifest if self._is_own_root(await uow.fingerprint.get(collection)) else {}
            await uow.manifest.upsert_many(manifest_rows(state.manifest_entries, collection), collection=collection)
            indexed_paths = {entry.path for entry in state.manifest_entries}
            await uow.manifest.delete_paths(set(stored_manifest) - indexed_paths, collection=collection)
            await uow.fingerprint.set(
                collection=collection,
                indexer_version=self._indexer_version,
                spec_hash=state.spec_hash,
                vocabulary_hash=state.vocabulary_hash,
                embed_model=self._embedder.model_id,
                knowledge_base_root=self._knowledge_base_path(),
            )
            await uow.page_type_contracts.upsert_many(page_type_contract_rows(self._spec, collection))
            await uow.page_type_contracts.prune_missing({pt.page_type for pt in self._spec.page_types}, collection=collection)

            # The events commit in the same transaction as the rows they describe.
            uow.record(
                PagesLoaded(
                    collection=collection,
                    pages=len(load.pages),
                    sections=len(load.sections),
                    claims=len(load.claims),
                    edges=len(load.edges),
                    pruned=pruned,
                    page_ids=changed_page_ids(starting_manifest, state.manifest_entries),
                ),
                CitingPagesChanged(
                    collection=collection, page_ids=digest_page_ids(load.pages, frozenset(self._spec.sources.digest_types.values()))
                ),
            )
            await uow.commit()
        return pruned

    def _knowledge_base_path(self) -> str:
        """The knowledge base root as a string."""
        return str(self._kb_reader.root)

    def _is_own_root(self, fingerprint: StoredFingerprint | None) -> bool:
        """Whether the stored fingerprint names this knowledge base root, or no root at all."""
        return fingerprint is None or fingerprint.knowledge_base_root in ("", self._knowledge_base_path())

    def _emit(self, event: Event) -> None:
        """Append an event for the bus to drain."""
        self.events.append(event)


def _full_manifest_entries(resolved: ResolveResult, parsed: _ParsedKnowledgeBase) -> list[ManifestEntry]:
    """One manifest entry per indexed note and source mirror, the first note of a duplicate id winning."""
    entries: list[ManifestEntry] = []
    seen_ids: set[str] = set()
    for resolved_note in resolved.notes:
        note = resolved_note.note
        page_id = note.frontmatter.id
        if page_id in seen_ids:
            continue
        seen_ids.add(page_id)
        stem = note.source_path.rsplit("/", 1)[-1].removesuffix(".md")
        entries.append(
            ManifestEntry(
                path=note.source_path,
                kind=FileType.NOTE,
                content_hash=card.content_hash(parsed.note_texts.get(note.source_path, "")),
                identity_hash=identity_hash(stem=stem, id_=page_id, title=note.frontmatter.title, aliases=note.frontmatter.aliases),
                page_id=page_id,
                source_key=None,
            )
        )
    entries.extend(
        ManifestEntry(
            path=source.path,
            kind=FileType.SOURCE,
            content_hash=card.content_hash(source.text),
            identity_hash=card.content_hash(source.source_key),
            page_id=None,
            source_key=source.source_key,
        )
        for source in parsed.sources
    )
    return entries


@dataclass(frozen=True, slots=True)
class _ParsedKnowledgeBase:
    """The parsed notes, mirrors, vocabulary, parse errors, orientation texts and raw texts of one knowledge base."""

    notes: tuple[KnowledgeBaseNote, ...]
    sources: tuple[MirrorDoc, ...]
    vocabulary: VocabularyIndex
    parse_errors: tuple[ValidationIssue, ...]
    about: Mapping[str, str]
    """One orientation text per audience."""
    vocabulary_text: str
    note_texts: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _FullState:
    """The link refs, validation rows, manifest entries and fingerprint hashes a full load writes."""

    link_ref_rows: list[Row]
    validation_rows: list[Row]
    manifest_entries: list[ManifestEntry]
    spec_hash: str
    vocabulary_hash: str
