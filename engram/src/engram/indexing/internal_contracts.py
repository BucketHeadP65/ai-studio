"""The indexing slice's own ports: the knowledge base reader, the units of work and the stage collaborators.

Every port is a structural `Protocol`, so an adapter or a fake satisfies it without subclassing.
Each stage's port names only the repository methods that stage calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

from core.cqrs.contracts import UnitOfWork

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder
    from core.ai.retrieval.contracts import Row
    from core.cqrs.contracts import Event

    from engram.indexing.domain.manifest import ManifestEntry
    from engram.indexing.domain.models import RawDocument, ValidationReport
    from engram.indexing.domain.summary_units import SummaryUnit, SummaryUpsertRow
    from engram.indexing.domain.switches import RetrievalSwitches, Signals
    from engram.indexing.infrastructure.repository import (
        ClaimQualifiers,
        Claims,
        Edges,
        EmbedMeta,
        IndexFingerprints,
        LinkRefs,
        ManifestEntries,
        Pages,
        PageTypeContracts,
        Sections,
        SignalsQueries,
        SourceRepository,
        UnitSummaries,
        ValidationIssues,
        VocabEntries,
    )
    from engram.indexing.read_models import CleanPage, PageKeyRowResult, PendingEmbedChunk, PendingEmbedRow, StoredFingerprint
    from engram.kb.contracts import KnowledgeBaseSchema


@runtime_checkable
class KnowledgeBaseReader(Protocol):
    """Reads the knowledge base's markdown files for the parse stage.

    It only reads. `meta_document` fetches one named `_meta/` page for the `api.about` content row.
    """

    @property
    def root(self) -> Path:
        """The knowledge base root this reader walks."""
        ...

    def unit_documents(self) -> Iterator[RawDocument]:
        """Yield every note document: the pages under the declared note folders, and the declared note files.

        A file reached more than once is yielded once, and a file opening with the generated banner is never yielded.

        Returns:
            An iterator over the page documents, in knowledge-base-relative path order.
        """
        ...

    def source_mirrors(self) -> Iterator[RawDocument]:
        """Yield every `Sources/*.md` source mirror document.

        Returns:
            An iterator over the source mirror documents.
        """
        ...

    def vocabulary_documents(self) -> Iterator[RawDocument]:
        """Yield every `_meta/vocabulary/*.md` vocabulary document.

        Returns:
            An iterator over the vocabulary documents.
        """
        ...

    def meta_document(self, name: str) -> RawDocument:
        """Fetch one `_meta/<name>.md` page.

        Args:
            name: The page name without its extension, such as `purpose`.

        Returns:
            The page document.

        Raises:
            FileNotFoundError: When the page does not exist.
        """
        ...


@runtime_checkable
class KnowledgeBaseUnitOfWork(UnitOfWork, Protocol):
    """Transaction for a knowledge base rebuild, with every indexing repository as an attribute.

    It carries the repositories the load stage writes, the summary repository it reconciles,
    the index state repositories the incremental path drives and the read-only signal repository.
    """

    pages: Pages
    sections: Sections
    claims: Claims
    claim_qualifiers: ClaimQualifiers
    edges: Edges
    sources: SourceRepository
    vocab: VocabEntries
    summaries: UnitSummaries
    meta: EmbedMeta
    manifest: ManifestEntries
    link_refs: LinkRefs
    validation_store: ValidationIssues
    fingerprint: IndexFingerprints
    page_type_contracts: PageTypeContracts
    signals: SignalsQueries


class ValidationReportSink(Protocol):
    """Writes the index validation report into the knowledge base's `_meta/dashboard.md` page."""

    async def write(self, report: ValidationReport) -> None:
        """Replace the validation block of `_meta/dashboard.md` with `report`.

        Args:
            report: The validation report to write.
        """
        ...


# -- the full rebuild's unit of work port ---------------------------------------


