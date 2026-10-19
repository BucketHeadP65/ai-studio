"""SQLAlchemy declarative mapping of the `kb.*` tables.

The alembic migrations own the DDL and these models only mirror it: names, PostgreSQL-native types, nullability,
server defaults and foreign keys. They are never used to create tables. `kb.embed_meta` has no primary key in the
DDL, so its mapper-level composite key over all three columns is never emitted, and it is written by delete-all
then insert.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from core.column_types import HALFVEC, Ltree
from pgvector.asyncpg import register_vector
from pydantic import JsonValue
from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, ForeignKeyConstraint, Integer, Text, UniqueConstraint, event, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from engram.db.contracts import KbBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "HALFVEC",
    "ClaimORM",
    "ClaimQualifierORM",
    "EdgeORM",
    "EmbedMetaORM",
    "IndexFingerprintORM",
    "IndexManifestORM",
    "LinkRefORM",
    "PageORM",
    "PageTypeContractORM",
    "QueryLogORM",
    "RawChunkORM",
    "SectionORM",
    "SourceRawORM",
    "UnitSummaryORM",
    "ValidationStoreORM",
    "VocabORM",
    "register_pgvector",
]

_SCHEMA = {"schema": "kb"}

# The dimensions of every stored embedding, one model and one size per database (`kb.embed_meta`).
EMBED_DIM = 1024


def _text_array() -> ARRAY[str]:
    """A `text[]` column type (PostgreSQL array of text)."""
    return ARRAY(Text())


class SourceRawORM(KbBase):
    """`kb.sources_raw`, immutable raw-mirror metadata, keyed by engram's own vendor-neutral id.

    Composite PK `(collection, source_id)`, the upsert conflict key. `provider` and `provider_key` name the
    connector a knowledge base declared and that host's own item key. Bibliographic details such as `doi` live
    in the `meta` jsonb. `UNIQUE (collection, provider, provider_key)` keeps one native item from being
    mirrored twice under different `source_id`s within the same collection.
    """

    __tablename__ = "sources_raw"
    __table_args__ = (UniqueConstraint("collection", "provider", "provider_key"), _SCHEMA)

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_key: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawChunkORM(KbBase):
    """`kb.raw_chunks`, raw fulltext cut into chunks at headings and to the chunk sizes, composite PK `(collection, id)`.

    `source_id` is containment (this row's own mirror, same knowledge base): the FK is the pair
    `(collection, source_id)` into `kb.sources_raw`, `ON DELETE CASCADE` kept.
    """

    __tablename__ = "raw_chunks"
    __table_args__ = (
        ForeignKeyConstraint(["collection", "source_id"], ["kb.sources_raw.collection", "kb.sources_raw.source_id"], ondelete="CASCADE"),
        _SCHEMA,
    )

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    section_path: Mapped[str | None] = mapped_column(Ltree)
    heading: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBED_DIM))


class PageORM(KbBase):
    """`kb.pages`, one row per memory page (all 8 page types), composite PK `(collection, id)`.

    `superseded_by` is containment (a page's own supersession stays in its knowledge base): the FK pair reuses
    this row's own `collection`, and it is checked when the transaction commits. `source_id` is also containment
    (a page's mirror lives in the same knowledge base): the FK pair is `(collection, source_id)` into `kb.sources_raw`.
    """

    __tablename__ = "pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["collection", "superseded_by"], ["kb.pages.collection", "kb.pages.id"], deferrable=True, initially="DEFERRED"
        ),
        ForeignKeyConstraint(["collection", "source_id"], ["kb.sources_raw.collection", "kb.sources_raw.source_id"]),
        _SCHEMA,
    )

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    page_type: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(_text_array(), nullable=False, server_default=text("'{}'"))
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_by: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(Text)
    source_hash: Mapped[str | None] = mapped_column(Text)
    facets: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    frontmatter: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    card_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created: Mapped[date | None] = mapped_column(Date)
    updated: Mapped[date | None] = mapped_column(Date)
    page_date: Mapped[date | None] = mapped_column(Date)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBED_DIM))


class SectionORM(KbBase):
    """`kb.sections`, one row per H2/H3 leaf section (the L2 read unit), composite PK `(collection, id)`.

    `page_id` is containment (a section belongs to its page, same knowledge base): the FK is the pair
    `(collection, page_id)` into `kb.pages`, `ON DELETE CASCADE` kept.
    """

    __tablename__ = "sections"
    __table_args__ = (
        ForeignKeyConstraint(["collection", "page_id"], ["kb.pages.collection", "kb.pages.id"], ondelete="CASCADE"),
        _SCHEMA,
    )

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    page_id: Mapped[str] = mapped_column(Text, nullable=False)
    canon: Mapped[str | None] = mapped_column(Text)
    heading: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Ltree, nullable=False)
    ord: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int | None] = mapped_column(Integer)
    body_hash: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBED_DIM))


class ClaimORM(KbBase):
    """`kb.claims`, one row per `^cN` claim block under `## Findings`, composite PK `(collection, id)`.

    `page_id` is containment (CASCADE, same pair as `SectionORM.page_id`). `subject_id` is a
    citation (what a claim is about may live in another knowledge base), so it carries its own
    `subject_collection` and a null-pairing CHECK (`(subject_id IS NULL) = (subject_collection IS
    NULL)`, Postgres does not enforce a composite FK when either column is NULL).
    """

    __tablename__ = "claims"
    __table_args__ = (
        ForeignKeyConstraint(["collection", "page_id"], ["kb.pages.collection", "kb.pages.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["subject_collection", "subject_id"], ["kb.pages.collection", "kb.pages.id"]),
        CheckConstraint("(subject_id IS NULL) = (subject_collection IS NULL)", name="claims_subject_pairing_check"),
        _SCHEMA,
    )

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    page_id: Mapped[str] = mapped_column(Text, nullable=False)
    block_id: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[str | None] = mapped_column(Text)
    loc: Mapped[str | None] = mapped_column(Text)
    loc_path: Mapped[str | None] = mapped_column(Ltree)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_id: Mapped[str | None] = mapped_column(Text)
    subject_collection: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, server_default="{}")
    """The claim's declared text fields by field name, each value as written."""
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    embedding: Mapped[list[float] | None] = mapped_column(HALFVEC(EMBED_DIM))


class ClaimQualifierORM(KbBase):
    """`kb.claim_qualifiers`, one qualifier target per row, named by the claim-grammar field.

    Composite PK `(collection, claim_id, qualifier, ord)`: a qualifier with several targets writes one row
    per target, with `ord` keeping the authored order. `claim_id` is containment, the FK pair
    `(collection, claim_id)` into `kb.claims` with `ON DELETE CASCADE`. `target_id` is a citation, the FK pair
    `(target_collection, target_id)` into `kb.pages` with no cascade. `target_collection` is this row's own
    collection.
    """

    __tablename__ = "claim_qualifiers"
    __table_args__ = (
        ForeignKeyConstraint(["collection", "claim_id"], ["kb.claims.collection", "kb.claims.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["target_collection", "target_id"], ["kb.pages.collection", "kb.pages.id"]),
        _SCHEMA,
    )

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    claim_id: Mapped[str] = mapped_column(Text, primary_key=True)
    qualifier: Mapped[str] = mapped_column(Text, primary_key=True)
    ord: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_collection: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)


class EdgeORM(KbBase):
    """`kb.edges`, every typed relation, resolved to ids (`bigserial` id), still FK-less.

    `src_collection` and `dst_collection` are NOT NULL: every edge names both endpoints' collections, today
    the indexing collection. There is no FK. `kb.edges` holds resolved relations only and is rebuilt
    wholesale on every index.
    """

    __tablename__ = "edges"
    __table_args__ = _SCHEMA

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    src_collection: Mapped[str] = mapped_column(Text, nullable=False)
    src_id: Mapped[str] = mapped_column(Text, nullable=False)
    src_kind: Mapped[str] = mapped_column(Text, nullable=False)
    rel: Mapped[str] = mapped_column(Text, nullable=False)
    dst_collection: Mapped[str] = mapped_column(Text, nullable=False)
    dst_id: Mapped[str] = mapped_column(Text, nullable=False)
    dst_kind: Mapped[str] = mapped_column(Text, nullable=False)
    qualifiers: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    provenance_claim: Mapped[str | None] = mapped_column(Text)



class VocabORM(KbBase):
    """`kb.vocab`, vocabularies indexed to data (schema-as-data), composite PK `(collection, vocab, value)`."""

    __tablename__ = "vocab"
    __table_args__ = _SCHEMA

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    vocab: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, primary_key=True)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))


class EmbedMetaORM(KbBase):
    """`kb.embed_meta`, the single-row embedding-model contract.

    The DDL declares no primary key. The composite mapper PK below is never emitted as DDL.
    """

    __tablename__ = "embed_meta"
    __table_args__ = _SCHEMA

    model: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    embedded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False)


