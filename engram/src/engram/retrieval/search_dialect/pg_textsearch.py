"""The `pg_textsearch` dialect: the knowledge base's BM25 SQL in that extension's grammar."""

from __future__ import annotations

from typing import Literal

from core.ai.retrieval.contracts import PgTextSearchHybridSearchBuilder

from engram.retrieval.knowledge_base_schema import KB_SEARCH_SCHEMA

# The one text column each table's BM25 index covers, because the `pg_textsearch` `bm25` access
# method indexes a single column. It must match the surface's `text_col` in `KB_SEARCH_SCHEMA`.
# Every schema name a migration passes (`wiki`, `memory`, `kb`) keeps its rows, and a migration that
# rebuilds a BM25 index under a new table name adds that table here.
_BM25_SEARCH_COLUMN: dict[str, str] = {
    "wiki.pages": "card_text",
    "wiki.sections": "body",
    "wiki.claims": "text",
    "wiki.raw_chunks": "body",
    "memory.pages": "card_text",
    "memory.sections": "body",
    "memory.claims": "text",
    "memory.raw_chunks": "body",
    "kb.pages": "card_text",
    "kb.raw_chunks": "body",
}

_BUILDER = PgTextSearchHybridSearchBuilder()


class PgTextSearchDialect:
    """Emits the knowledge base's BM25 SQL and `api.search` in `pg_textsearch` grammar.

    The index DDL uses the single-column `WITH (text_config='english')` form.
    """

    name: Literal["pg_search", "pg_textsearch"] = "pg_textsearch"

    def create_bm25_extension_ddl(self) -> str:
        """Return the `pg_textsearch` extension statement.

        Returns:
            The `CREATE EXTENSION IF NOT EXISTS pg_textsearch` statement.
        """
        return "CREATE EXTENSION IF NOT EXISTS pg_textsearch"

    def bm25_index_ddl(self, *, index: str, table: str, key: str, text_cols: tuple[str, ...]) -> str:
        """Emit a single-column `CREATE INDEX ... USING bm25` DDL.

        The indexed column is the one known search column of `table`.

        Args:
            index: Name of the index to create.
            table: Fully-qualified table the index covers (e.g. `kb.pages`).
            key: The key column, accepted for the `SearchDialect` contract and not emitted.
            text_cols: The text columns, accepted for the `SearchDialect` contract and unused.

        Returns:
            A single `CREATE INDEX` DDL statement for the arguments.

        Raises:
            KeyError: When `table` has no known search column.
        """
        del key, text_cols  # pg_textsearch needs no key_field and indexes one column.
        column = _BM25_SEARCH_COLUMN[table]
        return f"CREATE INDEX {index} ON {table} USING bm25 ({column}) WITH (text_config='english')"

    def api_search_body(self) -> str:
        """Return the full `api.search` function definition in `pg_textsearch` grammar.

        Returns:
            The complete `CREATE FUNCTION api.search(...)` statement in `pg_textsearch` grammar.
        """
        return _BUILDER.api_search_body(KB_SEARCH_SCHEMA)
