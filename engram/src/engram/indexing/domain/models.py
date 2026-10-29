"""The indexing slice's write-side contract: commands, events and result models.

The pure pipeline services (`resolver`, `validator`, `card`) and the handler bind to
these types. The result models' field names match the `engram.indexing.infrastructure.orm`
column names, so the Load stage builds each repository row field for field.

Two families live here:

* **the bus contract**: the two write commands and the events their handlers emit:
  `IndexKnowledgeBase` (project the knowledge base into Postgres) with the six pipeline events, and
  `IngestPage` (write one authored page as data, no knowledge base file) with `PageIngested`. A
  collection is either indexed from a knowledge base or written by a client, and
  `CollectionIsIndexedError` is how the second refuses to break the first.
* **the result models**: `RawDocument` (what a `KnowledgeBaseReader` yields), the
  resolved models the resolver produces (`ResolvedNote`, `ResolvedClaim`,
  `ResolvedLink`, `ResolvedEdge`), the loadable row models the Load stage upserts (`IndexedPage`,
  `SourceDoc`, `RawChunk`, `EdgeRow`), and the validation/index reports
  (`ValidationIssue`, `ValidationReport`, `IndexReport`).

No database or IO imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal

from core.cqrs.contracts import Command, Event
from core.markdown import split_frontmatter
from pydantic import BaseModel, ConfigDict, JsonValue

from engram.kb.contracts import AuthoringPayload, BaseFrontmatter, ClaimBlock, KnowledgeBaseNote

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


Severity = Literal["error", "warning"]
"""A validation issue's blocking level: an `error` makes the CLI exit 1, a `warning` does not."""


def frontmatter_value(frontmatter: BaseFrontmatter, field: str) -> str:
    """The value a page carries under `field`, as text.

    `field` is a frontmatter field name the knowledge base declares (`sources.key_field` for the mirror a
    page cites, `sources.hash_field` for that mirror's content hash). The indexing stages never name
    one themselves.

    Args:
        frontmatter: The page's frontmatter.
        field: The declared field name, or an empty string when the knowledge base declares none.

    Returns:
        The value as text, or an empty string when `field` is empty or the page carries nothing there.
        Every caller reads an empty string as "not set".
    """
    if not field:
        return ""
    value = getattr(frontmatter, field, None)
    return str(value) if value else ""


def cited_mirror_key(frontmatter: BaseFrontmatter, *, key_field: str, digest_link_field: str) -> str:
    """The key of the mirror a page cites: its key field's value, else its digest link field's value.

    Both field names are the ones the knowledge base declares (`sources.key_field`, `sources.digest_link_field`), so a
    digest that names its source only through the digest link field cites that mirror as a key-field page does.

    Args:
        frontmatter: The page's frontmatter.
        key_field: The declared key field, or an empty string.
        digest_link_field: The declared digest link field, or an empty string.

    Returns:
        The mirror key, or an empty string when the page names neither.
    """
    return frontmatter_value(frontmatter, key_field) or frontmatter_value(frontmatter, digest_link_field)


def loadable_mirrors(mirrors: Iterable[MirrorDoc]) -> tuple[MirrorDoc, ...]:
    """The mirrors a load may write, in order: the ones declaring a provider (`MirrorDoc.provider`).

    A mirror declaring none is left out of every downstream set (the `sources_raw` rows, the
    quote-verification texts, and the key universe a page's `source_id` may reference). The validate
    stage reports each one it drops.

    Args:
        mirrors: The parsed source mirrors.

    Returns:
        The mirrors that declare a provider, in their given order.
    """
    return tuple(mirror for mirror in mirrors if mirror.provider)


# -- bus contract: command + events -----------------------------------------


