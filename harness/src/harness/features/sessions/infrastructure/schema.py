"""The session projection's five tables and their indexes, created by idempotent statements.

No migration chain owns these tables. `ensure_schema` creates what is missing and touches nothing that is already there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.features.sessions.internal_contracts import ProjectionPool

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS projects (
        id uuid PRIMARY KEY,
        root_path text NOT NULL UNIQUE,
        name text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id uuid PRIMARY KEY,
        project_id uuid NOT NULL REFERENCES projects (id),
        title text NOT NULL,
        started_at timestamptz NOT NULL,
        last_activity_at timestamptz NOT NULL,
        archived boolean NOT NULL DEFAULT false,
        turn_count integer NOT NULL DEFAULT 0,
        input_tokens bigint NOT NULL DEFAULT 0,
        output_tokens bigint NOT NULL DEFAULT 0,
        cache_read_tokens bigint NOT NULL DEFAULT 0,
        cache_write_tokens bigint NOT NULL DEFAULT 0,
        cost numeric NOT NULL DEFAULT 0,
        unpriced bigint NOT NULL DEFAULT 0,
        session_kind text NOT NULL DEFAULT 'chat'
    )
    """,
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_kind text NOT NULL DEFAULT 'chat'",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cost numeric NOT NULL DEFAULT 0",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS unpriced bigint NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS sessions_listing ON sessions (project_id, archived, last_activity_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS turns (
        session_id uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
        seq integer NOT NULL,
        run_id text,
        entry text NOT NULL,
        started_at timestamptz NOT NULL,
        settled_at timestamptz,
        outcome text NOT NULL,
        input_tokens bigint NOT NULL DEFAULT 0,
        output_tokens bigint NOT NULL DEFAULT 0,
        cache_read_tokens bigint NOT NULL DEFAULT 0,
        cache_write_tokens bigint NOT NULL DEFAULT 0,
        PRIMARY KEY (session_id, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS turns_by_run ON turns (run_id)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        run_id text NOT NULL,
        idx integer NOT NULL,
        kind text NOT NULL,
        payload jsonb NOT NULL,
        text_content text NOT NULL DEFAULT '',
        PRIMARY KEY (run_id, idx)
    )
    """,
    "CREATE INDEX IF NOT EXISTS messages_search ON messages USING gin (to_tsvector('english', text_content))",
    """
    CREATE TABLE IF NOT EXISTS flags (
        id uuid PRIMARY KEY,
        session_id uuid NOT NULL,
        log_seq integer NOT NULL UNIQUE,
        target_kind text NOT NULL,
        target text NOT NULL,
        note text NOT NULL,
        tool_call_id text,
        source text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS flags_by_target ON flags (target_kind, target)",
    "CREATE INDEX IF NOT EXISTS flags_by_session ON flags (session_id)",
)
"""Every table, column and index the projection needs, in dependency order. Each statement creates only what is
missing, so running the tuple again against a current database changes nothing."""


async def ensure_schema(pool: ProjectionPool) -> None:
    """Create every table, column and index the projection needs that is not already there.

    Args:
        pool: The projection's database port.
    """
    for statement in SCHEMA:
        await pool.execute(statement)
