"""wiki schema: extensions, 10 base tables, BM25/HNSW/GIN/GIST indexes.

Revision ID: 0001
Revises:
Create Date: 2026-06-11 00:00:00.000000

Defines the ``wiki`` schema's base tables + indexes — this migration is the source
of truth for them. The schema is PG-native by design: ``halfvec(1024)`` (pgvector),
``ltree`` paths, and BM25 indexes have no SQLite analogue, and none of this is
autogeneratable — so the DDL is hand-written through ``op.execute``. The migration
owns the schema in full; the ORM maps onto these existing tables.

The ``vector`` and ``ltree`` extensions are created with an explicit ``SCHEMA public``
so their types resolve where the asyncpg client-side codec registration looks them up
(see the inline comment in ``upgrade``); the dialect-routed BM25 extension
(``pg_search`` on ParadeDB, ``pg_textsearch``) is created unqualified
because it is only ever used inside the search_path-pinned ``api.search`` function.

Downgrade irreversibility caveat: ``DROP SCHEMA wiki CASCADE`` also drops the
extensions' dependent objects within the schema, but the cluster-level
``CREATE EXTENSION`` objects (the BM25 extension plus ``vector`` / ``ltree``) are left
in place — extensions are cluster resources other databases/schemas may share, and
dropping them is out of this migration's remit. Re-running ``upgrade`` is the
intended path; ``downgrade`` is a coarse teardown for a disposable dev DB.
"""

from __future__ import annotations

from alembic import context, op

from engram.retrieval.search_dialect import SearchDialect

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _dialect() -> SearchDialect:
    """Return the BM25 :class:`SearchDialect` env.py stashed on ``config.attributes``.

    The channel is ``context.config.attributes["search_dialect"]``, set once in
    ``alembic/env.py`` from the resolved :class:`engram.config.DbDialect` before
    any revision runs. The lookup is narrowed against the runtime-checkable
    :class:`~engram.retrieval.search_dialect.dialect.SearchDialect` Protocol so the
    typed return needs no ``cast``.

    Returns:
        The configured dialect (``pg_search`` by default, ``pg_textsearch`` when
        ``APP_RETRIEVAL__DIALECT=pg_textsearch``).

    Raises:
        RuntimeError: If env.py has not stashed a dialect (revision run outside it).
    """
    dialect = context.config.attributes.get("search_dialect")
    if dialect is None:
        msg = "search_dialect not configured: alembic/env.py must stash it in config.attributes"
        raise RuntimeError(msg)
    if not isinstance(dialect, SearchDialect):
        msg = f"search_dialect must be a SearchDialect, got {type(dialect).__name__}"
        raise TypeError(msg)
    return dialect


def upgrade() -> None:
    """Create the ``wiki`` schema, required extensions, the 10 base tables, and their indexes."""
    op.execute("CREATE SCHEMA IF NOT EXISTS wiki")

    # `vector` + `ltree` are pinned to `public` with an explicit `SCHEMA public`, so their types
    # resolve where the asyncpg client-side codec registration looks them up (pgvector's
    # `register_vector` introspects `public.vector` / `public.halfvec`). An unqualified
    # `CREATE EXTENSION` lands in the first existing schema on the connection's search_path:
    # under a non-superuser `wiki` login role the freshly-created `wiki` schema
    # resolves `"$user"` ahead of `public`, so the types would land in `wiki` and the codec lookup
    # of `public.vector` would fail. ParadeDB pre-seeds these in `public`, so `IF NOT EXISTS` is a
    # no-op there — pinning `public` makes both beds agree. The BM25 extension is dialect-routed
    # (pg_search on ParadeDB, pg_textsearch) and needs NO `SCHEMA public`: its `<@>` /
    # `@@@` operators are only ever invoked inside the `api.search` SECURITY DEFINER body, whose
    # `SET search_path = wiki, public, pg_catalog` already covers wherever the extension landed.
    op.execute(_dialect().create_bm25_extension_ddl())
    op.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS ltree SCHEMA public")

    _create_tables()
    _create_indexes()