class _IdRepo(Protocol):
    """A repository keyed by one `id` (pages, sections, claims) with the whole-set prune and the embed methods."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...
    async def prune_missing(self, keep_ids: set[str], *, collection: str) -> int: ...
    async def rows_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedRow]: ...
    async def write_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class _PageChildRepo(_IdRepo, Protocol):
    """A page-owned repository (sections, claims) with the whole-set prune and the per-page prune."""

    async def prune_for_pages(self, keep_ids: set[str], page_ids: set[str], *, collection: str) -> int: ...


class _PageRepo(_IdRepo, Protocol):
    """The `kb.pages` repository with the delete by id and the committed pages' resolution keys."""

    async def delete_by_ids(self, ids: set[str], *, collection: str) -> None: ...
    async def key_rows(self, collection: str) -> Sequence[PageKeyRowResult]: ...


class _EdgeRepo(Protocol):
    """The edge repository with the whole replace, the per-source replace and the graph refresh."""

    async def replace_all(self, rows: Sequence[Row], *, collection: str) -> None: ...
    async def replace_for_sources(self, rows: Sequence[Row], src_page_ids: set[str], *, collection: str) -> int: ...
    async def refresh_graph(self) -> None: ...


class _SummaryRepo(Protocol):
    """The `kb.unit_summaries` repository, which the load prunes and never writes."""

    async def prune_missing(self, keep_unit_ids: set[str], *, collection: str) -> int: ...
    async def prune_for_pages(
        self, keep_section_ids: set[str], dirty_page_ids: set[str], deleted_page_ids: set[str], *, collection: str
    ) -> int: ...


class _VocabRepo(Protocol):
    """The vocab aggregate (composite `(collection, vocab, value)` key), pruned scoped to `collection`."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...
    async def prune_missing(self, keep: set[tuple[str, str]], *, collection: str) -> int: ...


class _SourceRepo(Protocol):
    """The `kb.sources_raw` and `kb.raw_chunks` repository with the prunes and the delete by key."""

    async def upsert_sources(self, rows: Sequence[Row]) -> None: ...
    async def upsert_chunks(self, rows: Sequence[Row]) -> None: ...
    async def prune_missing_sources(self, keep_keys: set[str], *, collection: str) -> int: ...
    async def prune_missing_chunks(self, keep_ids: set[str], *, collection: str) -> int: ...
    async def delete_sources_by_keys(self, source_ids: set[str], *, collection: str) -> None: ...
    async def chunks_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedChunk]: ...
    async def write_chunk_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class _MetaRepo(Protocol):
    """The single embed-meta contract row."""

    async def set_embed_meta(self, *, model: str, dim: int) -> None: ...


class _ManifestRepo(Protocol):
    """The per-collection index manifest, whose `load` maps each path to its row from the last index."""

    async def load(self, collection: str) -> Mapping[str, ManifestEntry]: ...
    async def upsert_many(self, entries: Sequence[Row], *, collection: str) -> None: ...
    async def delete_paths(self, paths: set[str], *, collection: str) -> None: ...


class _LinkRefRepo(Protocol):
    """The persisted outbound-link graph (resolved + dangling) powering the inbound closure."""

    async def src_pages_referencing(self, names: set[str], *, collection: str) -> set[str]: ...
    async def src_pages_pointing_at(self, dst_ids: set[str], *, collection: str) -> set[str]: ...
    async def replace_for_sources(self, rows: Sequence[Row], src_ids: set[str], *, collection: str) -> None: ...


class _ValidationStoreRepo(Protocol):
    """The persisted per-page validation issues for whole-knowledge-base report reassembly."""

    async def replace_all(self, issues: Sequence[Row], *, collection: str) -> None: ...
    async def replace_for_pages(self, issues: Sequence[Row], page_ids: set[str], *, collection: str) -> None: ...
    async def whole_report(self, collection: str) -> ValidationReport: ...


class _ClaimQualifierRepo(Protocol):
    """The `kb.claim_qualifiers` repository: replaced per owning page (delete + insert)."""

    async def replace_for_pages(self, rows: Sequence[Row], page_ids: set[str], *, collection: str) -> None: ...


class _FingerprintRepo(Protocol):
    """The single per-collection global-invalidator row (`get` / `set`), plus its retrieval switches."""

    async def get(self, collection: str) -> StoredFingerprint | None: ...
    async def set(
        self,
        *,
        collection: str,
        indexer_version: str,
        spec_hash: str,
        vocabulary_hash: str,
        embed_model: str,
        knowledge_base_root: str,
        spec: KnowledgeBaseSchema,
    ) -> None: ...
    async def touch(self, collection: str, *, knowledge_base_root: str, spec: KnowledgeBaseSchema) -> None: ...
    async def get_switches(self, collection: str) -> RetrievalSwitches: ...
    async def set_switches(self, *, collection: str, switches: RetrievalSwitches) -> None: ...


class _SignalsRepo(Protocol):
    """The knowledge base shape counts the retrieval mode decision reads."""

    async def measure(self, collection: str) -> Signals: ...


class _PageTypeContractRepo(Protocol):
    """The `kb.page_type_contracts` repository: upsert by `(collection, page_type)`, prune to the current page_types."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...
    async def prune_missing(self, keep: set[str], *, collection: str) -> int: ...


