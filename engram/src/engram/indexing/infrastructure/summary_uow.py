"""The SQLAlchemy unit of work behind the disclosure-summary stage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.ext.asyncio import AsyncSession

from engram.indexing.domain.summary_units import SummaryUnit
from engram.indexing.infrastructure.orm import PageORM, SectionORM, UnitSummaryORM

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncEngine

    from engram.indexing.domain.summary_units import SummaryUpsertRow

# The statuses that count as a current summary: `active` and `pending`. A unit with a row in
# one of them needs a new summary only when its `source_hash` no longer matches its body.
_CURRENT_STATUSES = ("active", "pending")


class SqlAlchemySummaryUnitOfWork:
    """The SQLAlchemy `SummaryUnitOfWork`: one session for one summary run.

    A unit is pending when it has no current summary, or only one written from an older body.
    Writes flush and never commit. `commit` persists the whole run.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Open a session on `engine`.

        Args:
            engine: The engine the session runs on.
        """
        self._engine = engine
        self._session = AsyncSession(engine, expire_on_commit=False)

    # -- statement factories (built without a database) --------------------

    @staticmethod
    def pending_pages_statement(collection: str) -> Select:
        """Pages of `collection` with no current summary: `LEFT JOIN … WHERE s.unit_id IS NULL`.

        A summary counts only when its `source_hash` matches the page's `body_hash` and its collection matches the page's.

        Args:
            collection: The collection to read.

        Returns:
            The select of the pending pages.
        """
        join_condition = and_(
            UnitSummaryORM.collection == PageORM.collection,
            UnitSummaryORM.unit_id == PageORM.id,
            UnitSummaryORM.source_hash == PageORM.body_hash,
            UnitSummaryORM.status.in_(_CURRENT_STATUSES),
        )
        return (
            select(PageORM.id, PageORM.title, PageORM.card_text, PageORM.body_hash)
            .outerjoin(UnitSummaryORM, join_condition)
            .where(PageORM.collection == collection, UnitSummaryORM.unit_id.is_(None))
        )

    @staticmethod
    def pending_sections_statement(collection: str) -> Select:
        """Sections of `collection` with no current summary, joined through their owning page.

        A summary counts only when its `source_hash` matches the section's `body_hash` and its collection matches the section's.
        Each row carries the owning page's title.

        Args:
            collection: The collection to read.

        Returns:
            The select of the pending sections.
        """
        join_condition = and_(
            UnitSummaryORM.collection == SectionORM.collection,
            UnitSummaryORM.unit_id == SectionORM.id,
            UnitSummaryORM.source_hash == SectionORM.body_hash,
            UnitSummaryORM.status.in_(_CURRENT_STATUSES),
        )
        return (
            select(SectionORM.id, PageORM.title, SectionORM.path, SectionORM.body, SectionORM.body_hash)
            .join(PageORM, and_(PageORM.collection == SectionORM.collection, PageORM.id == SectionORM.page_id))
            .outerjoin(UnitSummaryORM, join_condition)
            .where(PageORM.collection == collection, UnitSummaryORM.unit_id.is_(None))
        )

    @staticmethod
    def upsert_statement(row: SummaryUpsertRow) -> Insert:
        """`INSERT … ON CONFLICT (collection, unit_id) DO UPDATE` for `kb.unit_summaries`.

        `indexed_at` is stamped `now()` on insert and on update, so it always shows the last summary.

        Args:
            row: The summary row to write.

        Returns:
            The upsert statement.
        """
        stmt = insert(UnitSummaryORM).values(dict(row))
        set_: dict[str, object] = {
            c.name: stmt.excluded[c.name] for c in stmt.table.columns if c.name not in {"collection", "unit_id", "indexed_at"}
        }
        set_["indexed_at"] = func.now()
        return stmt.on_conflict_do_update(index_elements=["collection", "unit_id"], set_=set_)

    # -- async surface (flush only, `commit` persists the run) ----------------

    async def pending_units(self, collection: str) -> Sequence[SummaryUnit]:
        """Return `collection`'s pending pages, then its pending sections.

        A section's title is `<page title> > <heading path>`.

        Args:
            collection: The collection to read.

        Returns:
            The units waiting for a summary.
        """
        page_rows = (await self._session.execute(self.pending_pages_statement(collection))).all()
        section_rows = (await self._session.execute(self.pending_sections_statement(collection))).all()
        units = [
            SummaryUnit(unit_id=row.id, unit_type="page", title=row.title, body=row.card_text, source_hash=row.body_hash)
            for row in page_rows
        ]
        units.extend(
            SummaryUnit(
                unit_id=row.id,
                unit_type="section",
                title=f"{row.title} > {row.path}",
                body=row.body,
                source_hash=row.body_hash,
            )
            for row in section_rows
        )
        return units

    async def upsert_summary(self, row: SummaryUpsertRow) -> None:
        """Upsert one `kb.unit_summaries` row by `(collection, unit_id)`, flushing but not committing.

        Args:
            row: The summary row to write.
        """
        await self._session.execute(self.upsert_statement(row))
        await self._session.flush()

    async def commit(self) -> None:
        """Persist every upsert made through this unit of work's session."""
        await self._session.commit()

    async def close(self) -> None:
        """Close this unit of work's session, which gives its connection back to the pool."""
        await self._session.close()