def _create_tables() -> None:
    """The 10 base tables."""
    op.execute(
        """
        CREATE TABLE wiki.sources_raw (
          zotero_key  text PRIMARY KEY,
          collection  text NOT NULL DEFAULT 'research',
          title       text NOT NULL,
          year        int,
          venue       text,
          doi         text,
          arxiv_id    text,
          source_hash text NOT NULL,
          pdf_path    text,
          meta        jsonb NOT NULL DEFAULT '{}',
          synced_at   timestamptz
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.raw_chunks (
          id           text PRIMARY KEY,
          zotero_key   text NOT NULL REFERENCES wiki.sources_raw ON DELETE CASCADE,
          section_path ltree,
          heading      text,
          seq          int  NOT NULL,
          body         text NOT NULL,
          tokens       int,
          embedding    halfvec(1024)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.pages (
          id            text PRIMARY KEY,
          collection    text NOT NULL DEFAULT 'research',
          kind          text NOT NULL,
          slug          text NOT NULL,
          title         text NOT NULL,
          aliases       text[] NOT NULL DEFAULT '{}',
          summary       text,
          status        text NOT NULL,
          superseded_by text REFERENCES wiki.pages,
          entity_type   text,
          paper_type    text,
          year          int,
          venue         text,
          citekey       text,
          zotero_key    text REFERENCES wiki.sources_raw,
          source_hash   text,
          maturity      text,
          question_status text,
          tasks         text[] NOT NULL DEFAULT '{}',
          domains       text[] NOT NULL DEFAULT '{}',
          stages        text[] NOT NULL DEFAULT '{}',
          modalities    text[] NOT NULL DEFAULT '{}',
          scale_regimes text[] NOT NULL DEFAULT '{}',
          frontmatter   jsonb NOT NULL DEFAULT '{}',
          card_text     text NOT NULL,
          body_hash     text NOT NULL,
          created       date,
          updated       date,
          embedding     halfvec(1024)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.sections (
          id        text PRIMARY KEY,
          page_id   text NOT NULL REFERENCES wiki.pages ON DELETE CASCADE,
          canon     text,
          heading   text NOT NULL,
          path      ltree NOT NULL,
          ord       int NOT NULL,
          body      text NOT NULL,
          tokens    int,
          body_hash text NOT NULL,
          embedding halfvec(1024)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.claims (
          id           text PRIMARY KEY,
          page_id      text NOT NULL REFERENCES wiki.pages ON DELETE CASCADE,
          block_id     text NOT NULL,
          text         text NOT NULL,
          quote        text,
          loc          text,
          loc_path     ltree,
          kind         text NOT NULL,
          subject_id   text REFERENCES wiki.pages,
          on_dataset   text REFERENCES wiki.pages,
          on_task      text REFERENCES wiki.pages,
          metric       text REFERENCES wiki.pages,
          condition    text,
          baseline     text,
          magnitude    text,
          eval_setting text,
          status       text NOT NULL DEFAULT 'active',
          embedding    halfvec(1024)
        )
        """
    )

    # src_id/dst_id are plain text with no FK — edges are polymorphic over pages AND claims
    # (a FK can't span two tables) and resolved-only, so integrity is owned by the compiler.
    op.execute(
        """
        CREATE TABLE wiki.edges (
          id         bigserial PRIMARY KEY,
          src_id     text NOT NULL,
          src_kind   text NOT NULL,
          rel        text NOT NULL,
          dst_id     text NOT NULL,
          dst_kind   text NOT NULL,
          qualifiers jsonb NOT NULL DEFAULT '{}',
          provenance_claim text
        )
        """
    )

    # claim_refs is a text[] of logical claim references with no FK — an array can't be
    # FK-constrained and a dangling reference must stay representable (loose-coupling-by-design).
    op.execute(
        """
        CREATE TABLE wiki.comparison_cells (
          comparison_id text NOT NULL REFERENCES wiki.pages,
          row_entity    text NOT NULL REFERENCES wiki.pages,
          dimension     text NOT NULL,
          value         text NOT NULL,
          claim_refs    text[] NOT NULL DEFAULT '{}',
          PRIMARY KEY (comparison_id, row_entity, dimension)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.vocab (
          vocab      text NOT NULL,
          value      text NOT NULL,
          definition text NOT NULL,
          meta       jsonb NOT NULL DEFAULT '{}',
          PRIMARY KEY (vocab, value)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.embed_meta (
          model text NOT NULL, dim int NOT NULL, embedded_at timestamptz NOT NULL
        )
        """
    )

    op.execute(
        """
        CREATE TABLE wiki.query_log (
          id          bigserial PRIMARY KEY,
          asked_at    timestamptz NOT NULL DEFAULT now(),
          agent       text,
          question    text NOT NULL,
          archetype   text,
          surfaces    text[] NOT NULL DEFAULT '{}',
          cited_ids   text[] NOT NULL DEFAULT '{}',
          tokens_loaded int,
          wrote_back  text
        )
        """
    )


