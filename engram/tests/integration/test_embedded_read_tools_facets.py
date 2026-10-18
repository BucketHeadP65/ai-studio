"""Integration: `embedded_read_tools` gives each knowledge base's `search` the facet keys its own schema declares.

The collection is seeded with four sources whose raw chunks carry one made-up term, two of them with a page carrying a
`shade` value. A reader opened over a schema declaring `shade` narrows the search to the source whose page carries the
value asked for, and refuses a key the schema does not declare. Runs against the `migrated` fixture and skips when
Docker is unavailable.
"""

from __future__ import annotations

import asyncpg
import pytest

from pydantic_ai import ModelRetry

from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.domain import SearchResult
from engram.retrieval.infrastructure.embedded import KnowledgeBaseConnection, embedded_read_tools
from tests.integration.test_migrations import _asyncpg_dsn
from tests.integration.test_search_raw_facets import _COLLECTION, _TERM, _seed


def _schema() -> KnowledgeBaseSchema:
    return KnowledgeBaseSchema.model_validate(
        {"slug": _COLLECTION, "page_types": [{"page_type": "note"}], "facets": [{"key": "shade"}], "retrieval": {"collection": _COLLECTION}}
    )


class TestTheReaderTakesTheSchemasFacetKeys:
    async def test_the_search_narrows_by_a_declared_key_and_refuses_any_other(self, migrated: str, reader_url: str) -> None:
        connection = await asyncpg.connect(_asyncpg_dsn(migrated))
        try:
            await _seed(connection)
        finally:
            await connection.close()

        async with embedded_read_tools(KnowledgeBaseConnection(reader_url, spec=_schema())) as reader:
            facet_keys = reader.surfaces[0].facet_keys
            narrowed = await reader.direct_call_tool("search", {"query": _TERM, "facets": {"shade": ["teal"]}})
            with pytest.raises(ModelRetry, match=r"The declared keys are `shade`\."):
                await reader.direct_call_tool("search", {"query": _TERM, "facets": {"weight": ["heavy"]}})

        assert facet_keys == ("shade",)
        assert isinstance(narrowed, SearchResult)
        assert [hit.id for hit in narrowed.hits] == ["src.kept#0"]
