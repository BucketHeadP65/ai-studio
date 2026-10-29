"""SummaryStage writes pending summaries for units lacking a current one.

`SummaryStage` is the index-time disclosure-summary generator: it drains a `SummaryUnitOfWork`'s
`pending_units(collection)` — pages/sections whose `body_hash` has no matching `active`/`pending`
summary row — through an injected `Summarizer` port, and upserts each result as a `status='pending'`
row, never `active`. Everything here runs over the test-local `FakeSummaryUow` + `FakeSummarizer`, so
there is no database and no network.

`pending_units` / `upsert_summary` are `async def` on the fake because the production
`SqlAlchemySummaryUnitOfWork` runs real `SELECT`/`INSERT` I/O for both: the `SummaryUnitOfWork`
Protocol `SummaryStage` is typed against has to be async end-to-end for the production adapter to
satisfy it structurally.

`TestSqlAlchemySummaryUnitOfWork` below is the DB-less half of the persistence contract for the
production adapter (mirrors `test_compile_state_repositories.py`'s pattern): each `*_statement`
factory is indexed against the PostgreSQL dialect and asserted on its emitted SQL text, no
database needed. The round-trip behaviour these imply is an integration concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sqlalchemy.dialects import postgresql

from engram.indexing.contracts import SummaryStage, SummaryUnit
from engram.indexing.infrastructure.summary_uow import SqlAlchemySummaryUnitOfWork
from engram.testing.fakes.summarizer import FakeSummarizer
from engram.testing.fakes.summary_uow import FakeSummaryUow

if TYPE_CHECKING:
    from sqlalchemy import Insert, Select

    from engram.indexing.contracts import SummaryUpsertRow


def _compiles(statement: Insert | Select, *, literal_binds: bool = False) -> str:
    """Index a Core statement against the PostgreSQL dialect; return the SQL text.

    Raises whatever SQLAlchemy raises if the statement is malformed — a green call means the
    adapter emitted syntactically valid PostgreSQL (no live server needed). `literal_binds`
    inlines bound values into the text instead of leaving them as driver placeholders: needed to
    pin an `.in_(...)` clause's actual values, since SQLAlchemy indexes `.in_()` to an expanding
    postcompile bind (`status IN (__[POSTCOMPILE_status_1])`) whose SQL text is identical no
    matter which values the `IN` was built from.
    """
    compile_kwargs = {"literal_binds": True} if literal_binds else {}
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs=compile_kwargs))


async def test_stage_writes_pending_rows_via_summarizer() -> None:
    unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
    uow, summarizer = FakeSummaryUow([unit]), FakeSummarizer()
    stage = SummaryStage(lambda: uow, summarizer)

    written = await stage.run("workdocs")

    assert written == 1
    assert uow.upserts[0]["summary"] == "Summary of X."
    assert uow.upserts[0]["status"] == "pending"
    assert uow.upserts[0]["model"] == "fake-summarizer"
    assert (uow.committed, uow.closed) == (True, True)


class _RaisingSummarizer(FakeSummarizer):
    """A summarizer whose model call fails."""

    async def summarize(self, *, title: str, body: str) -> str:
        del title, body
        msg = "the summary model did not answer"
        raise RuntimeError(msg)


async def test_a_run_whose_summarizer_raises_closes_its_unit_of_work_uncommitted() -> None:
    unit = SummaryUnit(unit_id="mu.x", unit_type="page", title="X", body="Body.", source_hash="h1")
    uow = FakeSummaryUow([unit])
    stage = SummaryStage(lambda: uow, _RaisingSummarizer())

    with pytest.raises(RuntimeError, match="did not answer"):
        await stage.run("workdocs")

    assert (uow.committed, uow.closed) == (False, True)


async def test_stage_writes_nothing_when_no_units_are_pending() -> None:
    uow, summarizer = FakeSummaryUow([]), FakeSummarizer()
    stage = SummaryStage(lambda: uow, summarizer)

    written = await stage.run("workdocs")

    assert written == 0
    assert uow.upserts == []
    assert summarizer.calls == []
    assert uow.committed


async def test_stage_calls_summarizer_once_per_pending_unit_and_records_source_hash() -> None:
    page = SummaryUnit(unit_id="mu.a", unit_type="page", title="A", body="Body A.", source_hash="ha")
    section = SummaryUnit(unit_id="mu.a#intro", unit_type="section", title="A > intro", body="Body S.", source_hash="hs")
    uow, summarizer = FakeSummaryUow([page, section]), FakeSummarizer()
    stage = SummaryStage(lambda: uow, summarizer)

    written = await stage.run("workdocs")

    assert written == 2
    assert summarizer.calls == ["A", "A > intro"]
    assert uow.upserts[0]["unit_id"] == "mu.a"
    assert uow.upserts[0]["unit_kind"] == "page"
    assert uow.upserts[0]["source_hash"] == "ha"
    assert uow.upserts[1]["unit_id"] == "mu.a#intro"
    assert uow.upserts[1]["unit_kind"] == "section"
    assert uow.upserts[1]["source_hash"] == "hs"


class TestSqlAlchemySummaryUnitOfWork:
    """`kb.unit_summaries`: the pending-pages/-sections `LEFT JOIN` gates + the upsert."""

    def test_pending_pages_left_joins_unit_summaries_on_collection_id_hash_and_status_then_gates_null(self):
        sql = _compiles(SqlAlchemySummaryUnitOfWork.pending_pages_statement("research"), literal_binds=True)

        assert "FROM kb.pages" in sql
        assert "LEFT OUTER JOIN kb.unit_summaries" in sql
        assert "kb.unit_summaries.collection = kb.pages.collection" in sql
        assert "unit_summaries.unit_id = kb.pages.id" in sql
        assert "unit_summaries.source_hash = kb.pages.body_hash" in sql
        # Pins the actual IN-list values (an unqualified "status IN" assertion would pass
        # unchanged even if `_CURRENT_STATUSES` silently dropped "pending" or added "rejected" —
        # `.in_()` indexes to an expanding postcompile bind whose SQL text is value-agnostic
        # without `literal_binds`).
        assert "unit_summaries.status IN ('active', 'pending')" in sql
        assert "kb.pages.collection = 'research'" in sql
        assert "unit_summaries.unit_id IS NULL" in sql

    def test_pending_sections_scopes_through_the_owning_page_collection(self):
        sql = _compiles(SqlAlchemySummaryUnitOfWork.pending_sections_statement("research"), literal_binds=True)

        assert "FROM kb.sections" in sql
        assert "JOIN kb.pages ON kb.pages.collection = kb.sections.collection AND kb.pages.id = kb.sections.page_id" in sql
        assert "LEFT OUTER JOIN kb.unit_summaries" in sql
        assert "kb.unit_summaries.collection = kb.sections.collection" in sql
        assert "unit_summaries.unit_id = kb.sections.id" in sql
        assert "unit_summaries.source_hash = kb.sections.body_hash" in sql
        # Same value-pinning as the pages test above (see its comment).
        assert "unit_summaries.status IN ('active', 'pending')" in sql
        assert "kb.pages.collection = 'research'" in sql
        assert "unit_summaries.unit_id IS NULL" in sql

    def test_upsert_conflict_targets_collection_unit_id_sets_every_other_column_from_excluded_and_stamps_indexed_at(self):
        row: SummaryUpsertRow = {
            "unit_id": "mu.x",
            "unit_kind": "page",
            "collection": "research",
            "summary": "S",
            "status": "pending",
            "model": "fake-summarizer",
            "source_hash": "h1",
        }

        sql = _compiles(SqlAlchemySummaryUnitOfWork.upsert_statement(row))

        assert "INSERT INTO kb.unit_summaries" in sql
        assert "ON CONFLICT (collection, unit_id)" in sql
        assert "DO UPDATE SET" in sql
        for column in ("unit_kind", "summary", "status", "model", "source_hash"):
            assert f"{column} = excluded.{column}" in sql
        assert "unit_id = excluded.unit_id" not in sql
        assert "collection = excluded.collection" not in sql
        assert "indexed_at = now()" in sql