class IndexUnitOfWork(Protocol):
    """The transaction the full rebuild drives, with the repositories the load and embed stages touch.

    The repositories are read-only properties, so any repository with at least these methods is accepted.
    """

    @property
    def pages(self) -> _PageRepo:
        """The `kb.pages` repository (id-keyed aggregate + the clean-page key view)."""
        ...

    @property
    def sections(self) -> _PageChildRepo:
        """The `kb.sections` repository."""
        ...

    @property
    def claims(self) -> _PageChildRepo:
        """The `kb.claims` repository."""
        ...

    @property
    def claim_qualifiers(self) -> _ClaimQualifierRepo:
        """The `kb.claim_qualifiers` repository."""
        ...

    @property
    def edges(self) -> _EdgeRepo:
        """The `kb.edges` repository."""
        ...

    @property
    def sources(self) -> _SourceRepo:
        """The `kb.sources_raw` + `kb.raw_chunks` repository."""
        ...

    @property
    def vocab(self) -> _VocabRepo:
        """The `kb.vocab` repository."""
        ...

    @property
    def summaries(self) -> _SummaryRepo:
        """The `kb.unit_summaries` repository (the disclosure summaries the load reconciles)."""
        ...

    @property
    def meta(self) -> _MetaRepo:
        """The `kb.embed_meta` repository."""
        ...

    @property
    def manifest(self) -> _ManifestRepo:
        """The `kb.index_manifest` repository (the change detector)."""
        ...

    @property
    def link_refs(self) -> _LinkRefRepo:
        """The `kb.link_refs` repository (the inbound-closure graph)."""
        ...

    @property
    def validation_store(self) -> _ValidationStoreRepo:
        """The `kb.validation_store` repository (the persisted per-page validation issues)."""
        ...

    @property
    def fingerprint(self) -> _FingerprintRepo:
        """The `kb.index_fingerprint` repository (the global-invalidator row)."""
        ...

    @property
    def signals(self) -> _SignalsRepo:
        """The knowledge base shape counts the retrieval mode decision reads."""
        ...

    @property
    def page_type_contracts(self) -> _PageTypeContractRepo:
        """The `kb.page_type_contracts` repository (the indexed per-page_type writing contract)."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the rebuild transaction."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Roll back unless `commit` ran, then release the session."""
        ...

    async def commit(self) -> None:
        """Persist the transaction."""
        ...

    def record(self, *events: Event) -> None:
        """Record events to be written to the outbox when this scope commits.

        Args:
            *events: The events to write.
        """
        ...


# -- the embed stage's unit of work port --


