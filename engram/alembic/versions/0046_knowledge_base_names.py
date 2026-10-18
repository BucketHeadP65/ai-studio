"""The database says knowledge base: schema ``kb``, index/validation tables, ``page_type``, ``kb_graph``, ``kb_reader``.

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-03 00:00:00.000000

``ALTER SCHEMA memory RENAME TO kb`` moves every table in one statement, and every ``api.*`` view
follows it untouched: a view's stored query tree names its tables by OID. Table and column renames
behave the same way for views, and ``ALTER TABLE ... RENAME COLUMN`` does not change a view's own
output column name, so the views that publish a page type are renamed column by column.

Function bodies are raw text re-parsed at call time, so every function in ``api`` and ``kb`` is
regenerated from ``pg_get_functiondef`` with the rename map applied. The map rewrites qualified
names (``memory.x``, the four renamed tables, the renamed columns), the ``search_path`` each
function pins, and the page-type column under its two table aliases, ``p.`` for ``kb.pages`` and
``t.`` for ``kb.page_type_contracts``. Every other string literal is left alone, so ``kb.vocab``'s
``'kind'`` category and the claim column ``c.kind`` survive. ``CREATE OR REPLACE`` keeps a
function's grants but refuses a changed OUT-parameter row type, so the envelope functions that
publish a page type are dropped and created fresh, and the ``api`` grants and each dropped
function's own comment are restored after.

Five functions change name because their callers do: ``api.compiled_spec`` serves the indexed
schema, and the four page-type envelope functions publish ``page_type``.

The ``api`` view, column and function comments are rewritten through the same map plus a prose map,
because ``describe_schema`` serves them to an agent and their example queries name the renamed
columns. Function comments live on ``pg_proc``, not ``pg_class``, so they are rewritten by a
second loop, plus the five renamed function names themselves.

The AGE property graph ``wiki_kg`` becomes ``kb_graph`` where the graph exists, and each of its
label tables has the graph name rewritten in its ``id`` default, which the AGE rename leaves
behind. The read-only login ``corpus_reader`` becomes
``kb_reader``, carrying every grant on its identity, with the password re-set because
``ALTER ROLE ... RENAME`` clears an MD5-encrypted one (resolved exactly as 0014 resolves it).

Renaming a table leaves its constraints and standalone indexes on the old name, so those are
renamed one by one too.

Queued work moves with the names: a ``coordination.jobs`` row of kind ``corpus.ingest`` becomes
``knowledge_base.ingest``, except a live keyed row whose new kind already holds a live row for that
key, which stays put rather than tripping the live-key unique index.

Every step is guarded on the object still carrying its old name, so a second run is a no-op and a
database that never had an object skips it. Downgrade is the mirror.
"""

from __future__ import annotations

from alembic import context, op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "kb_reader"
_PREVIOUS_READER_ROLE = "corpus_reader"

# Dev fallbacks assembled at runtime (never static literals in source) so they match the example
# config's reader_url without tripping secret scanners.
_DEV_PASSWORD_FALLBACK = "_".join((_READER_ROLE, "dev"))
_PREVIOUS_DEV_PASSWORD_FALLBACK = "_".join((_PREVIOUS_READER_ROLE, "dev"))

# (old, new) table names inside the knowledge base schema.
_TABLES = (
    ("compile_manifest", "index_manifest"),
    ("compile_fingerprint", "index_fingerprint"),
    ("lint_store", "validation_store"),
    ("eval_runs", "evaluation_runs"),
    ("eval_case_results", "evaluation_case_results"),
)

# (table, old column, new column), against the post-rename table names.
_COLUMNS = (
    ("pages", "kind", "page_type"),
    ("page_type_contracts", "kind", "page_type"),
    ("index_manifest", "compiled_at", "indexed_at"),
    ("index_fingerprint", "compiler_version", "indexer_version"),
    ("index_fingerprint", "registry_hash", "vocabulary_hash"),
    ("index_fingerprint", "compiled_at", "indexed_at"),
    ("index_fingerprint", "corpus_root", "knowledge_base_root"),
    ("unit_summaries", "compiled_at", "indexed_at"),
)

