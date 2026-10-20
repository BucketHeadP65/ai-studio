"""The incremental index: reindexes only the files whose indexed output can differ.

`run_incremental` hashes every file, compares the hashes with the stored manifest and widens the changed
files into the dirty set: a structural change (a file added, removed or renamed, or an id renamed) adds the
unchanged pages that link to the keys that appeared or disappeared. Only the dirty set is parsed, resolved,
validated and loaded, in one transaction, and the graph is refreshed only when an edge changed.

You must route a fingerprint mismatch or an empty manifest to the full rebuild first. `run_incremental`
assumes a matching fingerprint and a non-empty manifest. `preview_dirty_set` makes the same plan for a dry
run and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from core.markdown import split_frontmatter
from core.observability import get_logger

from engram.indexing.domain import card
from engram.indexing.domain.manifest import (
    DirtySet,
    FileType,
    HashedFile,
    IdentitySurface,
    ManifestDiff,
    ManifestEntry,
    compute_closure,
    compute_diff,
    identity_hash,
)
from engram.indexing.domain.mirror_chunks import DEFAULT_CHUNK_SIZES
from engram.indexing.domain.models import (
    CitingPagesChanged,
    CutPlanCounts,
    DryRunPreview,
    IndexReport,
    MirrorDoc,
    PagesLoaded,
    PartitionPreview,
    ValidationIssue,
    ValidationReport,
    cited_mirror_key,
    loadable_mirrors,
)
from engram.indexing.domain.resolver import build_link_index, build_link_index_from_pages, resolve_with_index
from engram.indexing.domain.validator import mirror_provider_issues, validate
from engram.indexing.service.embed_stage import EmbedStage
from engram.indexing.service.orientation import orientation_page_ids, read_orientation
from engram.indexing.service.row_builder import (
    LoadSet,
    RowBuilder,
    digest_page_ids,
    duplicate_id_errors,
    id_str,
    link_ref_rows,
    page_type_contract_rows,
    row_ids,
    validation_row,
)
from engram.kb.contracts import parse as parse_note
from engram.kb.contracts import parse_vocabulary

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from pathlib import Path

    from core.ai.embedding import BatchEmbedder
    from core.ai.retrieval.contracts import Row

    from engram.indexing.domain.mirror_chunks import ChunkSizes
    from engram.indexing.domain.models import IndexKnowledgeBase, ResolvedNote
    from engram.indexing.domain.resolver import ResolveResult
    from engram.indexing.internal_contracts import IncrementalDeps, IncrementalUnitOfWork, KnowledgeBaseReader
    from engram.indexing.read_models import CleanPage, StoredFingerprint
    from engram.indexing.service.cut_plan_stage import CutPlanStage
    from engram.kb.contracts import ClaimGrammarSpec, FacetSpec, KnowledgeBaseNote, KnowledgeBaseSchema, SectionSummarySpec, VocabularyIndex

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RunIncrementalDeps:
    """The collaborators and fingerprint inputs `run_incremental` and `preview_dirty_set` read.

    It satisfies the `IncrementalDeps` protocol. `clean_pages` holds the stored pages with their manifest
    paths, read before the run.
    """

    uow_factory: Callable[[], IncrementalUnitOfWork]
    kb_reader: KnowledgeBaseReader
    embedder: BatchEmbedder
    spec: KnowledgeBaseSchema
    indexer_version: str
    spec_hash: str
    vocabulary_hash: str
    clean_pages: tuple[CleanPage, ...]
    path_root: Path | None = None
    path_root_source: str | None = None

    def clean_page_view(self) -> Sequence[CleanPage]:
        """The stored pages with their manifest paths.

        Returns:
            The pages `clean_pages` holds.
        """
        return self.clean_pages


async def run_incremental(
    cmd: IndexKnowledgeBase, *, deps: IncrementalDeps, chunk_sizes: ChunkSizes = DEFAULT_CHUNK_SIZES, cut_plan_stage: CutPlanStage | None = None
) -> IndexReport:
    """Reindex only the dirty set in one transaction and return its report.

    You must call it only when the stored fingerprint matches and the manifest is not empty.
    Pages that vanished or were renamed away are deleted, and every write is scoped to the command's collection.
    When `cmd.embed` is set, every file owning a row with no embedding joins the dirty set, and the changed
    rows are embedded after the load. The `PagesLoaded` and `CitingPagesChanged` events commit with the rows.

    Args:
        cmd: The index command. Its `collection` scopes every read and write, and its `embed` flag turns embedding on.
        deps: The unit of work factory, reader, embedder, schema, stored pages and fingerprint inputs.
        chunk_sizes: How many tokens a chunk of a changed source mirror aims for and may hold.
        cut_plan_stage: Gives the changed source mirrors the cut plans the chunk planner chose. `None` fills every heading
            section over the maximum chunk size.

    Returns:
        The report, with `mode="incremental"`, `skipped` counting the manifest files left untouched, `validation` holding
        the dirty set's issues and `standing_validation` every issue stored for the collection once the load committed.
    """
    collection = cmd.collection
    files = _read_files(deps.kb_reader, collection)
    vocabulary = _read_vocabulary(deps.kb_reader)

    stored = await _stored_manifest(deps, collection)
    hashed = {path: _hash_file(path, doc) for path, doc in files.items()}
    diff = compute_diff(stored, hashed)

    clean = deps.clean_page_view()
    embed_stage = EmbedStage(deps.uow_factory, deps.embedder)
    rebuild_about = False
    if cmd.embed:
        # Fold every file owning a row with no embedding into the dirty set, so its embed input is
        # rebuilt and no row is embedded from an empty string.
        widening = await _plan_embed_widening(embed_stage, clean=clean, stored=stored, collection=collection)
        diff = _widen_diff(diff, widening.content_changed)
        rebuild_about = widening.rebuild_about

    old_surfaces = _old_surfaces(clean, stored)
    reparse_notes = _parse_dirty_notes(frozenset({*diff.added, *diff.content_changed}), files, deps.spec).notes
    new_surfaces = _new_surfaces(reparse_notes)
    name_srcs = await _fetch_name_srcs(deps, diff, old_surfaces=old_surfaces, new_surfaces=new_surfaces, collection=collection)
    closure = _Closure(name_srcs=name_srcs, citers_by_key=_citers_by_key(clean))
    dirty_set = compute_closure(
        diff,
        old_surfaces=old_surfaces,
        new_surfaces=new_surfaces,
        inbound_by_name=closure.by_name,
        mirror_citers=closure.mirror_citers,
        path_of_page={page.id: page.path for page in clean}.get,
    )

    parsed = _parse_dirty_notes(dirty_set.dirty_paths, files, deps.spec)
    dirty_notes = parsed.notes
    dirty_sources = _dirty_sources(diff, files, collection)
    index = build_link_index_from_pages(_index_base(clean, dirty_set)).merge(build_link_index(dirty_notes))
    resolved = resolve_with_index(dirty_notes, index, relations=deps.spec.relations)
    other_ids = _other_page_ids(clean, dirty_set, resolved)
    read_mirrors = _all_mirrors(files)
    mirror_errors = mirror_provider_issues(read_mirrors)
    mirror_texts = {source.source_key: source.text for source in loadable_mirrors(read_mirrors)}
    validation = _incremental_validate(
        resolved,
        vocabulary,
        mirror_texts=mirror_texts,
        spec=deps.spec,
        other_ids=other_ids,
        path_root=deps.path_root,
        path_root_source=deps.path_root_source,
        parse_errors=(*parsed.errors, *mirror_errors),
        knowledge_base_source_keys=frozenset(mirror_texts),
    )

    orientation = read_orientation(deps.kb_reader) if rebuild_about else {}
    dirty_mirrors = loadable_mirrors(dirty_sources)
    cut_plans = CutPlanCounts()
    if cut_plan_stage is not None:
        dirty_mirrors, cut_plans = await cut_plan_stage.run(dirty_mirrors, collection=collection, sizes=chunk_sizes)
    load = _build_dirty_load(
        resolved,
        dirty_mirrors,
        vocabulary,
        collection,
        section_summary_spec=deps.spec.section_summaries,
        active_embed_strategies=deps.spec.active_embed_strategies(),
        orientation=orientation,
        facets=deps.spec.facets,
        claim_grammar=deps.spec.claims,
        # A dirty page may cite a mirror of the knowledge base or a `sources_raw` row an earlier run
        # kept after its mirror was removed. Both satisfy the pages foreign key.
        known_source_keys=frozenset(mirror_texts) | frozenset(page.source_id for page in clean if page.source_id),
        source_key_field=deps.spec.sources.key_field,
        digest_link_field=deps.spec.sources.digest_link_field,
        source_hash_field=deps.spec.sources.hash_field,
        date_field=deps.spec.retrieval.date_field,
        chunk_sizes=chunk_sizes,
    )
    write = _DirtyWrite(
        load=load,
        link_ref_rows=link_ref_rows(resolved, collection),
        # A parse or mirror error is keyed by its file path, so those paths join the dirty ids and
        # their errors are stored as a full rebuild stores them.
        validation_rows=_validation_rows(validation, row_ids(load.pages) | {issue.page_id for issue in (*parsed.errors, *mirror_errors)}, collection),
        manifest_rows=_manifest_rows(resolved.notes, hashed, collection),
        edge_src_ids=row_ids(load.pages) | dirty_set.deleted_page_ids,
        deleted_page_ids=set(dirty_set.deleted_page_ids),
        deleted_source_keys=_deletable_source_keys(
            dirty_set,
            clean=clean,
            dirty_notes=resolved.notes,
            source_key_field=deps.spec.sources.key_field,
            digest_link_field=deps.spec.sources.digest_link_field,
        ),
        removed_paths=set(diff.removed),
    )
    pruned, standing_validation = await _load_dirty(deps, write, collection)

    # The upsert cleared the embedding of every changed row, so only those rows are embedded.
    embedded = await embed_stage.run(load.embed_inputs, collection=collection) if cmd.embed else None
    report = IndexReport(
        pages=len(load.pages),
        sections=len(load.sections),
        claims=len(load.claims),
        edges=len(load.edges),
        pruned=pruned,
        embedded=embedded.embedded if embedded else 0,
        reused=embedded.reused if embedded else 0,
        embedding_tokens=embedded.input_tokens if embedded else 0,
        skipped=len(stored) - len(dirty_set.dirty_paths),
        mode="incremental",
        validation=validation,
        standing_validation=standing_validation,
        cut_plans=cut_plans,
    )
    return report


# How many sorted sample paths a dry-run partition lists. Its count is always the full size.
_DRY_RUN_SAMPLE = 10


async def preview_dirty_set(cmd: IndexKnowledgeBase, *, deps: IncrementalDeps) -> DryRunPreview:
    """Preview the files a real run would process, writing nothing.

    The preview names exactly the files a real incremental run would process, with the embed and edge work it would do.
    A fingerprint mismatch or an empty manifest previews a full rebuild (`would_full=True`, every file in
    `content_changed`) rather than raising.

    Args:
        cmd: The index command. Its `collection` scopes every read.
        deps: The unit of work factory, reader, embedder, schema, stored pages and fingerprint inputs. Only reads are made.

    Returns:
        The preview, with each partition's count and a sample of its paths.
    """
    collection = cmd.collection
    files = _read_files(deps.kb_reader, collection)

    if not _fingerprint_matches(deps, await _stored_fingerprint(deps, collection)):
        return _full_preview(files)  # a fingerprint mismatch makes a real run full
    stored = await _stored_manifest(deps, collection)
    if not stored:
        return _full_preview(files)  # an empty manifest makes a real run full

    hashed = {path: _hash_file(path, doc) for path, doc in files.items()}
    diff = compute_diff(stored, hashed)

    clean = deps.clean_page_view()
    old_surfaces = _old_surfaces(clean, stored)
    reparse_notes = _parse_dirty_notes(frozenset({*diff.added, *diff.content_changed}), files, deps.spec).notes
    new_surfaces = _new_surfaces(reparse_notes)
    name_srcs = await _fetch_name_srcs(deps, diff, old_surfaces=old_surfaces, new_surfaces=new_surfaces, collection=collection)
    closure = _Closure(name_srcs=name_srcs, citers_by_key=_citers_by_key(clean))
    path_of_page = {page.id: page.path for page in clean}.get

    dirty_set = compute_closure(
        diff,
        old_surfaces=old_surfaces,
        new_surfaces=new_surfaces,
        inbound_by_name=closure.by_name,
        mirror_citers=closure.mirror_citers,
        path_of_page=path_of_page,
    )
    closure_paths = _paths_of(name_srcs, path_of_page)
    citer_paths = _paths_of(closure.mirror_citers(_changed_keys(diff, old_surfaces=old_surfaces, new_surfaces=new_surfaces)), path_of_page)

    dirty_notes = _parse_dirty_notes(dirty_set.dirty_paths, files, deps.spec).notes
    dirty_sources = _dirty_sources(diff, files, collection)
    index = build_link_index_from_pages(_index_base(clean, dirty_set)).merge(build_link_index(dirty_notes))
    resolved = resolve_with_index(dirty_notes, index, relations=deps.spec.relations)
    load = _build_dirty_load(
        resolved,
        loadable_mirrors(dirty_sources),
        vocabulary=_read_vocabulary(deps.kb_reader),
        collection=collection,
        section_summary_spec=deps.spec.section_summaries,
        active_embed_strategies=deps.spec.active_embed_strategies(),
        facets=deps.spec.facets,
        claim_grammar=deps.spec.claims,
        source_key_field=deps.spec.sources.key_field,
        digest_link_field=deps.spec.sources.digest_link_field,
        source_hash_field=deps.spec.sources.hash_field,
        date_field=deps.spec.retrieval.date_field,
    )

    edges_to_rebuild = len(load.edges)
    return DryRunPreview(
        would_full=False,
        added=_partition(diff.added),
        removed=_partition(diff.removed),
        content_changed=_partition(diff.content_changed),
        inbound_closure=_partition(tuple(sorted(closure_paths))),
        mirror_citers=_partition(tuple(sorted(citer_paths))),
        pages_to_reembed=len(load.embed_inputs),
        edges_to_rebuild=edges_to_rebuild,
        graph_refresh=edges_to_rebuild > 0 or bool(dirty_set.deleted_page_ids),
    )


def _full_preview(files: Mapping[str, _FileDoc]) -> DryRunPreview:
    """The preview of a full rebuild: every file in `content_changed`, the graph refreshed, the counts left zero."""
    paths = tuple(sorted(files))
    return DryRunPreview(would_full=True, content_changed=_partition(paths), graph_refresh=True)


def _partition(paths: tuple[str, ...]) -> PartitionPreview:
    """A `PartitionPreview` over `paths`: the full count + the first `_DRY_RUN_SAMPLE` sorted paths."""
    ordered = tuple(sorted(paths))
    return PartitionPreview(count=len(ordered), sample=ordered[:_DRY_RUN_SAMPLE])


def _paths_of(page_ids: frozenset[str], path_of_page: Callable[[str], str | None]) -> set[str]:
    """The current paths of `page_ids`, dropping ids with no path."""
    return {path for page_id in page_ids if (path := path_of_page(page_id)) is not None}


def _changed_keys(
    diff: ManifestDiff, *, old_surfaces: dict[str, IdentitySurface], new_surfaces: dict[str, IdentitySurface]
) -> frozenset[str]:
    """The resolution keys of every changed or removed file, as `compute_closure` derives them."""
    keys: set[str] = set()
    for path in {*diff.content_changed, *diff.removed}:
        surface = new_surfaces.get(path) or old_surfaces.get(path)
        if surface is not None:
            keys |= surface.keys
    return frozenset(keys)


async def _stored_fingerprint(deps: IncrementalDeps, collection: str) -> StoredFingerprint | None:
    """The stored fingerprint, or `None` before the first index."""
    async with deps.uow_factory() as uow:
        return await uow.fingerprint.get(collection)


def _fingerprint_matches(deps: IncrementalDeps, stored: StoredFingerprint | None) -> bool:
    """Whether the current fingerprint inputs equal the stored snapshot."""
    if stored is None:
        return False
    return (
        stored.indexer_version == deps.indexer_version
        and stored.spec_hash == deps.spec_hash
        and stored.vocabulary_hash == deps.vocabulary_hash
        and stored.embed_model == deps.embedder.model_id
    )


async def _stored_manifest(deps: IncrementalDeps, collection: str) -> dict[str, ManifestEntry]:
    """The stored manifest by path."""
    async with deps.uow_factory() as uow:
        loaded = await uow.manifest.load(collection)
    return dict(loaded)


@dataclass(frozen=True, slots=True)
class _EmbedWidening:
    """The files owning a row with no embedding, and whether an orientation page needs rebuilding too."""

    content_changed: frozenset[str]
    rebuild_about: bool


async def _plan_embed_widening(
    stage: EmbedStage, *, clean: Sequence[CleanPage], stored: Mapping[str, ManifestEntry], collection: str
) -> _EmbedWidening:
    """Map every row with no embedding to the file that owns it."""
    path_of_page = {page.id: page.path for page in clean}
    path_of_key = {entry.source_key: entry.path for entry in stored.values() if entry.kind is FileType.SOURCE and entry.source_key}
    pending = await stage.pending(collection=collection)
    orientation_ids = orientation_page_ids(collection)

    paths: set[str] = set()
    rebuild_about = False
    for owner_id in (*pending.page_ids, *pending.section_ids, *pending.claim_ids):
        page_path = path_of_page.get(owner_id.split("#", 1)[0])
        if page_path is not None:
            paths.add(page_path)
        elif owner_id in orientation_ids:
            rebuild_about = True
    paths |= {path for _chunk_id, key in pending.chunks if (path := path_of_key.get(key)) is not None}
    return _EmbedWidening(content_changed=frozenset(paths), rebuild_about=rebuild_about)


def _widen_diff(diff: ManifestDiff, extra: frozenset[str]) -> ManifestDiff:
    """Move the unchanged paths in `extra` from `unchanged` into `content_changed`."""
    new = extra - {*diff.added, *diff.content_changed, *diff.removed}
    if not new:
        return diff
    return replace(diff, content_changed=tuple(sorted({*diff.content_changed, *new})), unchanged=tuple(sorted(set(diff.unchanged) - new)))


def _read_files(reader: KnowledgeBaseReader, collection: str) -> dict[str, _FileDoc]:
    """Every note and source mirror of the knowledge base, keyed by path."""
    files: dict[str, _FileDoc] = {}
    for document in reader.unit_documents():
        files[document.path] = _FileDoc(path=document.path, kind=FileType.NOTE, text=document.text)
    for document in reader.source_mirrors():
        mirror = MirrorDoc.from_text(document.path, document.text, collection)
        files[document.path] = _FileDoc(path=document.path, kind=FileType.SOURCE, text=document.text, source_key=mirror.source_key)
    return files


def _read_vocabulary(reader: KnowledgeBaseReader) -> VocabularyIndex:
    """The vocabulary index read from the `_meta/vocabulary/` files."""
    return parse_vocabulary((doc.path.rsplit("/", 1)[-1], doc.text) for doc in reader.vocabulary_documents()).index


def _hash_file(path: str, doc: _FileDoc) -> HashedFile:
    """Hash one file's raw text and its identity, a note's from its frontmatter and a mirror's from its source key."""
    content = card.content_hash(doc.text)
    if doc.kind is FileType.SOURCE:
        return HashedFile(path=path, kind=doc.kind, text=doc.text, content_hash=content, identity_hash=card.content_hash(doc.source_key))
    data, _ = split_frontmatter(doc.text)
    stem = _file_stem(path)
    identity = identity_hash(stem=stem, id_=str(data.get("id", "")), title=str(data.get("title", "")), aliases=_frontmatter_aliases(data))
    return HashedFile(path=path, kind=doc.kind, text=doc.text, content_hash=content, identity_hash=identity)