class _EmbeddedRepo(Protocol):
    """An embedded repository keyed by one `id`: the rows with no vector yet, and the vector write."""

    async def rows_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedRow]: ...
    async def write_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class _ChunkRepo(Protocol):
    """The raw-chunk aggregate: its NULL queue + the chunk-specific vector write."""

    async def chunks_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedChunk]: ...
    async def write_chunk_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class EmbedUnitOfWork(Protocol):
    """The transaction the embed stage drives: the four embedded repositories, the embed meta and the commit.

    `IndexUnitOfWork` and `IncrementalUnitOfWork` both satisfy it, and so does a factory of either.
    """

    @property
    def pages(self) -> _EmbeddedRepo:
        """The `kb.pages` repository."""
        ...

    @property
    def sections(self) -> _EmbeddedRepo:
        """The `kb.sections` repository."""
        ...

    @property
    def claims(self) -> _EmbeddedRepo:
        """The `kb.claims` repository."""
        ...

    @property
    def sources(self) -> _ChunkRepo:
        """The `kb.sources_raw` + `kb.raw_chunks` repository."""
        ...

    @property
    def meta(self) -> _MetaRepo:
        """The `kb.embed_meta` repository."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the short embed transaction."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Roll back unless `commit` ran, then release the session."""
        ...

    async def commit(self) -> None:
        """Persist the transaction."""
        ...


# -- the single page write's unit of work port ---------------------------------


class _UpsertRepo(_EmbeddedRepo, Protocol):
    """An embedded aggregate this path upserts into (pages / sections / claims)."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...


class _IngestPageRepo(_UpsertRepo, Protocol):
    """`kb.pages` with the upsert and the stored pages' resolution keys."""

    async def key_rows(self, collection: str) -> Sequence[PageKeyRowResult]: ...


class _IngestEdgeRepo(Protocol):
    """`kb.edges`: replace this page's outbound edges, and re-project the graph when they changed."""

    async def replace_for_sources(self, rows: Sequence[Row], src_page_ids: set[str], *, collection: str) -> int: ...
    async def refresh_graph(self) -> None: ...


class _IngestLinkRefRepo(Protocol):
    """`kb.link_refs`: replace this page's outbound link targets, resolved and dangling alike."""

    async def replace_for_sources(self, rows: Sequence[Row], src_ids: set[str], *, collection: str) -> None: ...


class _IngestManifestRepo(Protocol):
    """`kb.index_manifest`, read only to refuse a collection a knowledge base indexes."""

    async def load(self, collection: str) -> Mapping[str, ManifestEntry]: ...


class _IngestFingerprintRepo(Protocol):
    """`kb.index_fingerprint`'s switches half: the decision a `mode: "auto"` write stores and its embed veto."""

    async def get_switches(self, collection: str) -> RetrievalSwitches:
        """Return `collection`'s persisted switches, all-off before any switch has ever been written."""
        ...

    async def set_switches(self, *, collection: str, switches: RetrievalSwitches) -> None:
        """Persist `collection`'s decided switches, creating its fingerprint row when it has none."""
        ...


class IngestUnitOfWork(EmbedUnitOfWork, Protocol):
    """The transaction a single page write drives: the embed repositories plus the page writes.

    `pages`, `sections` and `claims` add the upsert. It has no source, cell, vocabulary or prune repository.
    Of the fingerprint it reads and writes only the retrieval switches, and `signals` measures what decides them.
    """

    @property
    def pages(self) -> _IngestPageRepo:
        """The `kb.pages` repository."""
        ...

    @property
    def sections(self) -> _UpsertRepo:
        """The `kb.sections` repository."""
        ...

    @property
    def claims(self) -> _UpsertRepo:
        """The `kb.claims` repository."""
        ...

    @property
    def claim_qualifiers(self) -> _ClaimQualifierRepo:
        """The `kb.claim_qualifiers` repository."""
        ...

    @property
    def edges(self) -> _IngestEdgeRepo:
        """The `kb.edges` repository."""
        ...

    @property
    def link_refs(self) -> _IngestLinkRefRepo:
        """The `kb.link_refs` repository."""
        ...

    @property
    def manifest(self) -> _IngestManifestRepo:
        """The `kb.index_manifest` repository (read, to refuse an indexed collection)."""
        ...

    @property
    def fingerprint(self) -> _IngestFingerprintRepo:
        """The `kb.index_fingerprint` repository's switches half."""
        ...

    @property
    def signals(self) -> _SwitchSignalsRepo:
        """The knowledge base shape counts."""
        ...