class IndexKnowledgeBase(Command):
    """Rebuild the Postgres target from the knowledge base (the one index-slice command).

    Incremental by default: `handle` re-hashes the knowledge base against the stored manifest and
    reindexes only the dirty set. `full` forces the whole-knowledge-base rebuild. `dry_run` computes
    and returns the dirty-set report but writes nothing (no upserts, deletes or commit). `summaries`
    opts into the disclosure-summary stage after embed: it writes `status='pending'`
    `kb.unit_summaries` rows for units lacking a current summary, never `active` ones.
    `studio summaries approve` is the separate, human-gated promotion step.
    """

    collection: str
    embed: bool = True  # False skips the embed stage (CI, fast dev loop)
    full: bool = False  # force a whole-knowledge-base rebuild
    dry_run: bool = False  # compute and report the dirty set, write nothing
    summaries: bool = False  # True also runs the disclosure-summary stage after embed
    collection_named: bool = False  # True: the caller named `collection` deliberately and may take over a foreign collection


class IngestPage(Command):
    """Write one authored page into a **written** collection: no knowledge base file, no index.

    A collection is **either indexed from a knowledge base or written by a client**. `IngestPage`
    refuses a collection that has an `index_manifest`, because an index would prune what it wrote
    as vanished. The page, its sections and its claims land as the same rows the indexer builds,
    carry the same embed inputs, and answer the same `api.search` query.

    `payload` validates its own frontmatter on construction (`AuthoringPayload`), so a malformed
    page is rejected before any transaction opens.
    """

    collection: str
    payload: AuthoringPayload
    embed: bool = True  # False lands the rows and leaves `embedding IS NULL` for a later pass


class IndexingStarted(Event):
    """Emitted when a rebuild begins, naming the knowledge base root being indexed."""

    knowledge_base_path: str


class NotesParsed(Event):
    """Emitted after the Parse stage: how many memory notes and source mirrors were read."""

    n_memory: int
    n_sources: int


class ValidationCompleted(Event):
    """Emitted after the Validate stage with the error/warning counts."""

    n_errors: int
    n_warnings: int


class PagesLoaded(Event):
    """Emitted after the Load transaction commits, naming its collection, with per-aggregate counts and its changed pages.

    `collection` is the knowledge base partition the transaction wrote. Several knowledge bases index into one
    database, so a subscriber reading this off the outbox knows whose rows the counts describe.
    `page_ids` holds the changed pages, sorted and unique: each note whose file is new to the index manifest the
    index started from or whose content hash differs from it. A page loaded again only to resolve its links or to
    embed its rows is not listed, and neither is a source mirror. A first index, an index of a root that took over
    the collection, and a dry run list none.
    """

    collection: str
    pages: int
    sections: int
    claims: int
    edges: int
    pruned: int
    page_ids: tuple[str, ...]


class CitingPagesChanged(Event):
    """Emitted after Load with the collection indexed and the page id of every digest page in it.

    A subscriber can enqueue the pages whose datasets may need generating again. `page_ids` is
    every page loaded this run whose type is a value of the schema's `sources.digest_types`, and the
    subscriber decides which need work.
    `collection` is the knowledge base partition they were loaded into.
    """

    collection: str
    page_ids: tuple[str, ...]


class EmbeddingsRefreshed(Event):
    """Emitted after the Embed stage: how many units were (re-)embedded vs reused."""

    embedded: int
    reused: int
    model: str


class IndexingCompleted(Event):
    """Emitted last, carrying the full `IndexReport`."""

    report: IndexReport


class PageIngested(Event):
    """Emitted after an `IngestPage` write commits, with the row counts it landed."""

    page_id: str
    collection: str
    sections: int
    claims: int


# -- raw documents -----------------------------------------------------------


class RawDocument(BaseModel):
    """A raw knowledge base file a `KnowledgeBaseReader` yields: its knowledge-base-relative path + text."""

    model_config = ConfigDict(frozen=True)

    path: str
    text: str