def _frontmatter_aliases(data: Mapping[str, object]) -> tuple[str, ...]:
    """The frontmatter's `aliases` as strings, empty when absent."""
    raw = data.get("aliases")
    if isinstance(raw, (list, tuple)):
        return tuple(str(alias) for alias in raw)
    return ()


@dataclass(frozen=True)
class _ParsedDirty:
    """The dirty NOTE files that parsed, and one `parse-error` validation issue per file that did not."""

    notes: tuple[KnowledgeBaseNote, ...]
    errors: tuple[ValidationIssue, ...]


def _parse_dirty_notes(paths: Collection[str], files: Mapping[str, _FileDoc], spec: KnowledgeBaseSchema) -> _ParsedDirty:
    """Parse the note files among `paths`, turning each failure into a `parse-error` issue keyed by its path."""
    notes: list[KnowledgeBaseNote] = []
    errors: list[ValidationIssue] = []
    for path in sorted(paths):
        doc = files.get(path)
        if doc is None:
            # The closure can name a file that moved or was deleted since the stored manifest.
            continue
        if doc.kind is not FileType.NOTE:
            continue
        try:
            notes.append(parse_note(doc.text, source_path=path, spec=spec))
        except Exception as exc:  # noqa: BLE001 (one bad note is a validation error, never an abort)
            _logger.warning("Failed to parse {path}: {reason}", path=str(path), reason=str(exc))
            errors.append(ValidationIssue(severity="error", rule="parse-error", page_id=path, message=f"failed to parse: {exc}"))
    return _ParsedDirty(notes=tuple(notes), errors=tuple(errors))