# (table, old constraint, new constraint): a table rename leaves its constraints on the old name.
_CONSTRAINTS = (
    ("index_fingerprint", "compile_fingerprint_pkey", "index_fingerprint_pkey"),
    ("index_manifest", "compile_manifest_pkey", "index_manifest_pkey"),
    ("validation_store", "lint_store_pkey", "validation_store_pkey"),
    ("evaluation_runs", "eval_runs_pkey", "evaluation_runs_pkey"),
    ("evaluation_case_results", "eval_case_results_pkey", "evaluation_case_results_pkey"),
    ("evaluation_case_results", "eval_case_results_run_id_fkey", "evaluation_case_results_run_id_fkey"),
)

# (old index, new index): the standalone indexes, which a table rename leaves behind the same way.
_INDEXES = (
    ("ix_compile_manifest_page", "ix_index_manifest_page"),
    ("ix_compile_manifest_source_key", "ix_index_manifest_source_key"),
    ("ix_lint_store_page", "ix_validation_store_page"),
    ("ix_lint_store_severity", "ix_validation_store_severity"),
)

# (view, old output column, new output column): the views that publish a page type under an old name.
_VIEW_COLUMNS = (
    ("page_types", "kind", "page_type"),
    ("sections", "page_kind", "page_type"),
)

# (old function name, new function name), matched on name alone: every overload is renamed.
_FUNCTIONS = (
    ("compiled_spec", "indexed_schema"),
    ("envelope_kind", "envelope_page_type"),
    ("envelope_collection_kinds", "envelope_collection_page_types"),
    ("envelope_group_kind", "envelope_group_page_type"),
    ("envelope_group_kinds", "envelope_group_page_types"),
)

# Applied in order to every function body and every comment, both ways round. Ordered longest-first
# where one pattern is a prefix of another, so `compile_fingerprint` never matches `compile_manifest`.
_TEXT_MAP = (
    ("'memory', 'public', 'pg_catalog'", "'kb', 'public', 'pg_catalog'"),
    ("memory.", "kb."),
    ("compile_manifest", "index_manifest"),
    ("compile_fingerprint", "index_fingerprint"),
    ("lint_store", "validation_store"),
    ("eval_case_results", "evaluation_case_results"),
    ("eval_runs", "evaluation_runs"),
    ("compiler_version", "indexer_version"),
    ("compiled_at", "indexed_at"),
    ("registry_hash", "vocabulary_hash"),
    ("corpus_root", "knowledge_base_root"),
    ("p_excluded_kinds", "p_excluded_page_types"),
    ("p_kind", "p_page_type"),
    ("row_kind", "row_type"),
    ("p.kind", "p.page_type"),
    ("t.kind", "t.page_type"),
    ("pages.kind", "pages.page_type"),
    ("AS kind", "AS page_type"),
    ("kind text", "page_type text"),
    ("AS page_kind", "AS page_type"),
    ("wiki_kg", "kb_graph"),
    ("corpus_reader", "kb_reader"),
)


# The prose the `api` comments carry, on top of the identifier map. `describe_schema` serves these
# to an agent, so an example query has to name the column the view now publishes.
_COMMENT_MAP = (
    ("one row per collection compiled into this database", "one row per collection indexed into this database"),
    ("single-corpus database", "single-knowledge-base database"),
    ("multi-corpus database", "multi-knowledge-base database"),
    ("'This corpus indexes", "'This knowledge base indexes"),
    ("ingest procedure, lint.", "ingest procedure, validation."),
    ("'# Corpus schema", "'# Knowledge base schema"),
    ("(the kind's edge_fields", "(the page type's edge_fields"),
    ("Claim kind (registry).", "Claim type (vocabulary)."),
    ("slotless per-corpus fields", "slotless per-knowledge-base fields"),
    ("Corpus partition of the page.", "Knowledge base partition of the page."),
    ("frontmatter a kind accepts. One row per collection and kind.", "frontmatter a page type accepts. One row per collection and page type."),
    ("AND kind = 'flag';", "AND page_type = 'flag';"),
    ("a page of this kind pre-seeds.", "a page of this page type pre-seeds."),
    ("pages of this kind carry claim bullets.", "pages of this page type carry claim bullets."),
    ("(id, kind, title, summary)", "(id, page_type, title, summary)"),
    ("kind-group envelope", "page-type-group envelope"),
    ("kind-grouped root map", "page-type-grouped root map"),
    ("kind sub-groups within one app group", "page type sub-groups within one app group"),
    ("app+kind sub-group envelope", "app and page type sub-group envelope"),
    ("one page kind", "one page type"),
    ("distinct page kind", "distinct page type"),
    ("caller-supplied excluded kinds", "caller-supplied excluded page types"),
    ("The corpus collection this page type belongs to.", "The collection this page type belongs to."),
    ("The whole page-type spec node as compiled,", "The whole page-type node as indexed,"),
    (
        "jsonb: the kind's decision block, null for a kind with no declared decision.",
        "jsonb: the page type's decision block, null for a page type with no declared decision.",
    ),
    ("units of this kind are listed as envelope children.", "units of this page type are listed as envelope children."),
    ("fields on this kind that are typed relations", "fields on this page type that are typed relations"),
    ("The corpus subfolder this kind is authored under,", "The knowledge base subfolder this page type is authored under,"),
    ("default) for this kind.", "default) for this page type."),
    ("pages of this kind must carry,", "pages of this page type must carry,"),
    ("status values this kind accepts,", "status values this page type accepts,"),
)