@dataclass(frozen=True, slots=True)
class MirrorDoc:
    """A parsed `Sources/` mirror: its knowledge base path + frontmatter + body, keyed by `source_key`."""

    source_key: str
    frontmatter: Mapping[str, object]
    body: str
    text: str
    collection: str
    path: str
    cut_plans: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    """The cut plans chosen for this mirror's text, by the content hash of the heading section text each cuts. Empty fills every longer section."""

    @property
    def provider(self) -> str:
        """The connector name this mirror declares in its `provider` field, stripped, an empty string when it declares none.

        A mirror naming no connector is not loadable, and engram never guesses a name. The index
        reports such a mirror against its path and leaves it out of the load.
        """
        declared = self.frontmatter.get("provider")
        return str(declared).strip() if declared is not None else ""

    @classmethod
    def from_text(cls, path: str, text: str, collection: str) -> MirrorDoc:
        """Parse a mirror file, taking its `source_key` from frontmatter, else from the filename stem.

        Args:
            path: The mirror's knowledge-base-relative path.
            text: The raw file text.
            collection: The collection the mirror belongs to.

        Returns:
            The parsed mirror.
        """
        data, body = split_frontmatter(text)
        raw_key = data.get("source_key")
        key = str(raw_key) if raw_key else path.rsplit("/", 1)[-1].removesuffix(".md")
        return cls(source_key=key, frontmatter=data, body=body, text=text, collection=collection, path=path)


# -- resolved models (resolver output) ---------------------------------------


class ResolvedLink(BaseModel):
    """A unit link resolved to ids, the shape a `kb.link_refs` row is built from.

    `src_id` and `dst_id` are page or claim ids. `dst_id` is `None` when
    the target did not resolve (a dangling link the validator reports). `provenance_claim`
    is the claim id for a link written on a claim, `None` for a page-level (frontmatter) link.
    """

    model_config = ConfigDict(frozen=True)

    src_id: str
    src_type: Literal["page", "claim"]
    rel: str
    dst_target: str  # the raw unit link target, retained for validation messages on a miss
    dst_id: str | None = None
    provenance_claim: str | None = None


class ResolvedEdge(ResolvedLink):
    """A typed relation resolved to ids, the shape `EdgeRow` is built from.

    `qualifiers` carries the claim block's scoping fields for a claim-level edge (copied verbatim
    into `kb.edges.qualifiers`).
    """

    qualifiers: dict[str, JsonValue] = {}


class ResolvedClaim(BaseModel):
    """A `ClaimBlock` with its links resolved to ids and `loc` parsed.

    `subject` becomes `subject_id` (`None` on a miss), `disputed_by` and each declared link
    field become id tuples, `edges` becomes resolved `ResolvedEdge`s. `loc` keeps the raw
    locator text and `loc_path` is its `§`-path indexed to an ltree label (`None`
    when the locator names no `§` section). Mirrors `kb.claims` columns.
    """

    model_config = ConfigDict(frozen=True)

    block: ClaimBlock  # the source claim (text, quote, kind, declared fields verbatim)
    block_id: str  # the anchor without the caret, e.g. 'c3' (kb.claims.block_id)
    loc: str
    loc_path: str | None = None
    subject_id: str | None = None
    link_ids: dict[str, tuple[str, ...]] = {}
    """Each declared link field of the claim by name, with the page ids its targets resolved to in written order.

    A target that resolved to no page is left out.
    """
    disputed_by_ids: tuple[str, ...] = ()
    edges: tuple[ResolvedEdge, ...] = ()
    field_links: tuple[ResolvedLink, ...] = ()
    """One link per target the claim's `subject`, declared link fields and `disputed_by` name, resolved or dangling.

    Each carries the field name as its `rel`, in the order subject, link fields, `disputed_by`, each field's targets in
    written order. They are recorded as link references and never become edges.
    """


class ResolvedNote(BaseModel):
    """One parsed note with its frontmatter edges and claim links resolved to ids.

    Holds the original `KnowledgeBaseNote` (so the validator and card stage see verbatim
    frontmatter/sections/claims) plus the resolved overlay: page-level `edges` from
    frontmatter relations and per-claim `ResolvedClaim`s aligned with `note.claims`
    by index.
    """

    model_config = ConfigDict(frozen=True)

    note: KnowledgeBaseNote
    edges: tuple[ResolvedEdge, ...] = ()
    claims: tuple[ResolvedClaim, ...] = ()
    superseded_by_id: str | None = None
    """The id of the page the frontmatter `superseded_by` link resolved to, `None` when the note has none or it names no page."""


# -- loadable row models (Load-stage input, 1:1 with ORM columns) ------------