def _old_surfaces(clean: Sequence[CleanPage], stored: Mapping[str, ManifestEntry]) -> dict[str, IdentitySurface]:
    """The stored identity surfaces by path: a note's from its page row, a mirror's from its manifest entry."""
    surfaces = {page.path: IdentitySurface(page_id=page.id, keys=_page_keys(page)) for page in clean}
    for path, entry in stored.items():
        if entry.kind is FileType.SOURCE and entry.source_key:
            # The source key keeps its case: the delete and the citer lookup match it exactly.
            surfaces[path] = IdentitySurface(page_id="", keys=frozenset({entry.source_key}))
    return surfaces


def _new_surfaces(dirty_notes: Sequence[KnowledgeBaseNote]) -> dict[str, IdentitySurface]:
    """The re-parsed dirty notes' identity surfaces keyed by path."""
    surfaces: dict[str, IdentitySurface] = {}
    for note in dirty_notes:
        keys = _normalized_keys(_note_stem(note), note.frontmatter.id, note.frontmatter.title, note.frontmatter.aliases)
        surfaces[note.source_path] = IdentitySurface(page_id=note.frontmatter.id, keys=keys)
    return surfaces


def _index_base(clean: Sequence[CleanPage], dirty_set: DirtySet) -> list[CleanPage]:
    """The stored pages that are neither dirty nor deleted."""
    return [page for page in clean if page.path not in dirty_set.dirty_paths and page.id not in dirty_set.deleted_page_ids]