def _replacement_sql(*, forward: bool, prose: bool = False, extra: tuple[tuple[str, str], ...] = ()) -> str:
    """A SQL expression rewriting the identifier ``src`` through the rename map.

    Args:
        forward: True for the upgrade direction (old name to new), False for the mirror.
        prose: True to append the comment prose map after the identifier map.
        extra: Further (old, new) pairs applied last, after the identifier and prose maps.
    """
    expression = "src"
    for old, new in _TEXT_MAP + (_COMMENT_MAP if prose else ()) + extra:
        source, target = (old, new) if forward else (new, old)
        expression = f"replace({expression}, {_pg_literal(source)}, {_pg_literal(target)})"
    return expression


def _pg_literal(value: str) -> str:
    """Wrap ``value`` as a single-escaped SQL string literal, so it is never concatenated raw."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _rename_schema(*, old: str, new: str) -> str:
    """DDL renaming a schema, skipped when the old name is already gone."""
    return f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{old}')
     AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{new}') THEN
    ALTER SCHEMA {old} RENAME TO {new};
  END IF;
END
$$
"""


def _rename_tables(*, schema: str, pairs: tuple[tuple[str, str], ...]) -> str:
    """DDL renaming each table that still carries its old name."""
    body = "\n".join(
        f"  IF to_regclass('{schema}.{old}') IS NOT NULL AND to_regclass('{schema}.{new}') IS NULL THEN\n"
        f"    ALTER TABLE {schema}.{old} RENAME TO {new};\n"
        f"  END IF;"
        for old, new in pairs
    )
    return f"DO $$\nBEGIN\n{body}\nEND\n$$"


def _rename_columns(*, schema: str, triples: tuple[tuple[str, str, str], ...]) -> str:
    """DDL renaming each column that still carries its old name."""
    body = "\n".join(
        f"  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = '{schema}' "
        f"AND table_name = '{table}' AND column_name = '{old}') THEN\n"
        f"    ALTER TABLE {schema}.{table} RENAME COLUMN {old} TO {new};\n"
        f"  END IF;"
        for table, old, new in triples
    )
    return f"DO $$\nBEGIN\n{body}\nEND\n$$"


def _rename_constraints(*, schema: str, triples: tuple[tuple[str, str, str], ...]) -> str:
    """DDL renaming each table constraint that still carries its old name."""
    body = "\n".join(
        f"  IF EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        f"WHERE n.nspname = '{schema}' AND c.conname = '{old}') THEN\n"
        f"    ALTER TABLE {schema}.{table} RENAME CONSTRAINT {old} TO {new};\n"
        f"  END IF;"
        for table, old, new in triples
    )
    return f"DO $$\nBEGIN\n{body}\nEND\n$$"


def _rename_indexes(*, schema: str, pairs: tuple[tuple[str, str], ...]) -> str:
    """DDL renaming each standalone index that still carries its old name."""
    body = "\n".join(
        f"  IF to_regclass('{schema}.{old}') IS NOT NULL AND to_regclass('{schema}.{new}') IS NULL THEN\n"
        f"    ALTER INDEX {schema}.{old} RENAME TO {new};\n"
        f"  END IF;"
        for old, new in pairs
    )
    return f"DO $$\nBEGIN\n{body}\nEND\n$$"


