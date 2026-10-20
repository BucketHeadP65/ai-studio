"""Repositories over the `kb.*` tables that the indexing slice writes.

Every mutating method flushes and never commits. The unit of work that shares the session owns the transaction.
Every prune, delete and embed queue is scoped to one `collection`, so several knowledge bases can share one database.
An upsert of an embedded table sets `embedding` to NULL only when the embed input changed, so the embed queue is every row whose `embedding IS NULL`.
A filter on a set of ids, names or paths binds the set as one `text[]` parameter, so no set size passes asyncpg's bind parameter limit.
The `*_statement` factories build each statement without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar, Literal

from core.db import batches
from sqlalchemy import CursorResult, Text, bindparam, case, delete, exists, func, null, select, text, union, update
from sqlalchemy.dialects.postgresql import ARRAY, Insert, insert
from sqlalchemy.orm.util import identity_key

from engram.db.contracts import KbBase
from engram.indexing.domain.manifest import FileType, ManifestEntry
from engram.indexing.domain.models import ValidationIssue, ValidationReport
from engram.indexing.domain.switches import GRAPH_OFF_EDGES_PER_NOTE, RetrievalSwitches, Signals
from engram.indexing.infrastructure.orm import (
    ClaimORM,
    ClaimQualifierORM,
    EdgeORM,
    EmbedMetaORM,
    IndexFingerprintORM,
    IndexManifestORM,
    LinkRefORM,
    PageORM,
    PageTypeContractORM,
    RawChunkORM,
    SectionORM,
    SourceRawORM,
    UnitSummaryORM,
    ValidationStoreORM,
    VocabORM,
)
from engram.indexing.read_models import PageKeyRowResult, PendingEmbedChunk, PendingEmbedRow, StoredFingerprint

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from core.ai.retrieval.contracts import Row
    from sqlalchemy import BindParameter, ColumnElement, CompoundSelect, Delete, Result, Select, Update
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import QueryableAttribute


def _utc_now() -> datetime:
    """The current time in UTC."""
    return datetime.now(tz=UTC)


def _deleted_count(result: Result[*tuple[object, ...]]) -> int:
    """The number of rows a `DELETE` removed, or 0 for a result with no `rowcount`."""
    return result.rowcount if isinstance(result, CursorResult) else 0


def _text_array(values: Sequence[str]) -> BindParameter[Sequence[str]]:
    """One `text[]` bind parameter holding `values` in the order given."""
    return bindparam(None, list(values), type_=ARRAY(Text))


# `= ANY(:values)` and `<> ALL(:values)` read the whole array for every row once Postgres caches a generic plan.
# A join over `unnest(:values)` hashes the array instead.
def _in_values(column: ColumnElement[str] | QueryableAttribute[str], values: Iterable[str]) -> ColumnElement[bool]:
    """`column IN (SELECT unnest(:values))`, binding one parameter however many values there are."""
    return column.in_(select(func.unnest(_text_array(sorted(values)))))


def _not_in_values(column: ColumnElement[str] | QueryableAttribute[str], values: Iterable[str]) -> ColumnElement[bool]:
    """`NOT EXISTS (SELECT * FROM unnest(:values) WHERE value = column)`, binding one parameter however many values there are."""
    listed = func.unnest(_text_array(sorted(values))).table_valued("value").render_derived(name="listed")
    return ~exists().where(column == listed.c.value)


def _upsert_statement(model: type[KbBase], key: tuple[str, ...], rows: Sequence[Row], *, embed_gate: str | None = None) -> Insert:
    """An `INSERT … ON CONFLICT (<key>) DO UPDATE` that sets `embedding` to NULL only when the `embed_gate` column changed."""
    stmt = insert(model).values(list(rows))
    keys = set(key)
    if embed_gate is not None:
        keys.add("embedding")
    set_: dict[str, object] = {c.name: stmt.excluded[c.name] for c in stmt.table.columns if c.name not in keys}
    if embed_gate is not None:
        changed = stmt.excluded[embed_gate].is_distinct_from(stmt.table.c[embed_gate])
        set_["embedding"] = case((changed, null()), else_=stmt.table.c.embedding)
    return stmt.on_conflict_do_update(index_elements=list(key), set_=set_)


def _embed_write_rows(vectors: Mapping[str, Sequence[float]], *, collection: str) -> list[Row]:
    """The `(collection, id, embedding)` parameters of a bulk `UPDATE` that writes embeddings."""
    return [{"collection": collection, "id": row_id, "embedding": list(vector)} for row_id, vector in vectors.items()]


class _IdRepository[OrmT: KbBase]:
    """Upsert, prune and embed queue for a table keyed by `(collection, id)`."""

    _model: type[OrmT]
    _embed_gate: ClassVar[str | None] = None

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # -- statement factories (built without a database) --------------------

    @classmethod
    def upsert_statement(cls, rows: Sequence[Row]) -> Insert:
        """`INSERT … ON CONFLICT (collection, id) DO UPDATE` for this table, embed-gated on `_embed_gate`."""
        return _upsert_statement(cls._model, ("collection", "id"), rows, embed_gate=cls._embed_gate)

    @classmethod
    def needing_embedding_statement(cls, collection: str) -> Select:
        """`SELECT * … WHERE embedding IS NULL AND collection = :col`, the embed queue of one collection."""
        return select(cls._model).where(cls._model.__table__.c.embedding.is_(None)).where(cls._model.__table__.c.collection == collection)

    # -- async surface (flush only, never commit) ----------------------------

    async def upsert_many(self, rows: Sequence[Row]) -> None:
        """Bulk upsert `rows` by id and expire any loaded instance of them, a no-op on an empty batch."""
        if not rows:
            return
        for batch in batches(rows):
            await self._db.execute(self.upsert_statement(batch))
        await self._db.flush()
        self._expire_upserted(rows)

    def _expire_upserted(self, rows: Sequence[Row]) -> None:
        """Expire the session's loaded instance of each upserted row so the next read reloads it."""
        for row in rows:
            row_collection = row.get("collection")
            row_id = row.get("id")
            if row_collection is None or row_id is None:
                continue
            instance = self._db.identity_map.get(identity_key(self._model, (row_collection, row_id)), None)
            if instance is not None:
                self._db.expire(instance)

    async def prune_missing(self, keep_ids: set[str], *, collection: str) -> int:
        """Delete `collection`'s rows whose id is not in `keep_ids` and return how many were removed."""
        result = await self._db.execute(self._prune_scoped_statement(keep_ids, collection))
        await self._db.flush()
        return _deleted_count(result)

    def _prune_scoped_statement(self, keep_ids: set[str], collection: str) -> Delete:
        """The `DELETE` of `collection`'s rows whose id is not in `keep_ids`."""
        stmt = delete(self._model).where(self._model.__table__.c.collection == collection)
        if keep_ids:
            stmt = stmt.where(_not_in_values(self._model.__table__.c.id, keep_ids))
        return stmt

    async def rows_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedRow]:
        """Return `collection`'s rows whose `embedding IS NULL`."""
        statement = self.needing_embedding_statement(collection).with_only_columns(self._model.__table__.c.id)
        result = await self._db.execute(statement)
        return [PendingEmbedRow(id=row_id) for row_id in result.scalars().all()]

    async def write_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None:
        """Set each row's `embedding` by `(collection, id)` and touch no other column, a no-op on an empty mapping."""
        if not vectors:
            return
        await self._db.execute(update(self._model), _embed_write_rows(vectors, collection=collection))
        await self._db.flush()

    async def flush(self) -> None:
        """Flush pending state to the connection without committing."""
        await self._db.flush()