@dataclass(frozen=True, slots=True)
class _Closure:
    """The two synchronous lookups `compute_closure` calls, over the linking pages fetched up front."""

    name_srcs: frozenset[str]
    citers_by_key: Mapping[str, frozenset[str]]

    def by_name(self, _names: frozenset[str]) -> frozenset[str]:
        """The pages linking to a key that appeared or disappeared, fetched before the closure ran."""
        return self.name_srcs

    def mirror_citers(self, keys: frozenset[str]) -> frozenset[str]:
        """The pages whose cited `source_id` is in `keys`."""
        empty: frozenset[str] = frozenset()
        return frozenset().union(*(self.citers_by_key.get(key, empty) for key in keys)) if keys else empty


def _affected_names(
    diff: ManifestDiff, *, old_surfaces: dict[str, IdentitySurface], new_surfaces: dict[str, IdentitySurface]
) -> frozenset[str]:
    """The resolution keys that appeared or disappeared, as `compute_closure` derives them."""
    empty: frozenset[str] = frozenset()
    affected: set[str] = set()
    for path in {*diff.identity_changed, *diff.added, *diff.removed}:
        old_keys = old_surfaces[path].keys if path in old_surfaces else empty
        new_keys = new_surfaces[path].keys if path in new_surfaces else empty
        affected |= old_keys ^ new_keys
    return frozenset(affected)