# -- the incremental path's unit of work port -----------------------------------


class _IncIdRepo(Protocol):
    """A single-`id`-keyed aggregate (pages/sections/claims) with the targeted delete."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...
    async def delete_by_ids(self, ids: set[str], *, collection: str) -> None: ...
    async def rows_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedRow]: ...
    async def write_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class _IncEmbeddedRepo(Protocol):
    """A single-`id`-keyed embedded aggregate the incremental path only upserts + re-embeds."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...
    async def rows_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedRow]: ...
    async def write_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class _IncPageChildRepo(_IncEmbeddedRepo, Protocol):
    """A page-owned embedded repository (sections, claims) the incremental path upserts, then prunes per page."""

    async def prune_for_pages(self, keep_ids: set[str], page_ids: set[str], *, collection: str) -> int: ...


class _IncEdgeRepo(Protocol):
    """The edge repository, replaced per source, plus the graph refresh."""

    async def replace_for_sources(self, rows: Sequence[Row], src_page_ids: set[str], *, collection: str) -> int: ...
    async def refresh_graph(self) -> None: ...


class _IncSummaryRepo(Protocol):
    """The disclosure summaries, reconciled against the rebuilt pages' units (never written here)."""

    async def prune_for_pages(
        self, keep_section_ids: set[str], dirty_page_ids: set[str], deleted_page_ids: set[str], *, collection: str
    ) -> int: ...


class _IncSourceRepo(Protocol):
    """The sources_raw + raw_chunks aggregate with the targeted source delete (cascades chunks)."""

    async def upsert_sources(self, rows: Sequence[Row]) -> None: ...
    async def upsert_chunks(self, rows: Sequence[Row]) -> None: ...
    async def delete_sources_by_keys(self, source_ids: set[str], *, collection: str) -> None: ...
    async def chunks_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedChunk]: ...
    async def write_chunk_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None: ...


class _IncLinkRefRepo(Protocol):
    """The persisted outbound-link graph powering the inbound closure (name + id queries)."""

    async def src_pages_referencing(self, names: set[str], *, collection: str) -> set[str]: ...
    async def src_pages_pointing_at(self, dst_ids: set[str], *, collection: str) -> set[str]: ...
    async def replace_for_sources(self, rows: Sequence[Row], src_ids: set[str], *, collection: str) -> None: ...


class _IncValidationStoreRepo(Protocol):
    """The persisted per-page validation issues (per-page replacement and the whole collection read back)."""

    async def replace_for_pages(self, issues: Sequence[Row], page_ids: set[str], *, collection: str) -> None: ...
    async def whole_report(self, collection: str) -> ValidationReport: ...


class _IncFingerprintRepo(Protocol):
    """The single per-collection global-invalidator row."""

    async def get(self, collection: str) -> StoredFingerprint | None: ...
    async def touch(self, collection: str, *, knowledge_base_root: str, spec: KnowledgeBaseSchema) -> None: ...


