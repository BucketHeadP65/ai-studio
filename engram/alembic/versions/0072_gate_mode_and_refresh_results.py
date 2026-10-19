"""`public.schedules` keeps a schedule's gate behaviour in `gate_mode` alone, every finished refresh stores its pass document, and `api.indexed_schema` goes.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-19 00:00:00.000000

`auto_approve_gates` said only what `gate_mode = 'auto'` says. A row whose switch is on runs auto, so its `gate_mode`
becomes `'auto'` before the column is dropped, and every other row keeps its mode. Downgrade adds the column back and
derives it from `gate_mode`, true exactly when the mode is `'auto'`, with 0030's comment on `gate_mode` set again.

A finished `kb.refresh` job stores its result as the JSON document a clean pass writes, a one-line `detail` and its
`counts`. A result stored as plain text becomes that document, the text as its detail and every count zero. Downgrade
leaves the documents as they are.

`api.indexed_schema` served the schema an index ran with to readers, and nothing reads it. It is dropped, and the comment
on `kb.index_fingerprint.spec` stops naming a reader. Downgrade creates the function again as 0071 holds it, with its
comment and its grant, and restores the column's comment.
"""

from __future__ import annotations

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_GATE_MODE_COMMENT = (
    "COMMENT ON COLUMN public.schedules.gate_mode IS "
    "'wait parks a person gate for a person, orchestrate hands it to the run''s own orchestrator, "
    "auto passes it with nobody asked.'"
)

# 0030's comment, byte-identical: what `downgrade` restores.
_PREVIOUS_GATE_MODE_COMMENT = (
    "COMMENT ON COLUMN public.schedules.gate_mode IS "
    "'wait parks a human gate, orchestrate hands it to the run''s own orchestrator, "
    "auto passes it with nobody asked -- the same thing auto_approve_gates true means.'"
)

_READER_ROLE = "kb_reader"

_INDEXED_SCHEMA = "api.indexed_schema(text)"

_SPEC_COLUMN_COMMENT = (
    "COMMENT ON COLUMN kb.index_fingerprint.spec IS "
    "'The validated knowledge base schema (as jsonb) the last index ran with. Null on a row a page write created, "
    "until the collection is indexed. No reader is granted it.'"
)

# 0071's function, comment and column comment, byte-identical: what `downgrade` restores.
_PREVIOUS_INDEXED_SCHEMA_FUNCTION = """
CREATE FUNCTION api.indexed_schema(p_collection text)
  RETURNS jsonb
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, kb, public, pg_temp AS $$
    SELECT f.spec FROM kb.index_fingerprint f
    WHERE f.collection = p_collection AND api.visible_collection(p_collection)
$$
"""

_PREVIOUS_INDEXED_SCHEMA_COMMENT = (
    f"COMMENT ON FUNCTION {_INDEXED_SCHEMA} IS "
    "'The validated CorpusSpec (as jsonb) the last compile ran with for one collection, or SQL NULL when that "
    "collection has never compiled, compiled before this column existed, or is not visible to this session. The one "
    "reader-granted door onto index_fingerprint -- the table itself stays ungranted. A restored database with no "
    "checkout reads its own corpus spec through here instead of corpusspec.json. Example: SELECT "
    "api.indexed_schema(<collection>);'"
)

_PREVIOUS_SPEC_COLUMN_COMMENT = (
    "COMMENT ON COLUMN kb.index_fingerprint.spec IS "
    "'The validated CorpusSpec (as jsonb) the compiler ran with, stamped alongside the rest of the fingerprint row on "
    "every compile; served to readers only through api.compiled_spec, never by a direct grant on this table.'"
)

_REFRESH_RESULTS_AS_DOCUMENTS = """
UPDATE coordination.jobs
SET result = jsonb_build_object(
    'detail', result,
    'counts', jsonb_build_object('sources_created', 0, 'requested_by_entry', '{}'::jsonb, 'created_by_connector', '{}'::jsonb)
)::text
WHERE kind = 'kb.refresh' AND state = 'done' AND result IS NOT NULL AND result <> '' AND NOT pg_input_is_valid(result, 'jsonb')
"""


def upgrade() -> None:
    """Fold `auto_approve_gates` into `gate_mode`, store every text refresh result as its pass document, and drop `api.indexed_schema`."""
    op.execute("UPDATE public.schedules SET gate_mode = 'auto' WHERE auto_approve_gates AND gate_mode <> 'auto'")
    op.execute("ALTER TABLE public.schedules DROP COLUMN auto_approve_gates")
    op.execute(_GATE_MODE_COMMENT)
    op.execute(_REFRESH_RESULTS_AS_DOCUMENTS)
    op.execute(f"DROP FUNCTION {_INDEXED_SCHEMA}")
    op.execute(_SPEC_COLUMN_COMMENT)


def downgrade() -> None:
    """Create `api.indexed_schema` again, then add `auto_approve_gates` back, derived from `gate_mode`, and restore 0030's comment."""
    op.execute(_PREVIOUS_INDEXED_SCHEMA_FUNCTION)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_INDEXED_SCHEMA} TO {_READER_ROLE}")
    op.execute(_PREVIOUS_INDEXED_SCHEMA_COMMENT)
    op.execute(_PREVIOUS_SPEC_COLUMN_COMMENT)
    op.execute("ALTER TABLE public.schedules ADD COLUMN auto_approve_gates boolean NOT NULL DEFAULT false")
    op.execute("UPDATE public.schedules SET auto_approve_gates = (gate_mode = 'auto')")
    op.execute(_PREVIOUS_GATE_MODE_COMMENT)