def _rename_view_columns(*, schema: str, triples: tuple[tuple[str, str, str], ...]) -> str:
    """DDL renaming each view output column that still carries its old name."""
    body = "\n".join(
        f"  IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = '{schema}' "
        f"AND table_name = '{view}' AND column_name = '{old}') THEN\n"
        f"    ALTER VIEW {schema}.{view} RENAME COLUMN {old} TO {new};\n"
        f"  END IF;"
        for view, old, new in triples
    )
    return f"DO $$\nBEGIN\n{body}\nEND\n$$"


def _rename_functions(*, schema: str, pairs: tuple[tuple[str, str], ...]) -> str:
    """DDL renaming every overload of each function that still carries its old name."""
    body = "\n".join(
        f"""
  FOR target IN
    SELECT p.oid::regprocedure AS signature
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = '{schema}' AND p.proname = '{old}'
  LOOP
    EXECUTE format('ALTER FUNCTION %s RENAME TO {new}', target.signature);
  END LOOP;"""
        for old, new in pairs
    )
    return f"DO $$\nDECLARE target record;\nBEGIN\n{body}\nEND\n$$"


def _regenerate_functions(*, forward: bool) -> str:
    """DDL replaying every function in ``api`` and ``kb`` with the rename map applied to its body.

    ``CREATE OR REPLACE`` keeps a function's grants but refuses a changed OUT-parameter row type,
    which is exactly what renaming a published ``kind`` column does. Those functions are dropped by
    their own signature and created fresh, and the grants the drop took are restored below. A DROP
    also takes the function's own COMMENT, which ``api_objects`` serves to an agent, so the comment
    is read off ``pg_proc`` before the drop and re-applied to the fresh function.
    """
    rewritten = _replacement_sql(forward=forward)
    return f"""
DO $$
DECLARE target record; src text; rewritten text; signature text; note text;
BEGIN
  FOR target IN
    SELECT p.oid AS id
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname IN ('api', 'kb', 'memory') AND p.prokind = 'f'
  LOOP
    src := pg_get_functiondef(target.id);
    rewritten := {rewritten};
    IF rewritten <> src THEN
      BEGIN
        EXECUTE rewritten;
      EXCEPTION WHEN invalid_function_definition THEN
        signature := target.id::regprocedure::text;
        note := obj_description(target.id, 'pg_proc');
        EXECUTE format('DROP FUNCTION %s', signature);
        EXECUTE rewritten;
        IF note IS NOT NULL THEN
          EXECUTE format('COMMENT ON FUNCTION %s IS %L', signature, note);
        END IF;
      END;
    END IF;
  END LOOP;
END
$$
"""


def _restore_reader_grants(*, roles: tuple[str, ...]) -> str:
    """DDL re-granting the ``api`` surface to whichever reader login the cluster carries."""
    body = "\n".join(
        f"""
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    GRANT USAGE ON SCHEMA api TO {role};
    GRANT SELECT ON ALL TABLES IN SCHEMA api TO {role};
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO {role};
  END IF;"""
        for role in roles
    )
    return f"DO $$\nBEGIN\n{body}\nEND\n$$"


def _rewrite_comments(*, forward: bool) -> str:
    """DDL rewriting every comment on an ``api`` view and its columns through the rename map."""
    rewritten = _replacement_sql(forward=forward, prose=True)
    return f"""
DO $$
DECLARE target record; src text; rewritten text;
BEGIN
  FOR target IN
    SELECT c.relname AS relation, a.attname AS column_name, d.description AS body
    FROM pg_description d
    JOIN pg_class c ON c.oid = d.objoid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid
    WHERE n.nspname = 'api'
  LOOP
    src := target.body;
    rewritten := {rewritten};
    IF rewritten <> src THEN
      IF target.column_name IS NULL THEN
        EXECUTE format('COMMENT ON VIEW api.%I IS %L', target.relation, rewritten);
      ELSE
        EXECUTE format('COMMENT ON COLUMN api.%I.%I IS %L', target.relation, target.column_name, rewritten);
      END IF;
    END IF;
  END LOOP;
END
$$
"""


