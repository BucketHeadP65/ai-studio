"""The ParadeDB dialect, BM25 SQL in `pg_search` grammar."""

from __future__ import annotations

from typing import Literal

from core.ai.retrieval.contracts import ParadeDBHybridSearchBuilder

from engram.retrieval.knowledge_base_schema import KB_SEARCH_SCHEMA

_BUILDER = ParadeDBHybridSearchBuilder()


class PgSearchDialect:
    """Emits the knowledge base's BM25 SQL and `api.search` in ParadeDB `pg_search` grammar.

    The index DDL uses the multi-column `WITH (key_field=...)` form.
    """

    name: Literal["pg_search", "pg_textsearch"] = "pg_search"

    def create_bm25_extension_ddl(self) -> str:
        """Return the ParadeDB BM25 extension statement.

        Returns:
            The `CREATE EXTENSION IF NOT EXISTS pg_search` statement.
        """
        return "CREATE EXTENSION IF NOT EXISTS pg_search"

    def bm25_index_ddl(self, *, index: str, table: str, key: str, text_cols: tuple[str, ...]) -> str:
        """Emit the ParadeDB `CREATE INDEX ... USING bm25` DDL.

        The key column leads the column list, the text columns follow, and the access method
        takes `WITH (key_field='<key>')`.

        Args:
            index: Name of the index to create.
            table: Fully-qualified table the index covers (e.g. `kb.pages`).
            key: The key column the BM25 access method scores rows by, it also
                leads the indexed-column list.
            text_cols: The text/filter columns to index, in order, after `key`.

        Returns:
            A single ParadeDB `CREATE INDEX` DDL statement for the arguments.
        """
        columns = ", ".join((key, *text_cols))
        return f"CREATE INDEX {index} ON {table} USING bm25 ({columns}) WITH (key_field='{key}')"

    def api_search_body(self) -> str:
        """Return the full `api.search` function definition in ParadeDB grammar.

        Returns:
            The complete `CREATE FUNCTION api.search(...)` statement in ParadeDB grammar.
        """
        return _BUILDER.api_search_body(KB_SEARCH_SCHEMA)