class QueryLogORM(KbBase):
    """`kb.query_log`, session telemetry, never indexed from the knowledge base."""

    __tablename__ = "query_log"
    __table_args__ = _SCHEMA

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    asked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    agent: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    archetype: Mapped[str | None] = mapped_column(Text)
    surfaces: Mapped[list[str]] = mapped_column(_text_array(), nullable=False, server_default=text("'{}'"))
    cited_ids: Mapped[list[str]] = mapped_column(_text_array(), nullable=False, server_default=text("'{}'"))
    tokens_loaded: Mapped[int | None] = mapped_column(Integer)
    wrote_back: Mapped[str | None] = mapped_column(Text)


class IndexManifestORM(KbBase):
    """`kb.index_manifest`, one row per indexed file, the change detector (composite PK).

    Maps each indexed file `path` to its raw `content_hash` and resolution `identity_hash`
    (plus the `page_id` / `source_key` it produced), so re-hashing all files and diffing yields
    the `{added, removed, content-changed}` partition without parsing.
    """

    __tablename__ = "index_manifest"
    __table_args__ = _SCHEMA

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    identity_hash: Mapped[str] = mapped_column(Text, nullable=False)
    page_id: Mapped[str | None] = mapped_column(Text)
    source_key: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


class LinkRefORM(KbBase):
    """`kb.link_refs`, every outbound link target (resolved + dangling), `bigserial` id.

    Keyed by the lower-cased `raw_target` so the inbound closure is a name query
    (`WHERE raw_target = :name`), which `kb.edges` (resolved-only, id-keyed) cannot answer.
    `src_page_id` is containment, the FK pair `(collection, src_page_id)` into `kb.pages` with
    `ON DELETE CASCADE`, so deleting a page clears its outbound refs. `dst_id` is a citation with no FK,
    since it may be dangling. `dst_collection` names its collection, with a null-pairing CHECK
    (`(dst_id IS NULL) = (dst_collection IS NULL)`).
    """

    __tablename__ = "link_refs"
    __table_args__ = (
        ForeignKeyConstraint(["collection", "src_page_id"], ["kb.pages.collection", "kb.pages.id"], ondelete="CASCADE"),
        CheckConstraint("(dst_id IS NULL) = (dst_collection IS NULL)", name="link_refs_dst_pairing_check"),
        _SCHEMA,
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    src_page_id: Mapped[str] = mapped_column(Text, nullable=False)
    src_kind: Mapped[str] = mapped_column(Text, nullable=False)
    rel: Mapped[str] = mapped_column(Text, nullable=False)
    raw_target: Mapped[str] = mapped_column(Text, nullable=False)
    dst_id: Mapped[str | None] = mapped_column(Text)
    dst_collection: Mapped[str | None] = mapped_column(Text)
    provenance_claim: Mapped[str | None] = mapped_column(Text)


class ValidationStoreORM(KbBase):
    """`kb.validation_store`, persisted per-page validation issues for whole-knowledge-base report reassembly (`bigserial` id).

    `page_id` is not a FK: a parse-error issue is keyed by file path, with no page row behind it.
    """

    __tablename__ = "validation_store"
    __table_args__ = _SCHEMA

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    page_id: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    rule: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    anchor: Mapped[str | None] = mapped_column(Text)


class IndexFingerprintORM(KbBase):
    """`kb.index_fingerprint`, the single per-collection global-invalidator row (PK `collection`).

    A mismatch on `indexer_version`, `spec_hash`, `vocabulary_hash` or `embed_model` forces a full rebuild.
    `switches` carries the retrieval switches decided by an index or by a page write (`RetrievalSwitches`), served to readers
    only through `api.retrieval_switches`. This table itself is ungranted. `spec` carries the validated
    `KnowledgeBaseSchema` the index ran with, as JSON. It is null on a row a page write created, until the
    collection is indexed.
    """

    __tablename__ = "index_fingerprint"
    __table_args__ = _SCHEMA

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    indexer_version: Mapped[str] = mapped_column(Text, nullable=False)
    spec_hash: Mapped[str] = mapped_column(Text, nullable=False)
    vocabulary_hash: Mapped[str] = mapped_column(Text, nullable=False)
    embed_model: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    knowledge_base_root: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    switches: Mapped[dict[str, bool]] = mapped_column(JSONB, nullable=False, default=dict)
    spec: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)