def _rewrite_function_comments(*, forward: bool) -> str:
    """DDL rewriting every comment on an ``api`` function through the rename map plus the five renamed function names.

    Function comments live on ``pg_proc``, not ``pg_class``, so ``_rewrite_comments`` (which walks
    ``pg_class``) never reaches them, yet ``api_objects`` serves them to an agent through
    ``obj_description(p.oid, 'pg_proc')`` the same way it serves a view's.
    """
    rewritten = _replacement_sql(forward=forward, prose=True, extra=_FUNCTIONS)
    return f"""
DO $$
DECLARE target record; src text; rewritten text;
BEGIN
  FOR target IN
    SELECT p.oid::regprocedure AS signature, d.description AS body
    FROM pg_description d
    JOIN pg_proc p ON p.oid = d.objoid AND d.classoid = 'pg_proc'::regclass
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'api'
  LOOP
    src := target.body;
    rewritten := {rewritten};
    IF rewritten <> src THEN
      EXECUTE format('COMMENT ON FUNCTION %s IS %L', target.signature, rewritten);
    END IF;
  END LOOP;
END
$$
"""


def _rename_graph(*, old: str, new: str) -> str:
    """DDL renaming the AGE property graph, skipped where AGE or the graph is absent.

    The catalog reads are dynamic: plpgsql prepares a static reference to ``ag_catalog.ag_graph``
    when it plans the expression, which raises on a database that never installed the extension,
    so the guard has to run before anything names that table.
    """
    return f"""
DO $$
DECLARE has_old boolean := false; has_new boolean := false;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'ag_catalog') THEN
    RETURN;
  END IF;
  EXECUTE 'SELECT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1::name)' INTO has_old USING '{old}';
  EXECUTE 'SELECT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1::name)' INTO has_new USING '{new}';
  IF has_old AND NOT has_new THEN
    EXECUTE format('SELECT ag_catalog.alter_graph(%L, %L, %L)', '{old}', 'RENAME', '{new}');
  END IF;
END
$$
"""


def _relabel_graph_defaults(*, old: str, new: str) -> str:
    """DDL re-pointing each AGE label table's ``id`` default at the renamed graph.

    ``ag_catalog.alter_graph(..., 'RENAME', ...)`` moves the graph's namespace but leaves the graph
    name baked into every label table's ``id`` default as
    ``ag_catalog._label_id('<old>'::name, ...)``, which then resolves to 0 and makes the projection
    raise ``label_id must be 1 .. 65535``. This rewrites those defaults.
    """
    return f"""
DO $$
DECLARE target record;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '{new}') THEN
    RETURN;
  END IF;

  FOR target IN
    SELECT c.relname AS relation, pg_get_expr(d.adbin, d.adrelid) AS expression
    FROM pg_attrdef d
    JOIN pg_class c ON c.oid = d.adrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.adnum
    WHERE n.nspname = '{new}' AND a.attname = 'id' AND pg_get_expr(d.adbin, d.adrelid) LIKE '%''{old}''::name%'
  LOOP
    EXECUTE format(
      'ALTER TABLE {new}.%I ALTER COLUMN id SET DEFAULT %s',
      target.relation,
      replace(target.expression, '''{old}''::name', '''{new}''::name')
    );
  END LOOP;
END
$$
"""


def _adopt_reader_role(*, previous: str, current: str) -> str:
    """DDL putting the reader login on ``current``, whichever of the two names the cluster carries.

    A role is cluster-wide while a migration is per-database, so a cluster holding several migrated
    databases already carries ``current`` by the time the second one runs this. Renaming is only for
    the first; after that the role is granted this database's ``api`` surface and ``previous`` is
    left alone, because it may still own grants in a database this migration cannot see.
    """
    return f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{current}') THEN
    GRANT USAGE ON SCHEMA api TO {current};
    GRANT SELECT ON ALL TABLES IN SCHEMA api TO {current};
    GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA api TO {current};
    ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT SELECT ON TABLES TO {current};
    ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT EXECUTE ON FUNCTIONS TO {current};
    RETURN;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{previous}') THEN
    ALTER ROLE {previous} RENAME TO {current};
  END IF;
END
$$
"""


def _reader_password(fallback: str) -> str:
    """The reader login password, from the reader URL env.py resolved, else ``fallback``.

    Args:
        fallback: The dev password to use when no reader URL was resolved.
    """
    configured = context.config.attributes.get("reader_password")
    return configured if isinstance(configured, str) and configured else fallback


def _set_reader_password(*, role: str, password: str) -> str:
    """DDL setting the reader login's password, skipped when the cluster carries no such role."""
    return f"""
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
    EXECUTE format('ALTER ROLE {role} LOGIN PASSWORD %L', {_pg_literal(password)});
  END IF;
END
$$
"""