async def _fetch_name_srcs(
    deps: IncrementalDeps,
    diff: ManifestDiff,
    *,
    old_surfaces: dict[str, IdentitySurface],
    new_surfaces: dict[str, IdentitySurface],
    collection: str,
) -> frozenset[str]:
    """The pages linking to a key that appeared or disappeared, or to a page id that was removed or renamed."""
    affected = _affected_names(diff, old_surfaces=old_surfaces, new_surfaces=new_surfaces)
    deleted_ids = _structural_deleted_ids(diff, old_surfaces=old_surfaces, new_surfaces=new_surfaces)
    if not affected and not deleted_ids:
        return frozenset()
    async with deps.uow_factory() as uow:
        by_name = await uow.link_refs.src_pages_referencing(set(affected), collection=collection)
        by_id = await uow.link_refs.src_pages_pointing_at(deleted_ids, collection=collection)
    return frozenset(by_name | by_id)


def _citers_by_key(clean: Sequence[CleanPage]) -> dict[str, frozenset[str]]:
    """The stored page ids citing each `source_id`."""
    by_key: dict[str, set[str]] = {}
    for page in clean:
        if page.source_id:
            by_key.setdefault(page.source_id, set()).add(page.id)
    return {key: frozenset(ids) for key, ids in by_key.items()}