class IncrementalUnitOfWork(Protocol):
    """The transaction the incremental path drives, with the repositories its load touches.

    It adds the deletes by id and by key and the per-source edge replace.
    The repositories are read-only properties, so any repository with at least these methods is accepted.
    """

    @property
    def pages(self) -> _IncIdRepo:
        """The `kb.pages` repository (upsert + targeted delete-by-id)."""
        ...

    @property
    def sections(self) -> _IncPageChildRepo:
        """The `kb.sections` repository."""
        ...

    @property
    def claims(self) -> _IncPageChildRepo:
        """The `kb.claims` repository."""
        ...

    @property
    def claim_qualifiers(self) -> _ClaimQualifierRepo:
        """The `kb.claim_qualifiers` repository."""
        ...

    @property
    def edges(self) -> _IncEdgeRepo:
        """The `kb.edges` repository (per-source replacement + gated refresh)."""
        ...

    @property
    def sources(self) -> _IncSourceRepo:
        """The `kb.sources_raw` + `kb.raw_chunks` repository (upsert + targeted delete)."""
        ...

    @property
    def summaries(self) -> _IncSummaryRepo:
        """The `kb.unit_summaries` repository (the disclosure summaries the load reconciles)."""
        ...

    @property
    def meta(self) -> _MetaRepo:
        """The `kb.embed_meta` repository."""
        ...

    @property
    def manifest(self) -> _ManifestRepo:
        """The `kb.index_manifest` repository (the change detector)."""
        ...

    @property
    def link_refs(self) -> _IncLinkRefRepo:
        """The `kb.link_refs` repository (the inbound-closure graph)."""
        ...

    @property
    def validation_store(self) -> _IncValidationStoreRepo:
        """The `kb.validation_store` repository (the persisted per-page validation issues)."""
        ...

    @property
    def fingerprint(self) -> _IncFingerprintRepo:
        """The `kb.index_fingerprint` repository (the global-invalidator row)."""
        ...

    @property
    def page_type_contracts(self) -> _PageTypeContractRepo:
        """The `kb.page_type_contracts` repository (the indexed per-page_type writing contract)."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the load transaction."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Roll back unless `commit` ran, then release the session."""
        ...

    async def commit(self) -> None:
        """Persist the transaction."""
        ...

    def record(self, *events: Event) -> None:
        """Record events to be written to the outbox when this scope commits.

        Args:
            *events: The events to write.
        """
        ...


class IncrementalDeps(Protocol):
    """What `run_incremental` needs: the unit of work factory, the reader, the embedder, the schema and the fingerprint inputs.

    The embed model in the fingerprint is the embedder's `model_id`.
    """

    @property
    def uow_factory(self) -> Callable[[], IncrementalUnitOfWork]:
        """Builds a fresh `IncrementalUnitOfWork` per transaction (load, then per embed batch)."""
        ...

    @property
    def kb_reader(self) -> KnowledgeBaseReader:
        """Reads the knowledge base's markdown surfaces (notes, mirrors, vocabulary)."""
        ...

    @property
    def embedder(self) -> BatchEmbedder:
        """Embeds batches of text and names the model recorded in `kb.embed_meta`."""
        ...

    @property
    def spec(self) -> KnowledgeBaseSchema:
        """The knowledge base's schema, which governs parse and validate."""
        ...

    @property
    def path_root(self) -> Path | None:
        """The root the validate stage's `dangling-code-path` check resolves `path_fields` against.

        `None` skips that check.
        """
        ...

    @property
    def path_root_source(self) -> str | None:
        """Where `path_root` came from (a CLI flag or a settings file), folded into its message.

        `None` (the default) omits that half of the `dangling-code-path` message.
        """
        ...

    @property
    def indexer_version(self) -> str:
        """This indexer's version (a fingerprint input)."""
        ...

    @property
    def spec_hash(self) -> str:
        """The hash of the active spec (a fingerprint input)."""
        ...

    @property
    def vocabulary_hash(self) -> str:
        """The hash of the vocabulary files (a fingerprint input)."""
        ...

    def clean_page_view(self) -> Sequence[CleanPage]:
        """Return the committed `kb.pages` key rows.

        Returns:
            The key rows of every committed page.
        """
        ...


class _SwitchFingerprintRepo(Protocol):
    """The fingerprint row's switches half: the decision's current state in, the decided state out."""

    async def get_switches(self, collection: str) -> RetrievalSwitches:
        """Return `collection`'s persisted switches, all-off before any switch write."""
        ...

    async def set_switches(self, *, collection: str, switches: RetrievalSwitches) -> None:
        """Persist `collection`'s decided switches."""
        ...


class _SwitchSignalsRepo(Protocol):
    """The knowledge base shape counts the retrieval mode decision compares against its thresholds."""

    async def measure(self, collection: str) -> Signals:
        """Count `collection`'s notes, page_types, token estimate, edges-per-note, and giant-component share."""
        ...


