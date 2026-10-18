"""The `SearchDialect` port, the backend-specific BM25 SQL contract.

A dialect satisfies it by shape alone and needs no inheritance.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class SearchDialect(Protocol):
    """Emits backend-specific BM25 SQL.

    The vector legs and the RRF merge are the same for every dialect and are not part of this contract.

    Attributes:
        name: Which backend this dialect targets, `"pg_search"` (ParadeDB's extension) or `"pg_textsearch"` (the
            extension the local Postgres ships).
    """

    name: Literal["pg_search", "pg_textsearch"]

    def create_bm25_extension_ddl(self) -> str:
        """Emit the `CREATE EXTENSION` statement for this backend's BM25 access method.

        The extension is `pg_search` or `pg_textsearch`, after the dialect.

        Returns:
            A single `CREATE EXTENSION` DDL statement in this backend's grammar.
        """
        ...

    def bm25_index_ddl(self, *, index: str, table: str, key: str, text_cols: tuple[str, ...]) -> str:
        """Emit the `CREATE INDEX ... USING bm25 (...)` DDL for one BM25 index.

        You must pass static, trusted identifiers, never runtime or user-supplied input.
        The arguments are interpolated into the DDL as SQL identifiers.

        Args:
            index: Name of the index to create.
            table: Fully-qualified table the index covers (e.g. `kb.pages`).
            key: The key column the BM25 access method scores rows by.
            text_cols: The text columns to index for lexical matching.

        Returns:
            A single `CREATE INDEX` DDL statement in this backend's BM25 grammar.
        """
        ...

    def api_search_body(self) -> str:
        """Return the full `CREATE OR REPLACE FUNCTION api.search(...)` body for this dialect.

        Returns:
            The complete `api.search` function definition (BM25 legs in this
            backend's grammar, identical vector legs and RRF merge).
        """
        ...
