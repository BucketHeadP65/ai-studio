"""Unit tests for `load_indexed_schema` (`src/engram/memory/indexed_schema.py`).

Driven through :class:`FakeSqlGateway` (the existing `rows`-shaped fake, already structurally
compatible with `IndexedSchemaQueries` — no new fake needed), so there is no database. These pin:

* a row with a jsonb `spec` value validates into the equal `KnowledgeBaseSchema`;
* no row, and a row whose `spec` column is `NULL` (a collection that indexed before this column
  existed), both raise `KnowledgeBaseSchemaMissingError` naming the collection and `studio index`;
* the query is capped to one row (`cap=1`) and bound by `collection`.
"""

from __future__ import annotations

import pytest

from engram.kb.contracts import KnowledgeBaseSchemaMissingError, load_indexed_schema
from engram.retrieval.domain import SqlResult
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec


class TestLoadCompiledSpec:
    async def test_a_row_with_a_spec_value_validates_into_the_equal_knowledge_base_spec(self):
        spec = research_spec()
        reader = FakeSqlGateway(result=SqlResult(rows=({"spec": spec.model_dump(mode="json")},), columns=("spec",)))

        loaded = await load_indexed_schema(reader, "research")

        assert loaded == spec

    async def test_no_row_raises_missing_knowledge_base_spec_naming_the_collection(self):
        reader = FakeSqlGateway(result=SqlResult(rows=(), columns=("spec",)))

        with pytest.raises(KnowledgeBaseSchemaMissingError) as caught:
            await load_indexed_schema(reader, "repo-docs")

        assert "repo-docs" in str(caught.value)
        assert "studio index" in str(caught.value)

    async def test_a_null_spec_column_raises_missing_knowledge_base_spec(self):
        """A collection indexed before this column existed: a row exists, `spec` reads NULL."""
        reader = FakeSqlGateway(result=SqlResult(rows=({"spec": None},), columns=("spec",)))

        with pytest.raises(KnowledgeBaseSchemaMissingError):
            await load_indexed_schema(reader, "research")

    async def test_the_query_is_capped_to_one_row_and_binds_the_collection(self):
        reader = FakeSqlGateway(result=SqlResult(rows=({"spec": research_spec().model_dump(mode="json")},), columns=("spec",)))

        await load_indexed_schema(reader, "field-notes")

        assert reader.last.cap == 1
        assert reader.last.params == {"collection": "field-notes"}
        assert "api.indexed_schema" in reader.last.sql