class IndexedPage(BaseModel):
    """A `kb.pages` row ready to upsert: identity + generic columns, `card_text`, `body_hash`.

    Field names match the `kb.pages` columns so the Load stage builds the repository row by
    `model_dump`. `frontmatter` is the long-tail jsonb (declared per-page-type fields, minus
    facets). `facets` is the declared-facet jsonb (each facet key to its authored values, non-empty
    only). A knowledge base's shape arrives entirely by declaration (`KnowledgeBaseSchema.facets` /
    `PageTypeSpec.frontmatter_fields`).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    collection: str
    page_type: str
    slug: str
    title: str
    aliases: tuple[str, ...] = ()
    summary: str | None = None
    status: str
    superseded_by: str | None = None
    source_id: str | None = None
    source_hash: str | None = None
    created: date | None = None
    page_date: date | None = None
    """The page date, read from the frontmatter field the schema's `retrieval.date_field` names. A search's date range reads it."""
    facets: dict[str, list[str]] = {}
    frontmatter: dict[str, JsonValue] = {}
    card_text: str
    body_hash: str


class SourceDoc(BaseModel):
    """A `kb.sources_raw` row (immutable mirror metadata), matching its columns.

    `source_id` is engram's own identity (the upsert conflict key). `provider` and
    `provider_key` name the adapter and its native key. `doi` / `arxiv_id` / `venue` / `year`
    fold into the `meta` jsonb, with no research-profile columns.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str
    provider: str
    provider_key: str
    collection: str
    title: str
    source_hash: str
    pdf_path: str | None = None
    meta: dict[str, JsonValue] = {}


class RawChunk(BaseModel):
    """A `kb.raw_chunks` row (an L4 verification span), matching its columns.

    `section_path` is the numbered-heading path indexed to an ltree label
    (`## 4. Experiments` becomes `s4`, `### 4.1` becomes `s4.s1`).
    """

    model_config = ConfigDict(frozen=True)

    id: str  # '<source_id>:<seq>'
    source_id: str
    section_path: str | None = None
    heading: str | None = None
    seq: int
    body: str
    tokens: int | None = None


class EdgeRow(BaseModel):
    """A `kb.edges` row (a resolved typed link), matching its columns except the bigserial id.

    `src_collection` and `dst_collection` are both the indexing collection.
    """

    model_config = ConfigDict(frozen=True)

    src_id: str
    src_collection: str
    src_kind: Literal["page", "claim"]
    rel: str
    dst_id: str
    dst_collection: str
    dst_kind: Literal["page", "claim"]
    qualifiers: dict[str, JsonValue] = {}
    provenance_claim: str | None = None


# -- validation + index reports --------------------------------------------------


class ValidationIssue(BaseModel):
    """One validation finding: a severity, the violated rule, the offending page (+ claim anchor)."""

    model_config = ConfigDict(frozen=True)

    severity: Severity
    rule: str  # stable machine key, e.g. 'missing-quote', 'comparative-missing-qualifiers'
    page_id: str
    message: str
    anchor: str | None = None  # the '^cN' claim anchor for a claim-level issue, else None


class ValidationReport(BaseModel):
    """The Validate stage's output: error/warning issue lists, counts, and ingest queue.

    Validation reports and never filters: a note with errors still loads. `ingest_queue`
    carries the missing targets of every relation the schema declares `queue_when_missing`,
    pages wanted but not yet ingested, surfaced as info, not errors.
    """

    model_config = ConfigDict(frozen=True)

    errors: tuple[ValidationIssue, ...] = ()
    warnings: tuple[ValidationIssue, ...] = ()
    ingest_queue: tuple[str, ...] = ()

    @property
    def n_errors(self) -> int:
        """The number of error-severity issues (the CLI exits 1 when this is > 0)."""
        return len(self.errors)

    @property
    def n_warnings(self) -> int:
        """The number of warning-severity issues."""
        return len(self.warnings)


class PartitionPreview(BaseModel):
    """One dirty-set partition in a `--dry-run` preview: its size + the first N sample paths.

    `count` is the whole partition's size. `sample` is the first `DRY_RUN_SAMPLE` or fewer paths,
    sorted, so the printed preview stays bounded on a large dirty set while still naming the files.
    The five partitions a preview reports are `added` / `removed` /
    `content_changed` / `inbound_closure` / `mirror_citers`.
    """

    model_config = ConfigDict(frozen=True)

    count: int = 0
    sample: tuple[str, ...] = ()


