"""Every read of the session projection: sessions, turns, messages, flags, projects and where a project lives.

Each function runs on the port its caller holds and validates every row into a row type before a column is read.
"""

from __future__ import annotations

import uuid

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass as validated

from harness.features.sessions.read_models import FlagRow, MessageRow, ProjectRow, SessionRow, TurnRow

if TYPE_CHECKING:
    from harness.features.sessions.internal_contracts import ProjectionPool


@validated(frozen=True)
class RankedRow:
    """One session a full-text search matched, and how well it matched."""

    session_id: uuid.UUID
    rank: float


@validated(frozen=True)
class RootRow:
    """The one column a root-path lookup reads: where a project lives on this machine."""

    root_path: str


_SESSION = TypeAdapter(SessionRow)
_TURN = TypeAdapter(TurnRow)
_MESSAGE = TypeAdapter(MessageRow)
_FLAG = TypeAdapter(FlagRow)
_PROJECT = TypeAdapter(ProjectRow)
_RANKED = TypeAdapter(RankedRow)
_ROOT = TypeAdapter(RootRow)
"""One validator per projected shape: a row is checked here, once, and read by name after."""

_SESSIONS_SQL = (
    "SELECT id, project_id, title, started_at, last_activity_at, archived, turn_count, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost, unpriced, session_kind FROM sessions "
    "WHERE ($1::uuid IS NULL OR project_id = $1) AND (NOT archived OR $2) AND ($3 = 'all' OR session_kind = $3)"
)

_SESSION_SQL = (
    "SELECT id, project_id, title, started_at, last_activity_at, archived, turn_count, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost, unpriced, session_kind FROM sessions WHERE id = $1"
)

_TURNS_SQL = (
    "SELECT seq, run_id, entry, started_at, settled_at, outcome, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens FROM turns WHERE session_id = $1 ORDER BY seq"
)

_MESSAGES_SQL = (
    "SELECT m.run_id, m.idx, m.kind, m.payload, m.text_content FROM messages m "
    "JOIN turns t ON t.run_id = m.run_id WHERE t.session_id = $1 ORDER BY t.seq, m.idx"
)

_FLAGS_SQL = (
    "SELECT id, session_id, target_kind, target, note, tool_call_id, source, created_at FROM flags "
    "WHERE ($1::text IS NULL OR target_kind = $1) AND ($2::text IS NULL OR target = $2) "
    "AND ($3::uuid IS NULL OR session_id = $3) ORDER BY created_at DESC"
)

_PROJECT_SQL = "SELECT id, name, root_path FROM projects WHERE id = $1"

_SESSION_EXISTS_SQL = "SELECT 1 FROM sessions WHERE id = $1"

_PROJECT_ROOT_SQL = "SELECT root_path FROM projects WHERE id = $1"

_SESSION_ROOT_SQL = "SELECT p.root_path FROM sessions s JOIN projects p ON p.id = s.project_id WHERE s.id = $1"

_RANKED_SQL = """
SELECT t.session_id, max(ts_rank(to_tsvector('english', m.text_content), plainto_tsquery('english', $1))) AS rank
FROM messages m JOIN turns t ON t.run_id = m.run_id
WHERE to_tsvector('english', m.text_content) @@ plainto_tsquery('english', $1)
GROUP BY t.session_id ORDER BY rank DESC LIMIT 50
"""


async def sessions(pool: ProjectionPool, *, project_id: uuid.UUID | None, archived: bool, session_type: str) -> list[SessionRow]:
    """Read every session a listing covers.

    Args:
        pool: The projection's database port.
        project_id: The project to narrow to, or `None` for every project.
        archived: Whether archived sessions are included.
        session_type: The session type to narrow to, or `all` for every type.

    Returns:
        The sessions' rows, in no set order.
    """
    rows = await pool.fetch(_SESSIONS_SQL, project_id, archived, session_type)
    return [_SESSION.validate_python(dict(row)) for row in rows]