def _create_indexes() -> None:
    """BM25 (dialect-routed), HNSW (pgvector cosine), GIN facet-array, and GIST ltree indexes."""
    # BM25: one index per searched table, routed through the dialect (ParadeDB pg_search emits
    # today's ``(key, text_cols…) WITH (key_field=…)`` shape verbatim; pg_textsearch
    # emits its ``(text_cols…) WITH (text_config=…)`` shape). The key column leads the col list,
    # the filter columns follow.
    dialect = _dialect()
    op.execute(
        dialect.bm25_index_ddl(
            index="pages_bm25",
            table="wiki.pages",
            key="id",
            text_cols=(
                "title",
                "summary",
                "card_text",
                "kind",
                "status",
                "entity_type",
                "paper_type",
                "year",
                "tasks",
                "domains",
                "stages",
            ),
        )
    )
    op.execute(
        dialect.bm25_index_ddl(index="sections_bm25", table="wiki.sections", key="id", text_cols=("heading", "canon", "body", "page_id"))
    )
    op.execute(
        dialect.bm25_index_ddl(
            index="claims_bm25", table="wiki.claims", key="id", text_cols=("text", "quote", "condition", "kind", "status", "page_id")
        )
    )
    op.execute(
        dialect.bm25_index_ddl(index="raw_chunks_bm25", table="wiki.raw_chunks", key="id", text_cols=("heading", "body", "zotero_key"))
    )

    # ANN (HNSW, cosine on halfvec).
    op.execute("CREATE INDEX pages_hnsw    ON wiki.pages      USING hnsw (embedding halfvec_cosine_ops)")
    op.execute("CREATE INDEX sections_hnsw ON wiki.sections   USING hnsw (embedding halfvec_cosine_ops)")
    op.execute("CREATE INDEX claims_hnsw   ON wiki.claims     USING hnsw (embedding halfvec_cosine_ops)")
    op.execute("CREATE INDEX raw_hnsw      ON wiki.raw_chunks USING hnsw (embedding halfvec_cosine_ops)")

    # Edge adjacency, GIN facet arrays, GIST ltree.
    op.execute("CREATE INDEX edges_src ON wiki.edges (src_id, rel)")
    op.execute("CREATE INDEX edges_dst ON wiki.edges (dst_id, rel)")
    op.execute("CREATE INDEX pages_tasks  ON wiki.pages USING gin (tasks)")
    op.execute("CREATE INDEX pages_stages ON wiki.pages USING gin (stages)")
    op.execute("CREATE INDEX sections_path ON wiki.sections USING gist (path)")


def downgrade() -> None:
    """Drop the ``wiki`` schema and everything in it.

    Coarse by design (see module docstring): the extensions are cluster
    resources and are intentionally left installed.
    """
    op.execute("DROP SCHEMA IF EXISTS wiki CASCADE")
