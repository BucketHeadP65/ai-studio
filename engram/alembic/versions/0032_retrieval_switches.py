"""`memory.compile_fingerprint` gains `switches`; `api.retrieval_switches` is the one reader door onto it.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-24 00:00:00.000000

`switches` carries the three retrieval-capability flags (`embeddings`, `sql`, `graph`;
`engram.compile.switches.RetrievalSwitches`) the compiler decides for a collection, stamped
alongside the rest of the compile fingerprint. Defaulted to `'{}'` so every existing row reads as
"nothing decided yet" rather than failing the `NOT NULL` backfill.

`memory.compile_fingerprint` stays ungranted to `corpus_reader`, exactly like its compile-state
siblings (0006). `api.retrieval_switches(p_collection)` is the one deliberate hole: a
`SECURITY DEFINER` function reading just this column, returning an empty object rather than no
row when the collection has never compiled, so a caller never has to branch on absence.
"""

from __future__ import annotations

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None

_READER_ROLE = "corpus_reader"
_SIGNATURE = "api.retrieval_switches(text)"

_FUNCTION_BODY = """
CREATE FUNCTION api.retrieval_switches(p_collection text)
  RETURNS jsonb
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = memory, public, pg_catalog AS $$
    SELECT COALESCE(
      (SELECT f.switches FROM memory.compile_fingerprint f WHERE f.collection = p_collection),
      '{}'::jsonb
    )
$$
"""

_FUNCTION_COMMENT = (
    "COMMENT ON FUNCTION api.retrieval_switches(text) IS "
    "'The compiled retrieval switches (embeddings/sql/graph, each boolean) for one collection, or "
    "an empty object when that collection has never compiled. The one reader-granted door onto "
    "compile_fingerprint -- the table itself stays ungranted. Example: "
    "SELECT api.retrieval_switches(''research'');'"
)

_COLUMN_COMMENT = (
    "COMMENT ON COLUMN memory.compile_fingerprint.switches IS "
    "'Feature-flag-shaped jsonb the compiler stamps alongside the rest of the fingerprint row; "
    "served to readers only through api.retrieval_switches, never by a direct grant on this table.'"
)


def upgrade() -> None:
    """Add `compile_fingerprint.switches` and `api.retrieval_switches`, its one reader door."""
    op.execute("ALTER TABLE memory.compile_fingerprint ADD COLUMN switches jsonb NOT NULL DEFAULT '{}'")
    op.execute(_COLUMN_COMMENT)
    op.execute(_FUNCTION_BODY)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_SIGNATURE} TO {_READER_ROLE}")
    op.execute(_FUNCTION_COMMENT)


def downgrade() -> None:
    """Drop `api.retrieval_switches`, then the `switches` column."""
    op.execute(f"DROP FUNCTION {_SIGNATURE}")
    op.execute("ALTER TABLE memory.compile_fingerprint DROP COLUMN switches")