class Pages(_IdRepository[PageORM]):
    """`kb.pages`, upsert by `(collection, id)` with re-embedding gated on `body_hash`."""

    _model = PageORM
    _embed_gate = "body_hash"

    @classmethod
    def needing_embedding_statement(cls, collection: str) -> Select:
        """`SELECT * FROM kb.pages WHERE embedding IS NULL AND collection = :col`.

        Args:
            collection: The collection to read.

        Returns:
            The select of the pages waiting for an embedding.
        """
        return select(cls._model).where(cls._model.__table__.c.embedding.is_(None)).where(cls._model.__table__.c.collection == collection)

    def _prune_scoped_statement(self, keep_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.pages WHERE collection = :col AND id NOT IN keep_ids`."""
        stmt = delete(PageORM).where(PageORM.__table__.c.collection == collection)
        if keep_ids:
            stmt = stmt.where(_not_in_values(PageORM.__table__.c.id, keep_ids))
        return stmt

    @staticmethod
    def prune_claims_statement(keep_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.claims WHERE collection = :col AND page_id NOT IN keep_ids`, the claims of the pages a prune removes.

        Args:
            keep_ids: The page ids the prune keeps.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        stmt = delete(ClaimORM).where(ClaimORM.__table__.c.collection == collection)
        if keep_ids:
            stmt = stmt.where(_not_in_values(ClaimORM.__table__.c.page_id, keep_ids))
        return stmt

    @staticmethod
    def clear_superseded_by_statement(keep_ids: set[str], collection: str) -> Update:
        """`UPDATE kb.pages SET superseded_by = NULL WHERE collection = :col AND superseded_by NOT IN keep_ids`.

        Args:
            keep_ids: The page ids the prune keeps.
            collection: The collection the pages belong to.

        Returns:
            The update statement, which unsets every `superseded_by` naming a page the prune removes.
        """
        page = PageORM.__table__.c
        stmt = update(PageORM).where(page.collection == collection, page.superseded_by.is_not(None))
        if keep_ids:
            stmt = stmt.where(_not_in_values(page.superseded_by, keep_ids))
        return stmt.values(superseded_by=None)

    async def prune_missing(self, keep_ids: set[str], *, collection: str) -> int:
        """Delete `collection`'s pages whose id is not in `keep_ids`, their claims first, and return how many pages were removed.

        A claim cites a page through its subject and its qualifiers with no cascade, so the claims of the pages removed
        go first, their qualifiers with them, then the pages. A page a kept claim still cites is refused by the database.
        A kept page whose `superseded_by` names a removed page keeps its row with `superseded_by` unset.

        Args:
            keep_ids: The page ids to keep.
            collection: The collection to prune.

        Returns:
            How many pages were removed.
        """
        await self._db.execute(self.prune_claims_statement(keep_ids, collection))
        await self._db.execute(self.clear_superseded_by_statement(keep_ids, collection))
        return await super().prune_missing(keep_ids, collection=collection)

    @staticmethod
    def delete_claims_by_ids_statement(ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.claims WHERE collection = :col AND page_id IN ids`, the claims of the pages a delete removes.

        Args:
            ids: The page ids to delete.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        return delete(ClaimORM).where(ClaimORM.__table__.c.collection == collection, _in_values(ClaimORM.__table__.c.page_id, ids))

    @staticmethod
    def clear_superseded_by_ids_statement(ids: set[str], collection: str) -> Update:
        """`UPDATE kb.pages SET superseded_by = NULL WHERE collection = :col AND superseded_by IN ids`.

        Args:
            ids: The page ids a delete removes.
            collection: The collection the pages belong to.

        Returns:
            The update statement, which unsets every `superseded_by` naming a page the delete removes.
        """
        page = PageORM.__table__.c
        return update(PageORM).where(page.collection == collection, _in_values(page.superseded_by, ids)).values(superseded_by=None)

    @staticmethod
    def delete_by_ids_statement(ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.pages WHERE collection = :col AND id IN ids`.

        The delete cascades to the pages' `sections` and `claims`.

        Args:
            ids: The page ids to delete.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        return delete(PageORM).where(PageORM.__table__.c.collection == collection, _in_values(PageORM.__table__.c.id, ids))

    async def delete_by_ids(self, ids: set[str], *, collection: str) -> None:
        """Delete the pages whose id is in `ids`, their claims first, a no-op on an empty set.

        A claim cites a page through its subject and its qualifiers with no cascade, so the pages' own claims go first,
        their qualifiers with them, then the pages. A page a kept claim still cites is refused by the database.
        A kept page whose `superseded_by` names a deleted page keeps its row with `superseded_by` unset.

        Args:
            ids: The page ids to delete.
            collection: The collection the pages belong to.
        """
        if not ids:
            return
        await self._db.execute(self.delete_claims_by_ids_statement(ids, collection))
        await self._db.execute(self.clear_superseded_by_ids_statement(ids, collection))
        await self._db.execute(self.delete_by_ids_statement(ids, collection))
        await self._db.flush()

    @staticmethod
    def key_rows_statement(collection: str) -> Select:
        """`SELECT id, slug, title, aliases, source_id FROM kb.pages WHERE collection = :col`.

        The file path is not on `kb.pages`. It comes from the index manifest.

        Args:
            collection: The collection to read.

        Returns:
            The select of every page's resolution keys and cited source.
        """
        page = PageORM.__table__.c
        return select(page.id, page.slug, page.title, page.aliases, page.source_id).where(page.collection == collection)

    async def key_rows(self, collection: str) -> Sequence[PageKeyRowResult]:
        """Return every page's id, slug, title, aliases and source id.

        Args:
            collection: The collection to read.

        Returns:
            One key row per page.
        """
        result = await self._db.execute(self.key_rows_statement(collection))
        return [
            PageKeyRowResult(id=row.id, slug=row.slug, title=row.title, aliases=tuple(row.aliases or ()), source_id=row.source_id)
            for row in result.all()
        ]


class _PageChildRepository[OrmT: KbBase](_IdRepository[OrmT]):
    """A table keyed by `(collection, id)` whose rows a page owns through `page_id`."""

    @classmethod
    def prune_for_pages_statement(cls, keep_ids: set[str], page_ids: set[str], collection: str) -> Delete:
        """`DELETE … WHERE collection = :col AND page_id IN page_ids [AND id NOT IN keep_ids]`, with sorted `IN` targets."""
        child = cls._model.__table__.c
        stmt = delete(cls._model).where(child.collection == collection, _in_values(child.page_id, page_ids))
        if keep_ids:
            stmt = stmt.where(_not_in_values(child.id, keep_ids))
        return stmt

    async def prune_for_pages(self, keep_ids: set[str], page_ids: set[str], *, collection: str) -> int:
        """Delete the rows of `page_ids` absent from `keep_ids` and return how many were removed, 0 for no pages."""
        if not page_ids:
            return 0
        result = await self._db.execute(self.prune_for_pages_statement(keep_ids, page_ids, collection))
        await self._db.flush()
        return _deleted_count(result)


class Sections(_PageChildRepository[SectionORM]):
    """`kb.sections`, upsert by `(collection, id)` with re-embedding gated on `body_hash`."""

    _model = SectionORM
    _embed_gate = "body_hash"


class Claims(_PageChildRepository[ClaimORM]):
    """`kb.claims`, upsert by `(collection, id)` with re-embedding gated on the claim `text`."""

    _model = ClaimORM
    _embed_gate = "text"



class UnitSummaries:
    """`kb.unit_summaries`, keyed `(collection, unit_id)`, where the index only deletes and never writes.

    A summary whose page or section is gone describes nothing, and no foreign key removes it.
    Every delete here is scoped to one collection.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def removed_section_ids_statement(keep_section_ids: set[str], page_ids: set[str], collection: str) -> Select:
        """`SELECT id FROM kb.sections WHERE collection = :col AND page_id IN page_ids [AND id NOT IN keep]`.

        Run it before the section prune deletes those rows.

        Args:
            keep_section_ids: The section ids the fresh load produced.
            page_ids: The pages whose sections are read.
            collection: The collection to read.

        Returns:
            The select of the section ids that are going away.
        """
        section = SectionORM.__table__.c
        stmt = select(section.id).where(section.collection == collection, _in_values(section.page_id, page_ids))
        if keep_section_ids:
            stmt = stmt.where(_not_in_values(section.id, keep_section_ids))
        return stmt

    @staticmethod
    def delete_units_statement(unit_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.unit_summaries WHERE collection = :col AND unit_id IN unit_ids`.

        Args:
            unit_ids: The page and section ids whose summaries are deleted.
            collection: The collection to delete from.

        Returns:
            The delete statement.
        """
        summary = UnitSummaryORM.__table__.c
        return delete(UnitSummaryORM).where(summary.collection == collection, _in_values(summary.unit_id, unit_ids))

    @staticmethod
    def prune_statement(keep_unit_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.unit_summaries WHERE collection = :col [AND unit_id NOT IN keep_unit_ids]`.

        Use it only after a full rebuild, whose pages and sections are every unit a summary may describe.

        Args:
            keep_unit_ids: The page and section ids to keep. Empty deletes every summary of the collection.
            collection: The collection to prune.

        Returns:
            The delete statement.
        """
        summary = UnitSummaryORM.__table__.c
        stmt = delete(UnitSummaryORM).where(summary.collection == collection)
        if keep_unit_ids:
            stmt = stmt.where(_not_in_values(summary.unit_id, keep_unit_ids))
        return stmt

    # -- async surface (flush only, never commit) ----------------------------

    async def prune_for_pages(
        self, keep_section_ids: set[str], dirty_page_ids: set[str], deleted_page_ids: set[str], *, collection: str
    ) -> int:
        """Delete the summaries of the rebuilt pages' removed sections and of the deleted pages.

        You must call this before the sections are deleted, because it reads the stored sections.
        A surviving section's summary stays, even when its body changed.
        An empty page set is a no-op returning 0.

        Args:
            keep_section_ids: The section ids the fresh load produced.
            dirty_page_ids: The pages this index rebuilt.
            deleted_page_ids: The pages this index deleted. Their own page summaries go too.
            collection: The collection to prune.

        Returns:
            The number of rows removed.
        """
        owning_pages = dirty_page_ids | deleted_page_ids
        if not owning_pages:
            return 0
        removed = await self._db.execute(self.removed_section_ids_statement(keep_section_ids, owning_pages, collection))
        gone_units = set(removed.scalars().all()) | deleted_page_ids
        if not gone_units:
            return 0
        result = await self._db.execute(self.delete_units_statement(gone_units, collection))
        await self._db.flush()
        return _deleted_count(result)

    async def prune_missing(self, keep_unit_ids: set[str], *, collection: str) -> int:
        """Delete `collection`'s summaries whose unit is not in `keep_unit_ids`.

        Args:
            keep_unit_ids: The page and section ids to keep.
            collection: The collection to prune.

        Returns:
            The number of rows removed.
        """
        result = await self._db.execute(self.prune_statement(keep_unit_ids, collection))
        await self._db.flush()
        return _deleted_count(result)


class VocabEntries:
    """`kb.vocab`, upsert on the `(collection, vocab, value)` key, with the prune scoped to `collection`."""

    _KEY = ("collection", "vocab", "value")

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    @classmethod
    def upsert_statement(cls, rows: Sequence[Row]) -> Insert:
        """`INSERT … ON CONFLICT (collection, vocab, value) DO UPDATE`.

        Args:
            rows: The vocabulary rows to write.

        Returns:
            The upsert statement.
        """
        return _upsert_statement(VocabORM, cls._KEY, rows)

    @classmethod
    def prune_statement(cls, keep: set[tuple[str, str]], collection: str) -> Delete:
        """`DELETE FROM kb.vocab WHERE collection = :col AND (vocab, value) NOT IN keep`.

        Args:
            keep: The `(vocab, value)` pairs to keep. Empty deletes every row of the collection.
            collection: The collection to prune.

        Returns:
            The delete statement.
        """
        stmt = delete(VocabORM).where(VocabORM.collection == collection)
        if keep:
            ordered = sorted(keep)
            pairs = func.unnest(_text_array([vocab for vocab, _ in ordered]), _text_array([value for _, value in ordered]))
            listed = pairs.table_valued("vocab", "value").render_derived(name="listed")
            stmt = stmt.where(~exists().where(VocabORM.vocab == listed.c.vocab, VocabORM.value == listed.c.value))
        return stmt

    async def upsert_many(self, rows: Sequence[Row]) -> None:
        """Bulk upsert vocabulary rows by their full key, a no-op on an empty batch.

        Args:
            rows: The vocabulary rows to write.
        """
        if not rows:
            return
        for batch in batches(rows):
            await self._db.execute(self.upsert_statement(batch))
        await self._db.flush()

    async def prune_missing(self, keep: set[tuple[str, str]], *, collection: str) -> int:
        """Delete `collection`'s vocabulary rows whose `(vocab, value)` pair is not in `keep`.

        Args:
            keep: The `(vocab, value)` pairs to keep.
            collection: The collection to prune.

        Returns:
            The number of rows removed.
        """
        result = await self._db.execute(self.prune_statement(keep, collection))
        await self._db.flush()
        return _deleted_count(result)

    async def all(self) -> Sequence[VocabORM]:
        """Return every vocabulary row of every collection, for tests and inspection.

        Returns:
            The vocabulary rows.
        """
        result = await self._db.execute(select(VocabORM))
        return list(result.scalars().all())


class PageTypeContracts:
    """`kb.page_type_contracts`, upsert on the `(collection, page_type)` key, with the prune scoped to `collection`.

    Every index writes one row per page type of the schema, full and incremental alike.
    """

    _KEY = ("collection", "page_type")

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    @classmethod
    def upsert_statement(cls, rows: Sequence[Row]) -> Insert:
        """`INSERT … ON CONFLICT (collection, page_type) DO UPDATE`.

        Args:
            rows: The contract rows to write.

        Returns:
            The upsert statement.
        """
        return _upsert_statement(PageTypeContractORM, cls._KEY, rows)

    @classmethod
    def prune_statement(cls, keep: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.page_type_contracts WHERE collection = :col AND page_type NOT IN keep`.

        Args:
            keep: The page types to keep. Empty deletes every row of the collection.
            collection: The collection to prune.

        Returns:
            The delete statement.
        """
        stmt = delete(PageTypeContractORM).where(PageTypeContractORM.collection == collection)
        if keep:
            stmt = stmt.where(_not_in_values(PageTypeContractORM.page_type, keep))
        return stmt

    async def upsert_many(self, rows: Sequence[Row]) -> None:
        """Bulk upsert page type contract rows by their full key, a no-op on an empty batch.

        Args:
            rows: The contract rows to write.
        """
        if not rows:
            return
        for batch in batches(rows):
            await self._db.execute(self.upsert_statement(batch))
        await self._db.flush()

    async def prune_missing(self, keep: set[str], *, collection: str) -> int:
        """Delete `collection`'s contract rows for page types not in `keep`.

        Args:
            keep: The page types to keep.
            collection: The collection to prune.

        Returns:
            The number of rows removed.
        """
        result = await self._db.execute(self.prune_statement(keep, collection))
        await self._db.flush()
        return _deleted_count(result)


class Edges:
    """`kb.edges`, whose rows are replaced as a set and never upserted.

    An edge has a generated id and no stable key, so a collection's edges, or one page's, are deleted and inserted again.
    Every delete filters on the row's own `src_collection`.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    @staticmethod
    def delete_collection_statement(collection: str) -> Delete:
        """`DELETE FROM kb.edges WHERE src_collection = :col`.

        Args:
            collection: The collection whose edges are deleted.

        Returns:
            The delete statement.
        """
        return delete(EdgeORM).where(EdgeORM.src_collection == collection)

    @staticmethod
    def delete_for_sources_statement(src_page_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.edges WHERE split_part(src_id,'#',1) IN src_page_ids AND src_collection = :col`.

        A claim or section edge's `src_id` is `<page_id>#<anchor>`, so the match reads the part before `#`.
        The delete removes a page's claim and section edges along with its page edges.

        Args:
            src_page_ids: The page ids whose edges are deleted.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        owning_page = func.split_part(EdgeORM.src_id, "#", 1)
        return delete(EdgeORM).where(_in_values(owning_page, src_page_ids), EdgeORM.src_collection == collection)

    @staticmethod
    def insert_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT INTO kb.edges` for `rows`, with the ids generated by the database.

        Args:
            rows: The edge rows to insert.

        Returns:
            The insert statement.
        """
        return insert(EdgeORM).values(list(rows))

    async def replace_all(self, rows: Sequence[Row], *, collection: str) -> None:
        """Delete `collection`'s edges, then insert `rows`, in the caller's transaction.

        Other collections' edges are never touched.

        Args:
            rows: The edge rows to insert.
            collection: The collection whose edges are replaced.
        """
        await self._db.execute(self.delete_collection_statement(collection))
        if rows:
            for batch in batches(rows):
                await self._db.execute(self.insert_statement(batch))
        await self._db.flush()

    async def replace_for_sources(self, rows: Sequence[Row], src_page_ids: set[str], *, collection: str) -> int:
        """Replace the edges owned by `src_page_ids` with `rows`, in the caller's transaction.

        The delete matches each edge's owning page, so a page's claim and section edges go with its page edges.

        Args:
            rows: The fresh edge rows to insert.
            src_page_ids: The changed and deleted page ids whose edges are deleted.
            collection: The collection the pages belong to.

        Returns:
            The number of edges deleted plus the number inserted. Zero means the graph needs no refresh.
        """
        deleted = 0
        if src_page_ids:
            result = await self._db.execute(self.delete_for_sources_statement(src_page_ids, collection))
            deleted = _deleted_count(result)
        if rows:
            for batch in batches(rows):
                await self._db.execute(self.insert_statement(batch))
        await self._db.flush()
        return deleted + len(rows)

    async def all(self) -> Sequence[EdgeORM]:
        """Return every edge of every collection, for tests and inspection.

        Returns:
            The edge rows.
        """
        result = await self._db.execute(select(EdgeORM))
        return list(result.scalars().all())

    async def refresh_graph(self) -> None:
        """Project the link graph again from the current edges by calling `kb.refresh_graph()`.

        It is a no-op under the recursive CTE backend and a full `kb_graph` rebuild under Apache AGE.
        Call it after replacing edges, so the `api.graph_*` functions read the current edges.
        """
        await self._db.execute(text("SELECT kb.refresh_graph()"))


class SourceRepository:
    """`kb.sources_raw` and `kb.raw_chunks`, the source mirrors and their chunks.

    Sources key on `(collection, source_id)` and chunks on `(collection, id)`.
    A chunk is embedded again when its `body` changes.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    # sources_raw ------------------------------------------------------------

    @staticmethod
    def upsert_sources_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT … ON CONFLICT (collection, source_id) DO UPDATE` for `kb.sources_raw`.

        Args:
            rows: The source rows to write.

        Returns:
            The upsert statement.
        """
        stmt = insert(SourceRawORM).values(list(rows))
        set_ = {c.name: stmt.excluded[c.name] for c in stmt.table.columns if c.name not in {"collection", "source_id"}}
        return stmt.on_conflict_do_update(index_elements=["collection", "source_id"], set_=set_)

    @staticmethod
    def prune_sources_scoped_statement(keep_keys: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.sources_raw WHERE collection = :col AND source_id NOT IN keep_keys`.

        Args:
            keep_keys: The source ids to keep. Empty deletes every source of the collection.
            collection: The collection to prune.

        Returns:
            The delete statement.
        """
        stmt = delete(SourceRawORM).where(SourceRawORM.collection == collection)
        if keep_keys:
            stmt = stmt.where(_not_in_values(SourceRawORM.source_id, keep_keys))
        return stmt

    @staticmethod
    def delete_sources_by_keys_statement(source_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.sources_raw WHERE collection = :col AND source_id IN source_ids`.

        The delete cascades to the sources' `kb.raw_chunks`.

        Args:
            source_ids: The source ids to delete.
            collection: The collection the sources belong to.

        Returns:
            The delete statement.
        """
        return delete(SourceRawORM).where(SourceRawORM.collection == collection, _in_values(SourceRawORM.source_id, source_ids))

    # raw_chunks -------------------------------------------------------------

    @staticmethod
    def upsert_chunks_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT … ON CONFLICT (collection, id) DO UPDATE` for `kb.raw_chunks`, with re-embedding gated on `body`.

        Args:
            rows: The chunk rows to write.

        Returns:
            The upsert statement.
        """
        return _upsert_statement(RawChunkORM, ("collection", "id"), rows, embed_gate="body")

    @staticmethod
    def prune_chunks_scoped_statement(keep_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.raw_chunks WHERE collection = :col AND id NOT IN keep_ids`.

        Args:
            keep_ids: The chunk ids to keep. Empty deletes every chunk of the collection.
            collection: The collection to prune.

        Returns:
            The delete statement.
        """
        stmt = delete(RawChunkORM).where(RawChunkORM.collection == collection)
        if keep_ids:
            stmt = stmt.where(_not_in_values(RawChunkORM.id, keep_ids))
        return stmt

    @staticmethod
    def chunks_needing_embedding_statement(collection: str) -> Select:
        """`SELECT * FROM kb.raw_chunks WHERE embedding IS NULL AND collection = :col`.

        Args:
            collection: The collection to read.

        Returns:
            The select of the chunks waiting for an embedding.
        """
        return select(RawChunkORM).where(RawChunkORM.embedding.is_(None)).where(RawChunkORM.collection == collection)

    # async surface ----------------------------------------------------------

    async def upsert_sources(self, rows: Sequence[Row]) -> None:
        """Bulk upsert source mirror rows by `source_id`, a no-op on an empty batch.

        Args:
            rows: The source rows to write.
        """
        if not rows:
            return
        for batch in batches(rows):
            await self._db.execute(self.upsert_sources_statement(batch))
        await self._db.flush()

    async def prune_missing_sources(self, keep_keys: set[str], *, collection: str) -> int:
        """Delete `collection`'s source rows not in `keep_keys`.

        Args:
            keep_keys: The source ids to keep.
            collection: The collection to prune.

        Returns:
            The number of rows removed.
        """
        result = await self._db.execute(self.prune_sources_scoped_statement(keep_keys, collection))
        await self._db.flush()
        return _deleted_count(result)

    async def delete_sources_by_keys(self, source_ids: set[str], *, collection: str) -> None:
        """Delete the source mirrors in `source_ids` and their chunks, a no-op on an empty set.

        Args:
            source_ids: The source ids to delete.
            collection: The collection the sources belong to.
        """
        if not source_ids:
            return
        await self._db.execute(self.delete_sources_by_keys_statement(source_ids, collection))
        await self._db.flush()

    async def upsert_chunks(self, rows: Sequence[Row]) -> None:
        """Bulk upsert raw chunk rows by `id`, a no-op on an empty batch.

        Args:
            rows: The chunk rows to write.
        """
        if not rows:
            return
        for batch in batches(rows):
            await self._db.execute(self.upsert_chunks_statement(batch))
        await self._db.flush()

    async def prune_missing_chunks(self, keep_ids: set[str], *, collection: str) -> int:
        """Delete `collection`'s chunk rows not in `keep_ids`.

        Args:
            keep_ids: The chunk ids to keep.
            collection: The collection to prune.

        Returns:
            The number of rows removed.
        """
        result = await self._db.execute(self.prune_chunks_scoped_statement(keep_ids, collection))
        await self._db.flush()
        return _deleted_count(result)

    async def chunks_needing_embedding(self, *, collection: str) -> Sequence[PendingEmbedChunk]:
        """Return `collection`'s raw chunk rows whose `embedding IS NULL`.

        Args:
            collection: The collection to read.

        Returns:
            The chunks waiting for an embedding.
        """
        columns = RawChunkORM.__table__.c
        result = await self._db.execute(self.chunks_needing_embedding_statement(collection).with_only_columns(columns.id, columns.source_id))
        return [PendingEmbedChunk(id=row_id, source_id=source_id) for row_id, source_id in result.all()]

    async def write_chunk_embeddings(self, vectors: Mapping[str, Sequence[float]], *, collection: str) -> None:
        """Set each raw chunk's `embedding` by `(collection, id)` and touch no other column, a no-op on an empty mapping.

        Args:
            vectors: The vector of each chunk id.
            collection: The collection every chunk belongs to.
        """
        if not vectors:
            return
        await self._db.execute(update(RawChunkORM), _embed_write_rows(vectors, collection=collection))
        await self._db.flush()


class EmbedMeta:
    """`kb.embed_meta`, the single row naming the embedding model and its dimension.

    Writing it replaces the row. It has no prune and no delete by id.
    """

    def __init__(self, db: AsyncSession, now_fn: Callable[[], datetime] = _utc_now) -> None:
        """Bind the repository to one session and one clock.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
            now_fn: The clock that stamps `embedded_at`.
        """
        self._db = db
        self._now = now_fn

    @staticmethod
    def delete_all_statement() -> Delete:
        """`DELETE FROM kb.embed_meta`, which clears the single row before it is inserted again.

        Returns:
            The delete statement.
        """
        return delete(EmbedMetaORM)

    @staticmethod
    def insert_statement(*, model: str, dim: int, embedded_at: datetime | None = None) -> Insert:
        """`INSERT INTO kb.embed_meta` with the model, its dimension and a stamped `embedded_at`.

        Args:
            model: The embedding model name.
            dim: The embedding dimension.
            embedded_at: The time to stamp, or None for the current time.

        Returns:
            The insert statement.
        """
        values: Row = {"model": model, "dim": dim, "embedded_at": embedded_at or datetime.now(tz=UTC)}
        return insert(EmbedMetaORM).values([values])

    async def set_embed_meta(self, *, model: str, dim: int) -> None:
        """Replace the single embed meta row with `model` and `dim`, stamped with the clock's time.

        Args:
            model: The embedding model name.
            dim: The embedding dimension.
        """
        await self._db.execute(self.delete_all_statement())
        await self._db.execute(self.insert_statement(model=model, dim=dim, embedded_at=self._now()))
        await self._db.flush()

    async def get_embed_meta(self) -> EmbedMetaORM | None:
        """Return the single embed meta row.

        Returns:
            The row, or None before the first embedding run.
        """
        result = await self._db.execute(select(EmbedMetaORM))
        return result.scalars().first()


class ManifestEntries:
    """`kb.index_manifest`, one row per indexed file keyed `(collection, path)`, with its content hash and identity hash.

    The incremental index compares it with the files on disk to find what was added, removed or changed.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def upsert_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT … ON CONFLICT (collection, path) DO UPDATE` for `kb.index_manifest`.

        Args:
            rows: The manifest rows to write.

        Returns:
            The upsert statement.
        """
        return _upsert_statement(IndexManifestORM, ("collection", "path"), rows)

    @staticmethod
    def load_statement(collection: str) -> Select:
        """`SELECT * FROM kb.index_manifest WHERE collection = :col`.

        Args:
            collection: The collection to read.

        Returns:
            The select of the collection's manifest rows.
        """
        return select(IndexManifestORM).where(IndexManifestORM.__table__.c.collection == collection)

    @staticmethod
    def delete_paths_statement(paths: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.index_manifest WHERE collection = :col AND path IN paths`.

        Args:
            paths: The file paths whose entries are deleted.
            collection: The collection the entries belong to.

        Returns:
            The delete statement.
        """
        return delete(IndexManifestORM).where(
            IndexManifestORM.__table__.c.collection == collection, _in_values(IndexManifestORM.__table__.c.path, paths)
        )

    # -- async surface (flush only, never commit) ----------------------------

    async def load(self, collection: str) -> dict[str, ManifestEntry]:
        """Return `collection`'s stored manifest keyed by path.

        An empty mapping means no index ran yet, and the incremental index then rebuilds everything.

        Args:
            collection: The collection to read.

        Returns:
            The entry of each file path.
        """
        result = await self._db.execute(self.load_statement(collection))
        return {
            row.path: ManifestEntry(
                path=row.path,
                kind=FileType(row.kind),
                content_hash=row.content_hash,
                identity_hash=row.identity_hash,
                page_id=row.page_id,
                source_key=row.source_key,
            )
            for row in result.scalars().all()
        }

    async def upsert_many(self, entries: Sequence[Row], *, collection: str) -> None:
        """Bulk upsert manifest entries by `(collection, path)`, a no-op on an empty batch.

        Args:
            entries: The manifest rows to write. Each carries its own `collection`.
            collection: Unused. The rows name their own collection.
        """
        del collection  # each row carries its own collection column
        if not entries:
            return
        for batch in batches(entries):
            await self._db.execute(self.upsert_statement(batch))
        await self._db.flush()

    async def delete_paths(self, paths: set[str], *, collection: str) -> None:
        """Delete the manifest entries for `paths`, a no-op on an empty set.

        Args:
            paths: The file paths whose entries are deleted.
            collection: The collection the entries belong to.
        """
        if not paths:
            return
        await self._db.execute(self.delete_paths_statement(paths, collection))
        await self._db.flush()


class LinkRefs:
    """`kb.link_refs`, every outbound link of every page, resolved or not, keyed by the lower-cased `raw_target`.

    It answers which pages link at a name, which `kb.edges` cannot.
    A page's links are replaced as a set.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def src_pages_referencing_statement(names: set[str], collection: str) -> Select:
        """`SELECT DISTINCT src_page_id … WHERE collection = :col AND raw_target IN names`.

        Args:
            names: The lower-cased link targets to match.
            collection: The collection to read.

        Returns:
            The select of every page linking at one of `names`.
        """
        return (
            select(LinkRefORM.__table__.c.src_page_id)
            .where(LinkRefORM.__table__.c.collection == collection, _in_values(LinkRefORM.__table__.c.raw_target, names))
            .distinct()
        )

    @staticmethod
    def src_pages_pointing_at_statement(dst_ids: set[str], collection: str) -> CompoundSelect:
        """`SELECT src_page_id … WHERE collection = :col AND dst_id IN dst_ids`, united with the pages whose claims cite them.

        The citing claims are read from `kb.claims` and `kb.claim_qualifiers`.

        Args:
            dst_ids: The resolved page ids to match.
            collection: The collection to read.

        Returns:
            The select of every page whose resolved link, claim subject or claim qualifier points at one of `dst_ids`.
        """
        link = LinkRefORM.__table__.c
        claim = ClaimORM.__table__.c
        qualifier = ClaimQualifierORM.__table__.c
        by_link = select(link.src_page_id).where(link.collection == collection, _in_values(link.dst_id, dst_ids))
        by_subject = select(claim.page_id).where(
            claim.collection == collection, claim.subject_collection == collection, _in_values(claim.subject_id, dst_ids)
        )
        by_qualifier = (
            select(claim.page_id)
            .join_from(ClaimQualifierORM, ClaimORM, (claim.collection == qualifier.collection) & (claim.id == qualifier.claim_id))
            .where(qualifier.collection == collection, qualifier.target_collection == collection, _in_values(qualifier.target_id, dst_ids))
        )
        return union(by_link, by_subject, by_qualifier)

    @staticmethod
    def delete_for_sources_statement(src_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.link_refs WHERE collection = :col AND src_page_id IN src_ids`.

        Args:
            src_ids: The pages whose outbound links are deleted.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        return delete(LinkRefORM).where(
            LinkRefORM.__table__.c.collection == collection, _in_values(LinkRefORM.__table__.c.src_page_id, src_ids)
        )

    @staticmethod
    def insert_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT INTO kb.link_refs` for `rows`, with the ids generated by the database.

        Args:
            rows: The link rows to insert.

        Returns:
            The insert statement.
        """
        return insert(LinkRefORM).values(list(rows))

    # -- async surface (flush only, never commit) ----------------------------

    async def src_pages_referencing(self, names: set[str], *, collection: str) -> set[str]:
        """Return the ids of the pages linking at any of `names`, empty on an empty `names`.

        Args:
            names: The lower-cased link targets to match.
            collection: The collection to read.

        Returns:
            The linking page ids.
        """
        if not names:
            return set()
        result = await self._db.execute(self.src_pages_referencing_statement(names, collection))
        return set(result.scalars().all())

    async def src_pages_pointing_at(self, dst_ids: set[str], *, collection: str) -> set[str]:
        """Return the ids of the pages whose resolved link, claim subject or claim qualifier points at any of `dst_ids`.

        An empty `dst_ids` answers an empty set.

        Args:
            dst_ids: The resolved page ids to match.
            collection: The collection to read.

        Returns:
            The linking page ids.
        """
        if not dst_ids:
            return set()
        result = await self._db.execute(self.src_pages_pointing_at_statement(dst_ids, collection))
        return set(result.scalars().all())

    async def replace_for_sources(self, rows: Sequence[Row], src_ids: set[str], *, collection: str) -> None:
        """Replace the outbound links of `src_ids` with `rows`, in the caller's transaction.

        Args:
            rows: The fresh link rows, resolved and unresolved.
            src_ids: The changed and deleted pages whose links are deleted.
            collection: The collection the pages belong to.
        """
        if src_ids:
            await self._db.execute(self.delete_for_sources_statement(src_ids, collection))
        if rows:
            for batch in batches(rows):
                await self._db.execute(self.insert_statement(batch))
        await self._db.flush()


class ClaimQualifiers:
    """`kb.claim_qualifiers`, replaced as a set per owning page, delete then insert.

    An upsert alone would keep the rows of a qualifier list that got shorter, so a page's rows are always cleared first.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def delete_for_pages_statement(page_ids: set[str], collection: str) -> Delete:
        """`DELETE … WHERE collection = :col AND claim_id IN (SELECT id FROM claims WHERE page_id IN page_ids)`.

        Args:
            page_ids: The pages whose claims' qualifier rows are deleted.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        claim_ids = select(ClaimORM.__table__.c.id).where(_in_values(ClaimORM.__table__.c.page_id, page_ids))
        return delete(ClaimQualifierORM).where(ClaimQualifierORM.collection == collection, ClaimQualifierORM.claim_id.in_(claim_ids))

    @staticmethod
    def insert_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT INTO kb.claim_qualifiers` for `rows`.

        Args:
            rows: The qualifier rows to insert.

        Returns:
            The insert statement.
        """
        return insert(ClaimQualifierORM).values(list(rows))

    # -- async surface (flush only, never commit) ----------------------------

    async def replace_for_pages(self, rows: Sequence[Row], page_ids: set[str], *, collection: str) -> None:
        """Replace the qualifier rows of the claims of `page_ids` with `rows`, in the caller's transaction.

        Args:
            rows: The fresh qualifier rows.
            page_ids: The changed and deleted pages whose claims' qualifier rows are deleted.
            collection: The collection the pages belong to.
        """
        if page_ids:
            await self._db.execute(self.delete_for_pages_statement(page_ids, collection))
        if rows:
            for batch in batches(rows):
                await self._db.execute(self.insert_statement(batch))
        await self._db.flush()


class ValidationIssues:
    """`kb.validation_store`, the stored validation issues of each page.

    The whole knowledge base's report is the stored issues of the unchanged pages plus the issues recomputed for the changed ones.
    """

    def __init__(self, db: AsyncSession) -> None:
        """Bind the repository to one session.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
        """
        self._db = db

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def delete_for_pages_statement(page_ids: set[str], collection: str) -> Delete:
        """`DELETE FROM kb.validation_store WHERE collection = :col AND page_id IN page_ids`.

        Args:
            page_ids: The pages whose stored issues are deleted.
            collection: The collection the pages belong to.

        Returns:
            The delete statement.
        """
        return delete(ValidationStoreORM).where(
            ValidationStoreORM.__table__.c.collection == collection, _in_values(ValidationStoreORM.__table__.c.page_id, page_ids)
        )

    @staticmethod
    def delete_for_collection_statement(collection: str) -> Delete:
        """`DELETE FROM kb.validation_store WHERE collection = :col`, every stored issue of one collection.

        Args:
            collection: The collection whose issues are deleted.

        Returns:
            The delete statement.
        """
        return delete(ValidationStoreORM).where(ValidationStoreORM.__table__.c.collection == collection)

    @staticmethod
    def insert_statement(rows: Sequence[Row]) -> Insert:
        """`INSERT INTO kb.validation_store` for `rows`, with the ids generated by the database.

        Args:
            rows: The issue rows to insert.

        Returns:
            The insert statement.
        """
        return insert(ValidationStoreORM).values(list(rows))

    @staticmethod
    def whole_report_statement(collection: str) -> Select:
        """`SELECT * FROM kb.validation_store WHERE collection = :col`, every stored issue.

        Args:
            collection: The collection to read.

        Returns:
            The select of the collection's stored issues.
        """
        return select(ValidationStoreORM).where(ValidationStoreORM.__table__.c.collection == collection)

    # -- async surface (flush only, never commit) ----------------------------

    async def replace_for_pages(self, issues: Sequence[Row], page_ids: set[str], *, collection: str) -> None:
        """Replace the stored issues of `page_ids` with `issues`, in the caller's transaction.

        Args:
            issues: The freshly computed issue rows.
            page_ids: The changed and deleted pages whose stored issues are deleted.
            collection: The collection the pages belong to.
        """
        if page_ids:
            await self._db.execute(self.delete_for_pages_statement(page_ids, collection))
        if issues:
            for batch in batches(issues):
                await self._db.execute(self.insert_statement(batch))
        await self._db.flush()

    async def replace_all(self, issues: Sequence[Row], *, collection: str) -> None:
        """Replace every stored issue of `collection` with `issues`, in the caller's transaction.

        Args:
            issues: Every issue row of a full rebuild.
            collection: The collection whose issues are replaced.
        """
        await self._db.execute(self.delete_for_collection_statement(collection))
        if issues:
            for batch in batches(issues):
                await self._db.execute(self.insert_statement(batch))
        await self._db.flush()

    async def whole_report(self, collection: str) -> ValidationReport:
        """Return the stored issues of `collection` as a report.

        The caller merges the recomputed issues of the changed pages into it.
        Its ingest queue is empty.

        Args:
            collection: The collection to read.

        Returns:
            The stored errors and warnings.
        """
        result = await self._db.execute(self.whole_report_statement(collection))
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        for row in result.scalars().all():
            issue = ValidationIssue(severity=_severity(row.severity), rule=row.rule, page_id=row.page_id, message=row.message, anchor=row.anchor)
            (errors if row.severity == "error" else warnings).append(issue)
        return ValidationReport(errors=tuple(errors), warnings=tuple(warnings))


class IndexFingerprints:
    """`kb.index_fingerprint`, one row per collection holding what its last index ran with.

    A mismatch on `indexer_version`, `spec_hash`, `vocabulary_hash` or `embed_model` forces a full rebuild.
    """

    def __init__(self, db: AsyncSession, now_fn: Callable[[], datetime] = _utc_now) -> None:
        """Bind the repository to one session and one clock.

        Args:
            db: The session every statement runs on. The caller owns its transaction.
            now_fn: The clock that stamps `indexed_at`.
        """
        self._db = db
        self._now = now_fn

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def get_statement(collection: str) -> Select:
        """`SELECT * FROM kb.index_fingerprint WHERE collection = :col`, the single row or none.

        Args:
            collection: The collection to read.

        Returns:
            The select of the collection's fingerprint.
        """
        return select(IndexFingerprintORM).where(IndexFingerprintORM.__table__.c.collection == collection)

    @staticmethod
    def upsert_statement(row: Row) -> Insert:
        """`INSERT … ON CONFLICT (collection) DO UPDATE` for `kb.index_fingerprint`.

        The update never writes `switches`, so a decided switches value survives every index.
        A new row inserts `switches` at its all-off default.

        Args:
            row: The fingerprint columns and a stamped `indexed_at`.

        Returns:
            The upsert statement.
        """
        stmt = insert(IndexFingerprintORM).values(row)
        set_: dict[str, object] = {c.name: stmt.excluded[c.name] for c in stmt.table.columns if c.name not in {"collection", "switches"}}
        return stmt.on_conflict_do_update(index_elements=["collection"], set_=set_)

    # -- async surface (flush only, never commit) ----------------------------

    async def get(self, collection: str) -> StoredFingerprint | None:
        """Return the fingerprint row of `collection`.

        Args:
            collection: The collection to read.

        Returns:
            The row, or None before the first index.
        """
        row = await self._row(collection)
        if row is None:
            return None
        return StoredFingerprint(
            indexer_version=row.indexer_version,
            spec_hash=row.spec_hash,
            vocabulary_hash=row.vocabulary_hash,
            embed_model=row.embed_model,
            knowledge_base_root=row.knowledge_base_root,
        )

    async def _row(self, collection: str) -> IndexFingerprintORM | None:
        """The ORM row of `collection`, or None before the first index."""
        result = await self._db.execute(self.get_statement(collection))
        return result.scalars().first()

    async def set(  # noqa: PLR0913 (the six fingerprint columns are one row, a wrapper object would just rename them)
        self,
        *,
        collection: str,
        indexer_version: str,
        spec_hash: str,
        vocabulary_hash: str,
        embed_model: str,
        knowledge_base_root: str,
    ) -> None:
        """Upsert the fingerprint row of `collection`, stamped with the clock's time.

        Args:
            collection: The collection indexed.
            indexer_version: The indexer version that ran.
            spec_hash: The hash of the schema.
            vocabulary_hash: The hash of the vocabulary.
            embed_model: The embedding model name.
            knowledge_base_root: The knowledge base folder indexed.
        """
        row: Row = {
            "collection": collection,
            "indexer_version": indexer_version,
            "spec_hash": spec_hash,
            "vocabulary_hash": vocabulary_hash,
            "embed_model": embed_model,
            "indexed_at": self._now(),
            "knowledge_base_root": knowledge_base_root,
        }
        await self._db.execute(self.upsert_statement(row))
        await self._db.flush()

    @staticmethod
    def touch_statement(collection: str, *, indexed_at: datetime, knowledge_base_root: str) -> Update:
        """`UPDATE kb.index_fingerprint SET indexed_at = … [, knowledge_base_root = …] WHERE collection = …`.

        It only updates and never creates a row.

        Args:
            collection: The collection indexed.
            indexed_at: The time to stamp.
            knowledge_base_root: The knowledge base folder indexed. Empty keeps the stored root.

        Returns:
            The update statement.
        """
        values: dict[str, object] = {"indexed_at": indexed_at}
        if knowledge_base_root:
            values["knowledge_base_root"] = knowledge_base_root
        return update(IndexFingerprintORM).where(IndexFingerprintORM.__table__.c.collection == collection).values(values)

    async def touch(self, collection: str, *, knowledge_base_root: str) -> None:
        """Stamp `indexed_at` and, when known, `knowledge_base_root` after an incremental index wrote rows.

        Args:
            collection: The collection indexed.
            knowledge_base_root: The knowledge base folder indexed. Empty keeps the stored root.
        """
        await self._db.execute(self.touch_statement(collection, indexed_at=self._now(), knowledge_base_root=knowledge_base_root))
        await self._db.flush()

    @staticmethod
    def set_switches_statement(collection: str, switches: RetrievalSwitches) -> Insert:
        """`INSERT … ON CONFLICT (collection) DO UPDATE SET switches = excluded.switches` for `kb.index_fingerprint`.

        A collection with no row gets one, with empty version and hash columns that no index matches.
        The conflict update writes `switches` alone, so an indexed row keeps every other column.

        Args:
            collection: The collection whose switches are stored.
            switches: The switches to store.

        Returns:
            The upsert statement.
        """
        stmt = insert(IndexFingerprintORM).values(
            collection=collection, indexer_version="", spec_hash="", vocabulary_hash="", embed_model="", switches=switches.model_dump()
        )
        return stmt.on_conflict_do_update(index_elements=["collection"], set_={"switches": stmt.excluded.switches})

    async def get_switches(self, collection: str) -> RetrievalSwitches:
        """Return the stored switches of `collection`.

        Args:
            collection: The collection to read.

        Returns:
            The switches, all off when none were ever written.
        """
        row = await self._row(collection)
        if row is None:
            return RetrievalSwitches()
        return RetrievalSwitches.model_validate(row.switches)

    async def set_switches(self, *, collection: str, switches: RetrievalSwitches) -> None:
        """Store the switches of `collection`, flushing but not committing.

        A collection that was never indexed gets a fingerprint row holding them.

        Args:
            collection: The collection to update.
            switches: The switches to store.
        """
        await self._db.execute(self.set_switches_statement(collection, switches))
        await self._db.flush()


# The seed of the reachability walk: the endpoint with the most incident edges in either
# direction. The tie break on `node` makes the same data always pick the same seed.
_SEED_NODE_SQL = """
SELECT node
FROM (
    SELECT src_id AS node FROM kb.edges WHERE src_collection = :collection AND dst_collection = :collection
    UNION ALL
    SELECT dst_id AS node FROM kb.edges WHERE src_collection = :collection AND dst_collection = :collection
) AS endpoints
GROUP BY node
ORDER BY count(*) DESC, node
LIMIT 1
"""

# The count of nodes reachable from `:seed` over edges in either direction. `reach` dedupes with
# `UNION`, so each node is expanded once and the walk ends when no new node is found, with no depth cap.
_REACHABLE_COUNT_SQL = """
WITH RECURSIVE undirected(src_id, dst_id) AS MATERIALIZED (
    SELECT src_id, dst_id FROM kb.edges WHERE src_collection = :collection AND dst_collection = :collection
    UNION ALL
    SELECT dst_id, src_id FROM kb.edges WHERE src_collection = :collection AND dst_collection = :collection
),
reach(node) AS (
    SELECT CAST(:seed AS text)
    UNION
    SELECT e.dst_id
    FROM reach r
    JOIN undirected e ON e.src_id = r.node
)
SELECT count(*) FROM reach
"""


class SignalsQueries:
    """Read-only counts over `kb.pages`, `kb.sections` and `kb.edges` that the automatic retrieval mode decision reads."""

    def __init__(self, db: AsyncSession) -> None:
        """Bind the queries to one session.

        Args:
            db: The session every statement runs on.
        """
        self._db = db

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def notes_and_page_types_statement(collection: str) -> Select:
        """`count(*), count(DISTINCT page_type)` over `kb.pages`, scoped to `collection`.

        Args:
            collection: The collection to count.

        Returns:
            The select of the page count and the page type count.
        """
        return select(func.count(), func.count(PageORM.page_type.distinct())).select_from(PageORM).where(PageORM.collection == collection)

    @staticmethod
    def token_estimate_statement(collection: str) -> Select:
        """`coalesce(sum(length(body)), 0) / 4` over `kb.sections`, scoped to `collection`.

        Args:
            collection: The collection to count.

        Returns:
            The select of the token estimate.
        """
        return (
            select(func.coalesce(func.sum(func.length(SectionORM.body)), 0) / 4)
            .select_from(SectionORM)
            .where(SectionORM.collection == collection)
        )

    @staticmethod
    def edges_count_statement(collection: str) -> Select:
        """`count(*)` over `kb.edges` with both endpoints in `collection`.

        Args:
            collection: The collection to count.

        Returns:
            The select of the edge count.
        """
        return select(func.count()).select_from(EdgeORM).where(EdgeORM.src_collection == collection, EdgeORM.dst_collection == collection)

    # -- async surface (read-only) --------------------------------------------

    async def measure(self, collection: str) -> Signals:
        """Count `collection`'s notes, page types, token estimate, edges per note and giant component share.

        The reachability walk runs only when `edges_per_note` reaches `GRAPH_OFF_EDGES_PER_NOTE`.
        Below it `giant_component` is 0.0.

        Args:
            collection: The collection to measure.

        Returns:
            The signals.
        """
        notes, page_types = (await self._db.execute(self.notes_and_page_types_statement(collection))).one()
        token_estimate = (await self._db.execute(self.token_estimate_statement(collection))).scalar_one()
        edges = (await self._db.execute(self.edges_count_statement(collection))).scalar_one()
        edges_per_note = edges / notes if notes > 0 else 0.0
        giant_component = 0.0
        if notes > 0 and edges_per_note >= GRAPH_OFF_EDGES_PER_NOTE:
            giant_component = await self._giant_component(collection, notes=notes)
        return Signals(
            notes=notes, token_estimate=int(token_estimate), page_types=page_types, edges_per_note=edges_per_note, giant_component=giant_component
        )

    async def _giant_component(self, collection: str, *, notes: int) -> float:
        """The share of `notes` reachable from the highest-degree node, over edges in either direction."""
        seed = (await self._db.execute(text(_SEED_NODE_SQL), {"collection": collection})).scalar_one_or_none()
        if seed is None:
            return 0.0
        reached = (await self._db.execute(text(_REACHABLE_COUNT_SQL), {"collection": collection, "seed": seed})).scalar_one()
        return reached / notes


def _severity(value: str) -> Literal["error", "warning"]:
    """Narrow a stored severity to `error` or `warning`, reading any other value as `warning`."""
    return "error" if value == "error" else "warning"
