"""In-memory `UnitOfWork` stand-ins for unit tests.

The bare CQRS unit of work fake is `core.testing.fakes.FakeUnitOfWork`. This module holds
:class:`FakeKnowledgeBaseUnitOfWork`, the indexing slice's unit of work with every repository as a
recording attribute, and :class:`FakeValidationReportSink`.

Each recording repo captures the rows upserted, the id sets pruned and the embeddings written, and hands
back scripted state such as `needing`, the embed-stage work queue. Every prune, replace and
embedding write records its mandatory `collection` keyword next to what it received.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from core.cqrs.contracts import CoordinationBuffer

from engram.indexing.contracts import RetrievalSwitches, Signals, ValidationReport

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from core.ai.retrieval.contracts import Row
    from core.cqrs.contracts import Event

    from engram.indexing.contracts import ManifestEntry, PageKeyRowResult, PendingEmbedChunk, PendingEmbedRow, StoredFingerprint

# The ordered log of the load operations whose relative order is part of the write contract.
type _CallTrace = list[str]


class _Tracer:
    """Appends `"<repo>.<method>"` to a shared call log, or to its own log when none is given."""

    def __init__(self, calls: _CallTrace | None, name: str) -> None:
        self._calls: _CallTrace = calls if calls is not None else []
        self._name = name

    def __call__(self, method: str) -> None:
        self._calls.append(f"{self._name}.{method}")


class _RecordingRepo:
    """Records upserts, prunes and embedding writes over the scripted `existing`, `page_of` and `needing`."""

    def __init__(self, needing: Sequence[PendingEmbedRow] = (), *, calls: _CallTrace | None = None, name: str = "") -> None:
        self.upserts: list[list[Row]] = []
        self.pruned: list[set[str]] = []
        self.pruned_collections: list[str] = []
        self.pruned_for_pages: list[tuple[set[str], set[str]]] = []
        self.pruned_for_pages_collections: list[str] = []
        self.deleted_ids: list[set[str]] = []
        self.deleted_ids_collections: list[str] = []
        self.embedded: list[tuple[str, Sequence[float]]] = []
        self.embedded_collections: list[str] = []
        self.needing: list[PendingEmbedRow] = list(needing)
        self.existing: set[str] = set()
        self.page_of: dict[str, str] = {}
        self.key_row_results: list[PageKeyRowResult] = []
        self._trace = _Tracer(calls, name)

    async def upsert_many(self, rows: Sequence[Row]) -> None:
        self._trace("upsert_many")
        self.upserts.append(list(rows))

    async def key_rows(self, collection: str) -> Sequence[PageKeyRowResult]:
        del collection  # single-collection fake: the scripted rows are returned verbatim
        return list(self.key_row_results)

    async def prune_missing(self, keep_ids: set[str], *, collection: str) -> int:
        self.pruned.append(set(keep_ids))
        self.pruned_collections.append(collection)
        deleted = self.existing - keep_ids
        self.existing &= keep_ids
        return len(deleted)

    async def prune_for_pages(self, keep_ids: set[str], page_ids: set[str], *, collection: str) -> int:
        self._trace("prune_for_pages")
        self.pruned_for_pages.append((set(keep_ids), set(page_ids)))
        self.pruned_for_pages_collections.append(collection)
        if not page_ids:  # mirror the real repo's empty-page-set early return
            return 0
        deleted = {row_id for row_id in self.existing if self.page_of.get(row_id) in page_ids and row_id not in keep_ids}
        self.existing -= deleted
        for row_id in deleted:
            self.page_of.pop(row_id, None)
        return len(deleted)

    async def delete_by_ids(self, ids: set[str], *, collection: str) -> int:
        self._trace("delete_by_ids")
        if not ids:  # mirror the real repo's empty-set early return (records no call)
            return 0
        self.deleted_ids.append(set(ids))
        self.deleted_ids_collections.append(collection)
        deleted = self.existing & ids
        self.existing -= ids
        return len(deleted)

    async def rows_needing_embedding(self, *, collection: str = "minimal") -> Sequence[PendingEmbedRow]:
        del collection  # the scripted queue plays every scoped read the same way
        return list(self.needing)

    async def write_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None:
        self.embedded.extend(vectors.items())
        self.embedded_collections.append(collection)

    @property
    def upserted_rows(self) -> list[Row]:
        """Every row upserted across all batches, flattened."""
        return [row for batch in self.upserts for row in batch]


class _RecordingEdgeRepo:
    """Records full and per-source edge replacements, counting deletes against the scripted `existing_src_ids`."""

    def __init__(self) -> None:
        self.replaced: list[list[Row]] = []
        self.replaced_collections: list[str] = []
        self.replaced_src_ids: list[set[str]] = []
        self.replaced_for_sources_collections: list[str] = []
        self.existing_src_ids: set[str] = set()
        self.refreshed = 0

    async def replace_all(self, rows: Sequence[Row], *, collection: str) -> None:
        self.replaced.append(list(rows))
        self.replaced_collections.append(collection)

    async def replace_for_sources(self, rows: Sequence[Row], src_page_ids: set[str], *, collection: str) -> int:
        self.replaced_src_ids.append(set(src_page_ids))
        self.replaced_for_sources_collections.append(collection)
        deleted = self.existing_src_ids & src_page_ids
        self.existing_src_ids -= src_page_ids
        return len(deleted) + len(rows)

    async def refresh_graph(self) -> None:
        self.refreshed += 1



class _RecordingSummaryRepo:
    """Records the disclosure-summary prunes over the scripted `section_units` and `page_units`."""

    def __init__(self, *, calls: _CallTrace | None = None, name: str = "summaries") -> None:
        self.section_units: dict[str, str] = {}
        self.page_units: set[str] = set()
        self.pruned: list[set[str]] = []
        self.pruned_collections: list[str] = []
        self.pruned_for_pages: list[tuple[set[str], set[str], set[str]]] = []
        self.pruned_for_pages_collections: list[str] = []
        self._trace = _Tracer(calls, name)

    async def prune_for_pages(
        self, keep_section_ids: set[str], dirty_page_ids: set[str], deleted_page_ids: set[str], *, collection: str
    ) -> int:
        self._trace("prune_for_pages")
        self.pruned_for_pages.append((set(keep_section_ids), set(dirty_page_ids), set(deleted_page_ids)))
        self.pruned_for_pages_collections.append(collection)
        owning_pages = dirty_page_ids | deleted_page_ids
        if not owning_pages:  # mirror the real repo's empty-page-set early return
            return 0
        gone = {unit for unit, page in self.section_units.items() if page in owning_pages and unit not in keep_section_ids}
        gone |= self.page_units & deleted_page_ids
        return self._drop(gone)

    async def prune_missing(self, keep_unit_ids: set[str], *, collection: str) -> int:
        self.pruned.append(set(keep_unit_ids))
        self.pruned_collections.append(collection)
        return self._drop({unit for unit in (set(self.section_units) | self.page_units) if unit not in keep_unit_ids})

    def _drop(self, units: set[str]) -> int:
        """Remove `units` from the store and return how many rows that deleted."""
        for unit in units:
            self.section_units.pop(unit, None)
        self.page_units -= units
        return len(units)


class _RecordingVocabRepo:
    """Records vocab upserts and prunes on the composite `(collection, vocab, value)` key."""

    def __init__(self) -> None:
        self.upserts: list[list[Row]] = []
        self.pruned: list[set[tuple[str, str]]] = []
        self.pruned_collections: list[str] = []
        self.existing: set[tuple[str, str]] = set()

    async def upsert_many(self, rows: Sequence[Row]) -> None:
        self.upserts.append(list(rows))

    async def prune_missing(self, keep: set[tuple[str, str]], *, collection: str) -> int:
        self.pruned.append(set(keep))
        self.pruned_collections.append(collection)
        deleted = self.existing - keep
        self.existing &= keep
        return len(deleted)

    @property
    def upserted_rows(self) -> list[Row]:
        """Every vocab row upserted across all batches, flattened."""
        return [row for batch in self.upserts for row in batch]


class _RecordingPageTypeContractRepo:
    """Records page-type-contract upserts and prunes on the composite `(collection, page_type)` key."""

    def __init__(self) -> None:
        self.upserts: list[list[Row]] = []
        self.pruned: list[set[str]] = []
        self.pruned_collections: list[str] = []
        self.existing: set[str] = set()

    async def upsert_many(self, rows: Sequence[Row]) -> None:
        self.upserts.append(list(rows))

    async def prune_missing(self, keep: set[str], *, collection: str) -> int:
        self.pruned.append(set(keep))
        self.pruned_collections.append(collection)
        deleted = self.existing - keep
        self.existing &= keep
        return len(deleted)

    @property
    def upserted_rows(self) -> list[Row]:
        """Every page-type-contract row upserted across all batches, flattened."""
        return [row for batch in self.upserts for row in batch]


class _RecordingSourceRepo:
    """Records the sources_raw and raw_chunks writes the load and embed stages make."""

    def __init__(self, needing_chunks: Sequence[PendingEmbedChunk] = ()) -> None:
        self.sources: list[list[Row]] = []
        self.chunks: list[list[Row]] = []
        self.pruned_sources: list[set[str]] = []
        self.pruned_sources_collections: list[str] = []
        self.pruned_chunks: list[set[str]] = []
        self.pruned_chunks_collections: list[str] = []
        self.deleted_keys: list[set[str]] = []
        self.deleted_keys_collections: list[str] = []
        self.embedded_chunks: list[tuple[str, Sequence[float]]] = []
        self.embedded_chunks_collections: list[str] = []
        self.needing_chunks: list[PendingEmbedChunk] = list(needing_chunks)
        self.existing_sources: set[str] = set()
        self.existing_chunks: set[str] = set()

    async def upsert_sources(self, rows: Sequence[Row]) -> None:
        self.sources.append(list(rows))

    async def upsert_chunks(self, rows: Sequence[Row]) -> None:
        self.chunks.append(list(rows))

    async def prune_missing_sources(self, keep_keys: set[str], *, collection: str) -> int:
        self.pruned_sources.append(set(keep_keys))
        self.pruned_sources_collections.append(collection)
        deleted = self.existing_sources - keep_keys
        self.existing_sources &= keep_keys
        return len(deleted)

    async def delete_sources_by_keys(self, source_ids: set[str], *, collection: str) -> int:
        if not source_ids:  # mirror the real repo's empty-set early return (records no call)
            return 0
        self.deleted_keys.append(set(source_ids))
        self.deleted_keys_collections.append(collection)
        deleted = self.existing_sources & source_ids
        self.existing_sources -= source_ids
        return len(deleted)

    async def prune_missing_chunks(self, keep_ids: set[str], *, collection: str) -> int:
        self.pruned_chunks.append(set(keep_ids))
        self.pruned_chunks_collections.append(collection)
        deleted = self.existing_chunks - keep_ids
        self.existing_chunks &= keep_ids
        return len(deleted)

    async def chunks_needing_embedding(self, *, collection: str = "minimal") -> Sequence[PendingEmbedChunk]:
        del collection  # the scripted queue plays every scoped read the same way
        return list(self.needing_chunks)

    async def write_chunk_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None:
        self.embedded_chunks.extend(vectors.items())
        self.embedded_chunks_collections.append(collection)

    @property
    def upserted_chunks(self) -> list[Row]:
        """Every raw-chunk row upserted across all batches, flattened."""
        return [row for batch in self.chunks for row in batch]


class _RecordingMetaRepo:
    """Records the single embed-meta row write (model and dimensions)."""

    def __init__(self) -> None:
        self.embed_meta: tuple[str, int] | None = None

    async def set_embed_meta(self, *, model: str, dim: int) -> None:
        self.embed_meta = (model, dim)


class _RecordingManifestRepo:
    """Records index-manifest writes, and `load` returns the scripted `stored` entries by path."""

    def __init__(self, *, calls: _CallTrace | None = None, name: str = "manifest") -> None:
        self.stored: dict[str, ManifestEntry] = {}
        self.manifest_upserts: list[list[Row]] = []
        self.upsert_collections: list[str] = []
        self.deleted_paths: list[set[str]] = []
        self.deleted_paths_collections: list[str] = []
        self._trace = _Tracer(calls, name)

    async def load(self, collection: str) -> dict[str, ManifestEntry]:
        del collection
        return dict(self.stored)

    async def upsert_many(self, entries: Sequence[Row], *, collection: str) -> None:
        self._trace("upsert_many")
        self.manifest_upserts.append(list(entries))
        self.upsert_collections.append(collection)

    async def delete_paths(self, paths: set[str], *, collection: str) -> None:
        if not paths:  # mirror the real repo's empty-set early return (records no call)
            return
        self.deleted_paths.append(set(paths))
        self.deleted_paths_collections.append(collection)


class _RecordingLinkRefRepo:
    """Records link-ref replacements, answering the closure queries from the scripted `referencing` and `pointing_at`."""

    def __init__(self) -> None:
        self.referencing: set[str] = set()
        self.pointing_at: set[str] = set()
        self.link_ref_rows: list[list[Row]] = []
        self.replaced_src_ids: list[set[str]] = []
        self.replaced_collections: list[str] = []
        self.referencing_calls: list[set[str]] = []
        self.pointing_at_calls: list[set[str]] = []

    async def src_pages_referencing(self, names: set[str], *, collection: str) -> set[str]:
        del collection
        self.referencing_calls.append(set(names))
        return set(self.referencing)

    async def src_pages_pointing_at(self, dst_ids: set[str], *, collection: str) -> set[str]:
        del collection
        self.pointing_at_calls.append(set(dst_ids))
        return set(self.pointing_at)

    async def replace_for_sources(self, rows: Sequence[Row], src_ids: set[str], *, collection: str) -> None:
        self.link_ref_rows.append(list(rows))
        self.replaced_src_ids.append(set(src_ids))
        self.replaced_collections.append(collection)


class _RecordingClaimQualifierRepo:
    """Records each per-page claim-qualifier replacement."""

    def __init__(self) -> None:
        self.qualifier_rows: list[list[Row]] = []
        self.replaced_page_ids: list[set[str]] = []
        self.replaced_collections: list[str] = []

    async def replace_for_pages(self, rows: Sequence[Row], page_ids: set[str], *, collection: str) -> None:
        self.qualifier_rows.append(list(rows))
        self.replaced_page_ids.append(set(page_ids))
        self.replaced_collections.append(collection)


class _RecordingValidationStoreRepo:
    """Records validation replacements, and `whole_report` returns the scripted `report`."""

    def __init__(self) -> None:
        self.report: ValidationReport = ValidationReport()
        self.validation_rows: list[list[Row]] = []
        self.replaced_page_ids: list[set[str]] = []
        self.replaced_collections: list[str] = []
        self.replaced_all: list[str] = []

    async def replace_all(self, issues: Sequence[Row], *, collection: str) -> None:
        self.validation_rows.append(list(issues))
        self.replaced_all.append(collection)

    async def replace_for_pages(self, issues: Sequence[Row], page_ids: set[str], *, collection: str) -> None:
        self.validation_rows.append(list(issues))
        self.replaced_page_ids.append(set(page_ids))
        self.replaced_collections.append(collection)

    async def whole_report(self, collection: str) -> ValidationReport:
        del collection
        return self.report


class _RecordingFingerprintRepo:
    """Records fingerprint and switch writes, answering reads from the scripted `stored` and `stored_switches`."""

    def __init__(self, *, calls: _CallTrace | None = None, name: str = "fingerprint") -> None:
        self.stored: StoredFingerprint | None = None
        self.fingerprint: Row | None = None
        self.touch_calls: list[tuple[str, str]] = []
        self.stored_switches: RetrievalSwitches = RetrievalSwitches()
        self.set_switches_calls: list[tuple[str, RetrievalSwitches]] = []
        self._trace = _Tracer(calls, name)

    async def get(self, collection: str) -> StoredFingerprint | None:
        del collection
        return self.stored

    async def set(
        self,
        *,
        collection: str,
        indexer_version: str,
        spec_hash: str,
        vocabulary_hash: str,
        embed_model: str,
        knowledge_base_root: str,
    ) -> None:
        self.fingerprint = {
            "collection": collection,
            "indexer_version": indexer_version,
            "spec_hash": spec_hash,
            "vocabulary_hash": vocabulary_hash,
            "embed_model": embed_model,
            "knowledge_base_root": knowledge_base_root,
        }

    async def touch(self, collection: str, *, knowledge_base_root: str) -> None:
        self._trace("touch")
        self.touch_calls.append((collection, knowledge_base_root))

    async def get_switches(self, collection: str) -> RetrievalSwitches:
        del collection
        return self.stored_switches

    async def set_switches(self, *, collection: str, switches: RetrievalSwitches) -> None:
        self.set_switches_calls.append((collection, switches))
        self.stored_switches = switches


class _RecordingSignalsRepo:
    """Records each `measure` call and returns the scripted `signals`."""

    def __init__(self) -> None:
        self.signals: Signals = Signals(notes=0, token_estimate=0, page_types=0, edges_per_note=0.0, giant_component=0.0)
        self.measured_collections: list[str] = []

    async def measure(self, collection: str) -> Signals:
        self.measured_collections.append(collection)
        return self.signals


class FakeKnowledgeBaseUnitOfWork:
    """In-memory :class:`~engram.indexing.internal_contracts.KnowledgeBaseUnitOfWork` with recording repos.

    Each repo is a recorder, so a test asserts which rows were upserted, which id sets were pruned
    and which embeddings were written.

    `calls` is the shared, ordered log of the load operations whose relative order is part of the
    write contract: each child aggregate's `upsert_many` and `prune_for_pages`, the targeted
    `pages.delete_by_ids`, the summary reconcile, `manifest.upsert_many`, `fingerprint.touch`, and
    `commit`.
    """

    def __init__(
        self,
        *,
        pages_needing: Sequence[PendingEmbedRow] = (),
        sections_needing: Sequence[PendingEmbedRow] = (),
        claims_needing: Sequence[PendingEmbedRow] = (),
        chunks_needing: Sequence[PendingEmbedChunk] = (),
    ) -> None:
        """Script the embed-stage work queues.

        An empty queue means nothing needs embedding. A populated queue means those units are embedded.

        Args:
            pages_needing: The pages that need embedding.
            sections_needing: The sections that need embedding.
            claims_needing: The claims that need embedding.
            chunks_needing: The raw chunks that need embedding.
        """
        self.committed = False
        self.rolled_back = False
        self.enter_count = 0
        self.coordination = CoordinationBuffer()
        self.calls: _CallTrace = []
        self.pages = _RecordingRepo(pages_needing, calls=self.calls, name="pages")
        self.sections = _RecordingRepo(sections_needing, calls=self.calls, name="sections")
        self.claims = _RecordingRepo(claims_needing, calls=self.calls, name="claims")
        self.claim_qualifiers = _RecordingClaimQualifierRepo()
        self.edges = _RecordingEdgeRepo()
        self.sources = _RecordingSourceRepo(chunks_needing)
        self.vocab = _RecordingVocabRepo()
        self.summaries = _RecordingSummaryRepo(calls=self.calls)
        self.meta = _RecordingMetaRepo()
        self.manifest = _RecordingManifestRepo(calls=self.calls)
        self.link_refs = _RecordingLinkRefRepo()
        self.validation_store = _RecordingValidationStoreRepo()
        self.fingerprint = _RecordingFingerprintRepo(calls=self.calls)
        self.page_type_contracts = _RecordingPageTypeContractRepo()
        self.signals = _RecordingSignalsRepo()

    async def __aenter__(self) -> Self:
        self.enter_count += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        del exc
        if not self.committed:
            self.rolled_back = True
            self.coordination.clear()

    async def commit(self) -> None:
        """Log the commit and mark the unit of work committed."""
        self.calls.append("commit")
        self.committed = True

    def record(self, *events: Event) -> None:
        """Queue events on the coordination buffer.

        Args:
            *events: The events to queue.
        """
        self.coordination.record(*events)

    def record_delivery(self, event_id: UUID, subscriber: str) -> None:
        """Queue a delivery record on the coordination buffer.

        Args:
            event_id: The delivered event.
            subscriber: The subscriber that received it.
        """
        self.coordination.record_delivery(event_id, subscriber)


class FakeValidationReportSink:
    """Records each validation report written to the sink, with no filesystem.

    A test reads `written` to check that the handler wrote the report.
    """

    def __init__(self) -> None:
        self.written: list[object] = []

    async def write(self, report: object) -> None:
        """Record the report.

        Args:
            report: The validation report.
        """
        self.written.append(report)