class PageTypeContractORM(KbBase):
    """`kb.page_type_contracts`, one row per declared page type, composite PK `(collection, page_type)`.

    `contract` carries the page type's schema entry as-is (`PageTypeSpec.model_dump()`). Every index upserts
    one row per declared page type and deletes the rows of page types no longer declared. The table is
    ungranted and served to readers only through `api.page_types`.
    """

    __tablename__ = "page_type_contracts"
    __table_args__ = _SCHEMA

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    page_type: Mapped[str] = mapped_column(Text, primary_key=True)
    contract: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, nullable=False)


class CutPlanORM(KbBase):
    """`kb.cut_plans`, one cut plan per text and chunk planner, composite PK `(collection, text_hash, planner)`.

    `text_hash` is the sha256 of the heading section text the plan cuts, `planner` the cut plan identity (the chunk
    planner's identity with the paragraph numbering version), and `starts` the paragraph numbers where a new chunk starts, paragraph 1 left out. A plan is written once and
    never changed. The table is ungranted.
    """

    __tablename__ = "cut_plans"
    __table_args__ = _SCHEMA

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    text_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    planner: Mapped[str] = mapped_column(Text, primary_key=True)
    starts: Mapped[list[int]] = mapped_column(ARRAY(Integer), nullable=False)