class DryRunPreview(BaseModel):
    """The `index --dry-run` dirty-set preview: what a real run WOULD recompute, written nowhere.

    Computed by re-hashing the knowledge base against the stored manifest and expanding the diff into the
    dirty set, with every write suppressed. The five partitions name the files a real incremental run
    would re-process. `pages_to_reembed` and `edges_to_rebuild` size the embed and edge work.
    `graph_refresh` is whether the AGE graph would be re-projected (true iff an edge would be inserted
    or a page removed). A cold start or a fingerprint mismatch previews the full rebuild (`would_full`
    true, every file in `content_changed`).
    """

    model_config = ConfigDict(frozen=True)

    would_full: bool = False
    added: PartitionPreview = PartitionPreview()
    removed: PartitionPreview = PartitionPreview()
    content_changed: PartitionPreview = PartitionPreview()
    inbound_closure: PartitionPreview = PartitionPreview()
    mirror_citers: PartitionPreview = PartitionPreview()
    pages_to_reembed: int = 0
    edges_to_rebuild: int = 0
    graph_refresh: bool = False


class CutPlanCounts(BaseModel):
    """What the chunk planner gave one index, each count over distinct heading section texts."""

    model_config = ConfigDict(frozen=True)

    reused: int = 0
    """The heading sections cut by a cut plan already stored, with no model call."""
    chosen: int = 0
    """The heading sections the chunk planner gave an accepted answer for, now stored as cut plans."""
    filled: int = 0
    """The heading sections the chunk planner gave no accepted answer for, because it failed, was late or was refused.

    They were filled and nothing was stored, so the next index that cuts them asks again.
    """


class IndexReport(BaseModel):
    """The full result of a rebuild: counts per stage, the validation summary, and duration.

    Returned by the handler and carried on `IndexingCompleted`. `mode` records which
    path produced it (`full` for the whole-knowledge-base rebuild, `incremental` for the
    dirty-set-only path), and `skipped` is the number of pages the manifest hit left
    untouched this run (`0` for a full rebuild). `dry_run` carries
    the dirty-set preview when the run was a `--dry-run` (nothing written), else `None`.
    """

    model_config = ConfigDict(frozen=True)

    pages: int = 0
    sections: int = 0
    claims: int = 0
    edges: int = 0
    pruned: int = 0
    embedded: int = 0
    reused: int = 0
    embedding_tokens: int = 0
    """The embedding provider's input token count for the vectors this run wrote."""
    skipped: int = 0
    mode: Literal["full", "incremental"] = "full"
    validation: ValidationReport = ValidationReport()
    standing_validation: ValidationReport | None = None
    """Every issue stored for the collection once the run committed, unchanged pages included, with an empty ingest queue.

    `None` when `validation` already covers every page, as after a full rebuild.
    """
    duration_seconds: float = 0.0
    dry_run: DryRunPreview | None = None
    cut_plans: CutPlanCounts = CutPlanCounts()
    """What the chunk planner gave this run's mirrors. All zero with no chunk planner."""


class IngestResult(BaseModel):
    """The result of an `IngestPage` write: the id it landed under and the rows it wrote.

    `edges` counts the resolved relations that became `kb.edges` rows. A link whose target is
    not already a stored page in this collection is recorded in `kb.link_refs` as dangling
    instead, which is where the ingest queue reads from. `embedded` and `reused` mean what they
    mean in the index report: `reused` is a row whose embed input was unchanged, so its vector stood.
    """

    model_config = ConfigDict(frozen=True)

    page_id: str
    collection: str
    sections: int = 0
    claims: int = 0
    edges: int = 0
    embedded: int = 0
    reused: int = 0


def page_type(frontmatter: BaseFrontmatter) -> str:
    """The `kb.pages.page_type` value for a note, its page `type`.

    Args:
        frontmatter: The note's frontmatter.

    Returns:
        The frontmatter's `type`.
    """
    return frontmatter.type