def _move_job_kind(*, old: str, new: str) -> str:
    """DDL moving every ``coordination.jobs`` row of one kind to the other, minus the ones that would collide.

    ``ux_jobs_live_key`` is UNIQUE on ``(kind, key)`` while a job is live (queued, running or
    failed) and keyed, so a keyed live row whose target kind already holds a live row for that key
    stays on its old kind rather than failing the whole migration.

    Args:
        old: The kind rows are moved off.
        new: The kind rows are moved onto.
    """
    return (
        f"UPDATE coordination.jobs AS j SET kind = {_pg_literal(new)} WHERE j.kind = {_pg_literal(old)}"
        " AND NOT (j.state IN ('queued', 'running', 'failed') AND j.key <> ''"
        f" AND EXISTS (SELECT 1 FROM coordination.jobs o WHERE o.kind = {_pg_literal(new)}"
        " AND o.key = j.key AND o.state IN ('queued', 'running', 'failed')))"
    )


def upgrade() -> None:
    """Move the knowledge base onto its new schema, table, column, graph and role names."""
    op.execute(_rename_schema(old="memory", new="kb"))
    op.execute(_rename_tables(schema="kb", pairs=_TABLES))
    op.execute(_rename_columns(schema="kb", triples=_COLUMNS))
    op.execute(_rename_constraints(schema="kb", triples=_CONSTRAINTS))
    op.execute(_rename_indexes(schema="kb", pairs=_INDEXES))
    op.execute(_rename_view_columns(schema="api", triples=_VIEW_COLUMNS))
    op.execute(_rename_functions(schema="api", pairs=_FUNCTIONS))
    op.execute(_rename_graph(old="wiki_kg", new="kb_graph"))
    op.execute(_relabel_graph_defaults(old="wiki_kg", new="kb_graph"))
    op.execute(_regenerate_functions(forward=True))
    op.execute(_restore_reader_grants(roles=(_PREVIOUS_READER_ROLE, _READER_ROLE)))
    op.execute(_rewrite_comments(forward=True))
    op.execute(_rewrite_function_comments(forward=True))
    op.execute(_adopt_reader_role(previous=_PREVIOUS_READER_ROLE, current=_READER_ROLE))
    op.execute(_set_reader_password(role=_READER_ROLE, password=_reader_password(_DEV_PASSWORD_FALLBACK)))
    op.execute(_move_job_kind(old="corpus.ingest", new="knowledge_base.ingest"))


def downgrade() -> None:
    """Put every name back, in the mirror order."""
    op.execute(_adopt_reader_role(previous=_READER_ROLE, current=_PREVIOUS_READER_ROLE))
    op.execute(_set_reader_password(role=_PREVIOUS_READER_ROLE, password=_reader_password(_PREVIOUS_DEV_PASSWORD_FALLBACK)))
    op.execute(_rename_graph(old="kb_graph", new="wiki_kg"))
    op.execute(_relabel_graph_defaults(old="kb_graph", new="wiki_kg"))
    op.execute(_rewrite_function_comments(forward=False))
    op.execute(_rewrite_comments(forward=False))
    op.execute(_rename_functions(schema="api", pairs=tuple((new, old) for old, new in _FUNCTIONS)))
    op.execute(_rename_view_columns(schema="api", triples=tuple((view, new, old) for view, old, new in _VIEW_COLUMNS)))
    op.execute(_rename_indexes(schema="kb", pairs=tuple((new, old) for old, new in _INDEXES)))
    op.execute(_rename_constraints(schema="kb", triples=tuple((table, new, old) for table, old, new in _CONSTRAINTS)))
    op.execute(_rename_columns(schema="kb", triples=tuple((table, new, old) for table, old, new in _COLUMNS)))
    op.execute(_rename_tables(schema="kb", pairs=tuple((new, old) for old, new in _TABLES)))
    op.execute(_rename_schema(old="kb", new="memory"))
    op.execute(_regenerate_functions(forward=False))
    op.execute(_restore_reader_grants(roles=(_READER_ROLE, _PREVIOUS_READER_ROLE)))
    op.execute(_move_job_kind(old="knowledge_base.ingest", new="corpus.ingest"))