class UnitSummaryORM(KbBase):
    """`kb.unit_summaries`, indexed disclosure summary for a page or section, composite PK `(collection, unit_id)`.

    A summary starts at `status='pending'`, the DDL default, and stays there until it is promoted or
    rejected. `model` and `source_hash` record which model wrote it from which content.
    """

    __tablename__ = "unit_summaries"
    __table_args__ = _SCHEMA

    collection: Mapped[str] = mapped_column(Text, primary_key=True)
    unit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    unit_kind: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    model: Mapped[str] = mapped_column(Text, nullable=False)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))


async def register_pgvector(engine: AsyncEngine) -> None:
    """Register the pgvector binary codec on every asyncpg connection of `engine`.

    You must call it before reading or writing a `HALFVEC` column. Each pooled connection learns the codec
    once, when it is created. It opens no connection itself, so an unreachable database fails only on first use.
    Calling it twice registers the listener once.

    Args:
        engine: The async engine whose connections need the codec.
    """
    if event.contains(engine.sync_engine, "connect", _register_vector_on_connect):
        return
    event.listen(engine.sync_engine, "connect", _register_vector_on_connect)


def _register_vector_on_connect(dbapi_connection: object, connection_record: object) -> None:
    """Register the pgvector codec on a new connection, through the asyncpg dialect's `run_async`."""
    del connection_record
    dbapi_connection.run_async(register_vector)  # type: ignore[attr-defined]