class ReaderScopes(Protocol):
    """Gives a private knowledge base its own reader role, and takes the scope away once it is shared."""

    async def scope(self, collection: str, *, private: bool) -> None:
        """Give `collection` its reader role when `private`, or remove its scope when it is not.

        Every call is idempotent.

        Args:
            collection: The knowledge base's collection.
            private: Whether the knowledge base declares itself private.
        """
        ...


class SwitchUnitOfWork(Protocol):
    """The transaction `SwitchStage` drives: the fingerprint's retrieval switches and the signal counts."""

    @property
    def fingerprint(self) -> _SwitchFingerprintRepo:
        """The `kb.index_fingerprint` repository's switches half."""
        ...

    @property
    def signals(self) -> _SwitchSignalsRepo:
        """The knowledge base shape counts."""
        ...

    async def __aenter__(self) -> Self:
        """Begin a short transaction."""
        ...

    async def __aexit__(self, *exc: object) -> None:
        """Roll back unless `commit` ran, then release the session."""
        ...

    async def commit(self) -> None:
        """Persist the transaction."""
        ...


@runtime_checkable
class Summarizer(Protocol):
    """The disclosure-summary generator port: names its model, summarizes one unit at a time."""

    @property
    def model_id(self) -> str:
        """The identifier recorded (provenance) on every summary this summarizer writes."""
        ...

    async def summarize(self, *, title: str, body: str) -> str:
        """Return a prose summary of `body`, using `title` as context.

        Args:
            title: The unit's title.
            body: The text to summarize.

        Returns:
            The summary.
        """
        ...


@runtime_checkable
class ChunkPlanner(Protocol):
    """The model that chooses where a heading section of a mirror changes topic, answering paragraph numbers only."""

    @property
    def identity(self) -> str:
        """What decides the planner's answers: its model name, prompt version and thinking level, named by each cut plan it chooses and by the index fingerprint."""
        ...

    async def chunk_starts(self, paragraphs: Sequence[str]) -> Sequence[int]:
        """Answer the numbers of the paragraphs where a new chunk starts, paragraphs numbered from 1 by their place.

        The caller refuses an answer that is not strictly increasing paragraph numbers and fills the text instead.
        A call may fail or run late, and the caller then fills the text.

        Args:
            paragraphs: The text's paragraphs in reading order.

        Returns:
            The paragraph numbers where a new chunk starts, in increasing order.
        """
        ...


class CutPlans(Protocol):
    """The write side of the stored cut plans: plans are added and never changed."""

    async def insert(self, collection: str, planner: str, plans: Mapping[str, tuple[int, ...]]) -> None:
        """Store cut plans in one transaction of their own, keeping any plan already stored under the same key.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            plans: The paragraph numbers where a new chunk starts, by the content hash of the text each cuts.
        """
        ...


class CutPlanQueries(Protocol):
    """The read side of the stored cut plans."""

    async def cut_plans(self, collection: str, planner: str, text_hashes: Collection[str]) -> dict[str, tuple[int, ...]]:
        """The stored cut plans of one knowledge base and one chunk planner for the text hashes asked.

        Args:
            collection: The knowledge base the plans belong to.
            planner: The cut plan identity (`cut_plan_identity`) the plans are stored under.
            text_hashes: The content hashes of the texts asked for.

        Returns:
            The paragraph numbers by text hash, leaving out each hash with no stored plan.
        """
        ...


class SummaryUnitOfWork(Protocol):
    """The transaction `SummaryStage` drives: read the pending units, write rows, commit once.

    It is no context manager. One instance serves exactly one `run()` call.
    """

    async def pending_units(self, collection: str) -> Sequence[SummaryUnit]:
        """Return the collection's pages and sections with no `active` or `pending` summary.

        Args:
            collection: The collection to read.

        Returns:
            The units waiting for a summary.
        """
        ...

    async def upsert_summary(self, row: SummaryUpsertRow) -> None:
        """Upsert one `kb.unit_summaries` row by `unit_id`, flushing but not committing.

        Args:
            row: The summary row to write.
        """
        ...

    async def commit(self) -> None:
        """Persist every `upsert_summary` call made through this UoW."""
        ...