def _structural_deleted_ids(
    diff: ManifestDiff, *, old_surfaces: dict[str, IdentitySurface], new_surfaces: dict[str, IdentitySurface]
) -> set[str]:
    """The old ids of the notes removed or whose id was renamed, as `compute_closure` derives them."""
    deleted = {old_surfaces[path].page_id for path in diff.removed if path in old_surfaces and old_surfaces[path].page_id}
    deleted |= {
        old_surfaces[path].page_id
        for path in diff.identity_changed
        if path in old_surfaces and path in new_surfaces and old_surfaces[path].page_id != new_surfaces[path].page_id
    }
    return deleted


def _other_page_ids(clean: Sequence[CleanPage], dirty_set: DirtySet, resolved: ResolveResult) -> frozenset[str]:
    """The stored page ids a dirty note could duplicate: every id except the dirty and deleted ones."""
    dirty_ids = {resolved_note.note.frontmatter.id for resolved_note in resolved.notes}
    return frozenset(page.id for page in clean if page.id not in dirty_ids and page.id not in dirty_set.deleted_page_ids)


def _deletable_source_keys(
    dirty_set: DirtySet, *, clean: Sequence[CleanPage], dirty_notes: Sequence[ResolvedNote], source_key_field: str, digest_link_field: str
) -> set[str]:
    """The removed source keys no surviving page still cites, which the load may delete."""
    surviving_refs = {page.source_id for page in clean if page.source_id and page.id not in dirty_set.deleted_page_ids}
    surviving_refs |= {
        key
        for resolved in dirty_notes
        if (key := cited_mirror_key(resolved.note.frontmatter, key_field=source_key_field, digest_link_field=digest_link_field))
    }
    return set(dirty_set.deleted_source_keys) - surviving_refs


