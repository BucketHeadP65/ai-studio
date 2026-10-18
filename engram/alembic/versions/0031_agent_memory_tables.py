"""The agent-memory authority tables, mirroring `PostgresMemoryStore._ensure_schema` byte-for-byte.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-24 00:00:00.000000

`pydantic_ai_harness.memory` (`PostgresMemoryStore`, in the pydantic-ai-harness package) is
the authority for memory bytes, versions, and operation receipts -- this migration does not
duplicate that logic in Python. It only takes over the DDL the store would otherwise issue for
itself on first use (`_ensure_schema`, table default `agent_memory`), so a writer role without
CREATE privilege on `public` still works: `CREATE ... IF NOT EXISTS` against an object that
already exists needs no CREATE grant, only the grants DML already requires.

The store issues every name unqualified, so at runtime they resolve against the connecting role's
`search_path` (`public`, for the asyncpg writer DSN this app uses). These tables are created
explicitly as `public.<name>` so the chain-owned DDL and the store's runtime DDL name the same
relations. Shapes below are copied from `_ensure_schema` for the store's default table name
`agent_memory`:

- `public.agent_memory` -- one row per memory file path; `version` advances off the sequence
  below on every write, `last_operation_id` is the id of the write/delete that produced the
  current row.
- `public.agent_memory_operations` -- the operation-id receipt table the store's
  reserve/complete-operation calls use for idempotent replay.
- `public.agent_memory_versions` -- the sequence `version` columns draw from; not a table, but
  DDL the store issues from the same `_ensure_schema` call, so it is chain-owned for the same
  reason.
- `public.agent_memory_metadata` -- the single-row bootstrap marker `_ensure_schema` inserts with
  `ON CONFLICT (id) DO NOTHING`; seeded here so the store's own insert finds the row already
  present and skips the one-time version backfill it gates behind that insert's `RETURNING`.
"""

from __future__ import annotations

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the four `agent_memory*` objects and seed the metadata bootstrap row."""
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.agent_memory (
          path              TEXT PRIMARY KEY,
          content           TEXT NOT NULL,
          version           BIGINT NOT NULL DEFAULT 1,
          last_operation_id TEXT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.agent_memory_operations (
          id          TEXT PRIMARY KEY,
          fingerprint TEXT NOT NULL,
          version     TEXT,
          existed     BOOLEAN NOT NULL,
          completed   BOOLEAN NOT NULL
        )
        """
    )
    op.execute("CREATE SEQUENCE IF NOT EXISTS public.agent_memory_versions MINVALUE 0 START 0")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.agent_memory_metadata (
          id                  BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
          versions_initialized BOOLEAN NOT NULL
        )
        """
    )
    op.execute(
        "INSERT INTO public.agent_memory_metadata (id, versions_initialized) VALUES (TRUE, TRUE) "
        "ON CONFLICT (id) DO NOTHING"
    )

    op.execute(
        "COMMENT ON TABLE public.agent_memory IS "
        "'One row per agent-memory file path. Owned by PostgresMemoryStore; this table exists "
        "only so a writer role without CREATE can use it -- the store does all reads/writes.'"
    )
    op.execute("COMMENT ON COLUMN public.agent_memory.path IS 'The memory file path; primary key.'")
    op.execute("COMMENT ON COLUMN public.agent_memory.content IS 'The full file content.'")
    op.execute(
        "COMMENT ON COLUMN public.agent_memory.version IS "
        "'Monotonic version stamp, drawn from public.agent_memory_versions on every write.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.agent_memory.last_operation_id IS "
        "'Id of the write/delete operation that produced the current row, or null.'"
    )

    op.execute(
        "COMMENT ON TABLE public.agent_memory_operations IS "
        "'Operation-id receipts PostgresMemoryStore uses to replay a repeated write/delete "
        "idempotently instead of re-applying it.'"
    )
    op.execute("COMMENT ON COLUMN public.agent_memory_operations.id IS 'The caller-supplied operation id; primary key.'")
    op.execute(
        "COMMENT ON COLUMN public.agent_memory_operations.fingerprint IS "
        "'Hash of the operation''s arguments; a replayed id with a different fingerprint is a conflict.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.agent_memory_operations.version IS "
        "'The resulting agent_memory.version as text, or null for a delete.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.agent_memory_operations.existed IS "
        "'Whether the target path already existed when this operation ran.'"
    )
    op.execute(
        "COMMENT ON COLUMN public.agent_memory_operations.completed IS "
        "'False while the operation is reserved but not yet committed; true once its receipt is final.'"
    )

    op.execute(
        "COMMENT ON TABLE public.agent_memory_metadata IS "
        "'Single-row bootstrap marker PostgresMemoryStore seeds on first schema setup; not queried "
        "by application code.'"
    )
    op.execute("COMMENT ON COLUMN public.agent_memory_metadata.id IS 'Always TRUE; the CHECK enforces exactly one row.'")
    op.execute(
        "COMMENT ON COLUMN public.agent_memory_metadata.versions_initialized IS "
        "'Always TRUE once seeded; marks that the one-time version backfill has already run.'"
    )


def downgrade() -> None:
    """Drop the `agent_memory*` objects in dependency-safe order (operations/metadata before the sequence they don't reference, table last)."""
    op.execute("DROP TABLE IF EXISTS public.agent_memory_operations")
    op.execute("DROP TABLE IF EXISTS public.agent_memory_metadata")
    op.execute("DROP SEQUENCE IF EXISTS public.agent_memory_versions")
    op.execute("DROP TABLE IF EXISTS public.agent_memory")
