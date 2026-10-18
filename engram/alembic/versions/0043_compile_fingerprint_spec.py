"""`memory.compile_fingerprint` gains `spec`; `api.compiled_spec` is the one reader door onto it.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-02 00:00:00.000000

A deployment restored from a database dump carries no checkout, so it cannot read a corpus's
`corpusspec.json` off disk. `spec` is the compiler's own answer: every compile stamps the validated
`CorpusSpec` it ran with (`model_dump(mode="json")`) alongside the rest of the fingerprint row,
nullable until a collection's first compile after this column exists.

`memory.compile_fingerprint` stays ungranted to `corpus_reader`, exactly like its `switches`
sibling (0032). `api.compiled_spec(p_collection)` is the one reader door onto this column, guarded
like every other `api` object since 0037 (`api.visible_collection`).
"""

from __future__ import annotations

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_SIGNATURE = "api.compiled_spec(text)"

_FUNCTION_BODY = """
CREATE FUNCTION api.compiled_spec(p_collection text)
  RETURNS jsonb
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT f.spec FROM memory.compile_fingerprint f
    WHERE f.collection = p_collection AND api.visible_collection(p_collection)
$$
"""

_FUNCTION_COMMENT = (
    "COMMENT ON FUNCTION api.compiled_spec(text) IS "
    "'The validated CorpusSpec (as jsonb) the last compile ran with for one collection, or SQL "
    "NULL when that collection has never compiled, compiled before this column existed, or is not "
    "visible to this session. The one reader-granted door onto compile_fingerprint -- the table "
    "itself stays ungranted. A restored database with no checkout reads its own corpus spec "
    "through here instead of corpusspec.json. Example: SELECT api.compiled_spec(<collection>);'"
)

_COLUMN_COMMENT = (
    "COMMENT ON COLUMN memory.compile_fingerprint.spec IS "
    "'The validated CorpusSpec (as jsonb) the compiler ran with, stamped alongside the rest of the "
    "fingerprint row on every compile; served to readers only through api.compiled_spec, never by "
    "a direct grant on this table.'"
)


def upgrade() -> None:
    """Add `compile_fingerprint.spec` and `api.compiled_spec`, its one reader door."""
    op.execute("ALTER TABLE memory.compile_fingerprint ADD COLUMN spec jsonb")
    op.execute(_COLUMN_COMMENT)
    op.execute(_FUNCTION_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_FUNCTION_COMMENT)


def downgrade() -> None:
    """Drop `api.compiled_spec`, then the `spec` column."""
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    op.execute("ALTER TABLE memory.compile_fingerprint DROP COLUMN spec")