def _incremental_validate(  # noqa: PLR0913 (one param per validation input, as validate takes them)
    resolved: ResolveResult,
    vocabulary: VocabularyIndex,
    *,
    mirror_texts: Mapping[str, str],
    spec: KnowledgeBaseSchema,
    other_ids: frozenset[str],
    path_root: Path | None = None,
    path_root_source: str | None = None,
    parse_errors: Sequence[ValidationIssue] = (),
    knowledge_base_source_keys: frozenset[str] | None = None,
) -> ValidationReport:
    """Validate the dirty notes exactly as the full rebuild validates them."""
    report = validate(
        resolved.notes,
        vocabulary,
        mirror_texts=mirror_texts,
        resolve_result=resolved,
        spec=spec,
        global_ids=other_ids,
        path_root=path_root,
        path_root_source=path_root_source,
        knowledge_base_source_keys=knowledge_base_source_keys,
    )
    extra = (*parse_errors, *duplicate_id_errors(resolved.notes))
    if not extra:
        return report
    return report.model_copy(update={"errors": (*extra, *report.errors)})


def _build_dirty_load(  # noqa: PLR0913 (one param per row-build input)
    resolved: ResolveResult,
    dirty_sources: Sequence[MirrorDoc],
    vocabulary: VocabularyIndex,
    collection: str,
    *,
    section_summary_spec: SectionSummarySpec,
    active_embed_strategies: tuple[str, ...] | None = None,
    orientation: Mapping[str, str] | None = None,
    facets: tuple[FacetSpec, ...] = (),
    claim_grammar: ClaimGrammarSpec | None = None,
    known_source_keys: frozenset[str] | None = None,
    source_key_field: str,
    digest_link_field: str,
    source_hash_field: str,
    date_field: str,
    chunk_sizes: ChunkSizes = DEFAULT_CHUNK_SIZES,
) -> LoadSet:
    """The dirty notes' and mirrors' rows and embed inputs, built exactly as the full rebuild builds them."""
    active = frozenset(active_embed_strategies) if active_embed_strategies is not None else None
    builder = RowBuilder(
        collection,
        section_summary_spec=section_summary_spec,
        active=active,
        facets=facets,
        claim_grammar=claim_grammar,
        known_source_keys=known_source_keys,
        source_key_field=source_key_field,
        digest_link_field=digest_link_field,
        source_hash_field=source_hash_field,
        date_field=date_field,
        chunk_sizes=chunk_sizes,
    )
    for resolved_note in resolved.notes:
        builder.add_note(resolved_note)
    for source in dirty_sources:
        builder.add_source(source)
    builder.add_about(orientation or {})
    return builder.build(vocabulary)


def _all_mirrors(files: Mapping[str, _FileDoc]) -> tuple[MirrorDoc, ...]:
    """Every source mirror of the knowledge base, loadable or not, with an empty collection."""
    return tuple(MirrorDoc.from_text(doc.path, doc.text, "") for doc in files.values() if doc.kind is FileType.SOURCE)


def _dirty_sources(diff: ManifestDiff, files: Mapping[str, _FileDoc], collection: str) -> tuple[MirrorDoc, ...]:
    """The added and changed source mirrors, stamped with `collection`."""
    dirty_paths = {*diff.added, *diff.content_changed}
    return tuple(
        MirrorDoc.from_text(files[path].path, files[path].text, collection)
        for path in sorted(dirty_paths)
        if files[path].kind is FileType.SOURCE
    )


