"""api graph surface: graph_expand + graph_paths, backend-routed (CTE or AGE).

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-17 00:00:03.000000

Adds the link-graph traversal functions to the ``api`` schema — ``graph_expand``
(typed-relation neighbourhood walk) and ``graph_paths`` (shortest enumerated
paths) — completing the agent-facing surface 0002 began. The SQL is **not**
authored here: it is routed through the :class:`GraphBackendSql` instance
``alembic/env.py`` stashes on ``config.attributes["graph_backend"]`` (the same
channel 0002 uses for ``search_dialect``), so the configured backend
(``cte`` on the ParadeDB bed, ``age`` on the local Postgres and the managed target) emits its own traversal
mechanics behind one shared function signature + ``RETURNS`` contract.

The migration only orchestrates. Because alembic runs over **asyncpg** — where a
single ``op.execute`` may carry only ONE SQL command — the backend exposes its DDL
as **lists of single statements** and this migration iterates and executes each.
It runs the backend's one-time projection statements when present (AGE's ``CREATE
EXTENSION age`` + ``create_graph('wiki_kg')`` + ``wiki.edges`` projection, one
command per element; the CTE backend needs none and returns ``[]``), then each
function's ``CREATE FUNCTION`` + its ``COMMENT`` (separate single-command
elements), then the ``GRANT EXECUTE ... TO wiki_reader`` the bodies deliberately
omit (the reader role 0003 created sees ``api`` only — these ``SECURITY DEFINER``
functions run as their owner to read ``wiki.*``). The GRANT signatures match the
backends' ``CREATE FUNCTION`` argument lists exactly.

Downgrade drops both functions and, only for a backend with projection DDL (AGE),
drops the ``wiki_kg`` graph — the empty-``create_graph_ddl()`` guard makes the
graph drop a no-op under the CTE backend (which never created one).
"""

from __future__ import annotations

from alembic import context, op

from engram.retrieval.graph_backend import GraphBackendSql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

# The api.graph_* function signatures (the GRANT/DROP arg lists) — byte-identical to the
# CREATE FUNCTION clauses both backends emit, so the grant/drop resolve to those functions.
_EXPAND_SIGNATURE = "api.graph_expand(text[], integer, text[], integer)"
_PATHS_SIGNATURE = "api.graph_paths(text, text, integer, text[], integer)"


def _backend() -> GraphBackendSql:
    """Return the :class:`GraphBackendSql` env.py stashed on ``config.attributes``.

    The channel is ``context.config.attributes["graph_backend"]``, set once in
    ``alembic/env.py`` from the resolved :class:`engram.config.GraphBackend`
    before any revision runs (mirroring how 0002 reads ``search_dialect``). The
    lookup is narrowed against the runtime-checkable :class:`GraphBackendSql`
    Protocol so the typed return needs no ``cast``.

    Returns:
        The configured backend (``cte`` by default, ``age`` when
        ``APP_RETRIEVAL__GRAPH_BACKEND=age``).

    Raises:
        RuntimeError: If env.py has not stashed a backend (revision run outside it).
        TypeError: If the stashed object does not satisfy :class:`GraphBackendSql`.
    """
    backend = context.config.attributes.get("graph_backend")
    if backend is None:
        msg = "graph_backend not configured: alembic/env.py must stash it in config.attributes"
        raise RuntimeError(msg)
    if not isinstance(backend, GraphBackendSql):
        msg = f"graph_backend must be a GraphBackendSql, got {type(backend).__name__}"
        raise TypeError(msg)
    return backend


def upgrade() -> None:
    """Project the graph (AGE only), create both functions, and grant them to ``wiki_reader``."""
    backend = _backend()

    # alembic runs over asyncpg, where one statement may carry only a SINGLE command — so the
    # backend hands us LISTS of single statements and we execute each on its own. We send each
    # via the raw driver (``exec_driver_sql``) rather than ``op.execute``: ``op.execute`` wraps a
    # bare str in ``text()``, whose bind-parameter parser treats the AGE backend's openCypher
    # colon-tokens (``(:Node)``, ``[*..]->`` labels, ``$seed_ids`` params) as ``:name`` binds and
    # raises (CTE has no colons, so it survived — but the literal path is correct for both).
    #
    # The one-time projection runs ONLY when the backend needs it: AGE returns CREATE EXTENSION age +
    # create_graph + the wiki.edges projection; the CTE backend returns [] (walks wiki.edges directly).
    # AGE's projection writes to the ``ag_catalog`` and MUST commit statement-by-statement: bundling it
    # into alembic's single migration transaction makes Postgres abort the whole migration with
    # "ResourceOwnerEnlarge called after release started" — the catalog writes and the subsequent
    # ``cypher`` cannot share one outer transaction (the AGE guidance runs them as separate
    # autocommitted psql statements). So when projection DDL exists we run it inside ``autocommit_block()``:
    # it commits the migration txn and executes each statement in its own autocommit, then resumes the txn.
    # The CTE backend's empty list skips the block entirely, so its transactional behaviour is unchanged.
    projection = backend.create_graph_ddl()
    if projection:
        with op.get_context().autocommit_block():
            age_bind = op.get_bind()
            for statement in projection:
                age_bind.exec_driver_sql(statement)

    bind = op.get_bind()

    # The function definitions + their M-Schema COMMENTs come straight from the backend (the SQL
    # and RETURNS columns are owned there, not redefined here); each element is one command.
    for statement in (*backend.expand_statements(), *backend.paths_statements(), *backend.refresh_statements()):
        bind.exec_driver_sql(statement)

    # The reader role 0003 created sees api only; these SECURITY DEFINER functions need an
    # explicit EXECUTE grant (the bodies leave the GRANT to this migration by design). The GRANTs
    # carry no colons, but go through the same literal driver path for uniformity.
    bind.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {_EXPAND_SIGNATURE} TO wiki_reader")
    bind.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {_PATHS_SIGNATURE} TO wiki_reader")


def downgrade() -> None:
    """Drop both functions, and (AGE only) the ``wiki_kg`` graph.

    Dropping the functions is backend-agnostic. The graph drop is AGE-only (gated by the
    ``create_graph_ddl()`` signal the CTE backend leaves empty) and must stay robust three ways.
    It is **schema-qualified** (``ag_catalog.drop_graph`` — AGE installs its functions in
    ``ag_catalog``, which is not on the migration's ``search_path``, so a bare ``drop_graph``
    raises ``UndefinedFunction``). It is **existence-guarded** (drops only when the ``ag_catalog``
    schema and the ``wiki_kg`` graph are actually present), so a downgrade under the AGE backend
    on a database that never projected the graph is a safe no-op rather than a ``graph does not
    exist`` error. And like the upgrade's projection, it runs in ``autocommit_block()`` because
    AGE's catalog-mutating DDL cannot share alembic's single migration transaction.
    """
    bind = op.get_bind()
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {_PATHS_SIGNATURE}")
    bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {_EXPAND_SIGNATURE}")
    bind.exec_driver_sql("DROP FUNCTION IF EXISTS wiki.refresh_graph()")

    if _backend().create_graph_ddl():
        with op.get_context().autocommit_block():
            op.get_bind().exec_driver_sql(
                "DO $$ BEGIN "
                "IF to_regnamespace('ag_catalog') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'wiki_kg') "
                "THEN PERFORM ag_catalog.drop_graph('wiki_kg', true); END IF; "
                "END $$"
            )