async def session(pool: ProjectionPool, session_id: uuid.UUID) -> SessionRow | None:
    """Read one session's row.

    Args:
        pool: The projection's database port.
        session_id: The session's id.

    Returns:
        The row, or `None` when nothing projected that session.
    """
    row = await pool.fetchrow(_SESSION_SQL, session_id)
    return None if row is None else _SESSION.validate_python(dict(row))


async def session_exists(pool: ProjectionPool, session_id: uuid.UUID) -> bool:
    """Read whether a session has a projected row at all.

    Args:
        pool: The projection's database port.
        session_id: The session's id.

    Returns:
        True when a session row has that id.
    """
    return await pool.fetchval(_SESSION_EXISTS_SQL, session_id) is not None


async def turns(pool: ProjectionPool, session_id: uuid.UUID) -> list[TurnRow]:
    """Read one session's turns.

    Args:
        pool: The projection's database port.
        session_id: The session's id.

    Returns:
        The rows, in the order the turns happened.
    """
    rows = await pool.fetch(_TURNS_SQL, session_id)
    return [_TURN.validate_python(dict(row)) for row in rows]


async def messages(pool: ProjectionPool, session_id: uuid.UUID) -> list[MessageRow]:
    """Read every message one session's turns produced.

    Args:
        pool: The projection's database port.
        session_id: The session's id.

    Returns:
        The rows, turn by turn, each run's messages in the order they were said.
    """
    rows = await pool.fetch(_MESSAGES_SQL, session_id)
    return [_MESSAGE.validate_python(dict(row)) for row in rows]


async def ranked_sessions(pool: ProjectionPool, query: str) -> list[uuid.UUID]:
    """Read the sessions a full-text search over their messages matched.

    Args:
        pool: The projection's database port.
        query: The search text.

    Returns:
        At most 50 session ids, best match first.
    """
    rows = await pool.fetch(_RANKED_SQL, query)
    return [_RANKED.validate_python(dict(row)).session_id for row in rows]


async def flags(pool: ProjectionPool, *, target_kind: str | None, target: str | None, session_id: uuid.UUID | None) -> list[FlagRow]:
    """Read every friction flag a listing covers.

    Args:
        pool: The projection's database port.
        target_kind: The friction target type to narrow to, or `None` for every type.
        target: The name of the flagged thing to narrow to, or `None` for every name.
        session_id: The subject session to narrow to, or `None` for every session.

    Returns:
        The rows, newest first.
    """
    rows = await pool.fetch(_FLAGS_SQL, target_kind, target, session_id)
    return [_FLAG.validate_python(dict(row)) for row in rows]


async def project(pool: ProjectionPool, project_id: uuid.UUID) -> ProjectRow | None:
    """Read one project's row.

    Args:
        pool: The projection's database port.
        project_id: The project's id.

    Returns:
        The row, or `None` when nothing projected that project.
    """
    row = await pool.fetchrow(_PROJECT_SQL, project_id)
    return None if row is None else _PROJECT.validate_python(dict(row))


async def project_root(pool: ProjectionPool, project_id: str) -> Path | None:
    """Read where a project lives.

    Args:
        pool: The projection's database port.
        project_id: The project's id, as text.

    Returns:
        The project's root, or `None` for a malformed id or one no project row has.
    """
    return await _root(pool, _PROJECT_ROOT_SQL, project_id)


async def session_root(pool: ProjectionPool, session_id: str) -> Path | None:
    """Read the root of the project a session was opened against.

    Args:
        pool: The projection's database port.
        session_id: The session's id, as text.

    Returns:
        The project's root, or `None` for a malformed id or one no session row has.
    """
    return await _root(pool, _SESSION_ROOT_SQL, session_id)


async def _root(pool: ProjectionPool, sql: str, value: str) -> Path | None:
    """One root-path lookup by id, `None` for a malformed id or a row that is not there."""
    try:
        key = uuid.UUID(value)
    except ValueError:
        return None
    row = await pool.fetchrow(sql, key)
    return None if row is None else Path(_ROOT.validate_python(dict(row)).root_path)