async def _load_dirty(deps: IncrementalDeps, write: _DirtyWrite, collection: str) -> tuple[int, ValidationReport]:
    """Write the dirty set and record its events in one transaction, returning the child rows deleted and every stored issue."""
    load = write.load
    dirty_page_ids = row_ids(load.pages)
    section_ids = row_ids(load.sections)
    async with deps.uow_factory() as uow:
        await uow.sources.upsert_sources(load.sources)
        await uow.pages.upsert_many(load.pages)
        await uow.sources.upsert_chunks(load.chunks)
        await uow.sections.upsert_many(load.sections)
        await uow.claims.upsert_many(load.claims)

        pruned = await uow.summaries.prune_for_pages(section_ids, dirty_page_ids, write.deleted_page_ids, collection=collection)

        # A claim cites a page through its subject and its qualifiers with no cascade, so the dirty
        # pages' qualifiers are replaced and their stale claims pruned before any page is deleted.
        await uow.claim_qualifiers.replace_for_pages(load.claim_qualifiers, write.edge_src_ids, collection=collection)
        pruned += await uow.claims.prune_for_pages(row_ids(load.claims), dirty_page_ids, collection=collection)

        await uow.pages.delete_by_ids(write.deleted_page_ids, collection=collection)
        await uow.sources.delete_sources_by_keys(write.deleted_source_keys, collection=collection)

        pruned += await uow.sections.prune_for_pages(section_ids, dirty_page_ids, collection=collection)

        edge_delta = await uow.edges.replace_for_sources(load.edges, write.edge_src_ids, collection=collection)
        await uow.link_refs.replace_for_sources(write.link_ref_rows, write.edge_src_ids, collection=collection)
        # A parse error is keyed by its file path and a vocabulary drift by its own marker, so every
        # key the fresh rows carry and every dirty or removed path is cleared too.
        validation_keys = (
            write.edge_src_ids
            | {id_str(row["page_id"]) for row in write.validation_rows}
            | {id_str(row["path"]) for row in write.manifest_rows}
            | write.removed_paths
        )
        await uow.validation_store.replace_for_pages(write.validation_rows, validation_keys, collection=collection)
        standing_validation = await uow.validation_store.whole_report(collection)
        await uow.manifest.upsert_many(write.manifest_rows, collection=collection)
        await uow.manifest.delete_paths(write.removed_paths, collection=collection)
        # Stamp the index time and the knowledge base root on the fingerprint row, which exists on this path.
        await uow.fingerprint.touch(collection, knowledge_base_root=str(deps.kb_reader.root))
        # Keep the page-type contracts current on every run.
        await uow.page_type_contracts.upsert_many(page_type_contract_rows(deps.spec, collection))
        await uow.page_type_contracts.prune_missing({pt.page_type for pt in deps.spec.page_types}, collection=collection)

        if edge_delta:  # re-project the graph only when an edge changed
            await uow.edges.refresh_graph()

        # The events commit in the same transaction as the rows they describe. `pruned` counts the
        # child rows the reconcile deleted.
        uow.record(
            PagesLoaded(
                collection=collection,
                pages=len(load.pages),
                sections=len(load.sections),
                claims=len(load.claims),
                edges=len(load.edges),
                pruned=pruned,
            ),
            CitingPagesChanged(collection=collection, page_ids=digest_page_ids(load.pages, frozenset(deps.spec.sources.digest_types.values()))),
        )
        await uow.commit()
    return pruned, standing_validation


def _validation_rows(validation: ValidationReport, dirty_page_ids: set[str], collection: str) -> list[Row]:
    """The `kb.validation_store` rows of the issues keyed by one of `dirty_page_ids`."""
    return [validation_row(issue, collection) for issue in (*validation.errors, *validation.warnings) if issue.page_id in dirty_page_ids]


def _manifest_rows(resolved_notes: Sequence[ResolvedNote], hashed: Mapping[str, HashedFile], collection: str) -> list[Row]:
    """One `kb.index_manifest` row per dirty note, from the hash of its exact source path."""
    rows: list[Row] = []
    for resolved_note in resolved_notes:
        note = resolved_note.note
        hashed_file = hashed.get(note.source_path)
        if hashed_file is None:
            continue
        rows.append(
            {
                "collection": collection,
                "path": hashed_file.path,
                "kind": FileType.NOTE.value,
                "content_hash": hashed_file.content_hash,
                "identity_hash": hashed_file.identity_hash,
                "page_id": note.frontmatter.id,
                "source_key": None,
            }
        )
    return rows


def _page_keys(page: CleanPage) -> frozenset[str]:
    """A clean page's normalized resolution-key set `{slug, id, title, *aliases}`."""
    return _normalized_keys(page.slug, page.id, page.title, tuple(page.aliases))


def _normalized_keys(stem: str, id_: str, title: str, aliases: tuple[str, ...]) -> frozenset[str]:
    """The resolution keys stripped and lower-cased, empty ones dropped, as `identity_hash` normalizes them."""
    return frozenset(key.strip().lower() for key in (stem, id_, title, *aliases) if key.strip())


def _note_stem(note: KnowledgeBaseNote) -> str:
    """A note's filename stem (its Obsidian link name)."""
    return _file_stem(note.source_path)


def _file_stem(path: str) -> str:
    """The basename of a knowledge base path without its directory or `.md` extension."""
    return path.rsplit("/", 1)[-1].removesuffix(".md")


@dataclass(frozen=True, slots=True)
class _FileDoc:
    """One knowledge base file read: its path, file type, raw text and, for a mirror, its `source_key`."""

    path: str
    kind: FileType
    text: str
    source_key: str = ""


@dataclass(frozen=True, slots=True)
class _DirtyWrite:
    """The rows `_load_dirty` writes and the page ids, source keys and paths it deletes or replaces."""

    load: LoadSet
    link_ref_rows: list[Row]
    validation_rows: list[Row]
    manifest_rows: list[Row]
    edge_src_ids: set[str]
    deleted_page_ids: set[str]
    deleted_source_keys: set[str]
    removed_paths: set[str]
