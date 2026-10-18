"""Every write to the session projection. Each public function is one transaction, named for the write it does.

A row argument holds values ready to bind, in the column order of the table it is written to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass as validated

if TYPE_CHECKING:
    from collections.abc import Sequence

    from harness.features.sessions.internal_contracts import ProjectionPool

type _Rows = list[tuple[object, ...]]


@validated(frozen=True)
class RunIdRow:
    """The one column a run sweep reads: which run a session's turns were written under."""

    run_id: str


_RUN_ID = TypeAdapter(RunIdRow)
"""The validator of a run sweep's rows."""

_SESSION_RUN_IDS_SQL = "SELECT DISTINCT run_id FROM turns WHERE session_id = $1 AND run_id IS NOT NULL"


async def replace_all_rows(pool: ProjectionPool, project: tuple[str, str, str], rows: tuple[_Rows, _Rows, _Rows, _Rows]) -> None:
    """Empty all five tables, then write one project's rows, in one transaction.

    Args:
        pool: The projection's database port.
        project: The project's id, resolved root path and display name.
        rows: The session, turn, message and flag rows to write.
    """
    project_id, root_path, name = project
    sessions, turns, messages, flags = rows
    async with pool.transaction() as connection:
        await connection.execute("TRUNCATE projects, sessions, turns, messages, flags")
        await _write_project(connection, project_id=project_id, root_path=root_path, name=name)
        await _write_sessions(connection, sessions)
        await _write_turns(connection, turns)
        await _write_messages(connection, messages)
        await _write_flags(connection, flags)


async def write_session_rows(
    pool: ProjectionPool, session_id: str, project: tuple[str, str, str], rows: tuple[_Rows, _Rows, _Rows, _Rows]
) -> None:
    """Write one session's rows in one transaction, replacing its turns and their messages outright.

    Its project row is inserted when missing. Its session row and its flags are upserted.

    Args:
        pool: The projection's database port.
        session_id: The session whose turns and messages are replaced.
        project: The project's id, resolved root path and display name.
        rows: The session, turn, message and flag rows to write.
    """
    project_id, root_path, name = project
    sessions, turns, messages, flags = rows
    async with pool.transaction() as connection:
        await _write_project(connection, project_id=project_id, root_path=root_path, name=name)
        await _write_sessions(connection, sessions)
        await _clear_session_turns(connection, session_id)
        await _write_turns(connection, turns)
        await _write_messages(connection, messages)
        await _write_flags(connection, flags)


async def reconcile_projects(pool: ProjectionPool, projects: Sequence[tuple[str, str, str]]) -> None:
    """Upsert one row per project, its root and its display name, in one transaction.

    Never removes a row. A project that is no longer registered keeps its row, and so does every session that ran under it.

    Args:
        pool: The projection's database port.
        projects: Each project's id, root path and display name.
    """
    if not projects:
        return
    async with pool.transaction() as connection:
        await connection.executemany(
            """
            INSERT INTO projects (id, root_path, name) VALUES ($1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET root_path = excluded.root_path, name = excluded.name
            """,
            projects,
        )


async def delete_session_rows(pool: ProjectionPool, session_id: str) -> list[str]:
    """Remove one session's rows, its turns, its messages and its flags, in one transaction.

    Args:
        pool: The projection's database port.
        session_id: The session's id.

    Returns:
        The run ids the session's turns were written under, for the caller to remove elsewhere.
    """
    async with pool.transaction() as connection:
        run_id_rows = await connection.fetch(_SESSION_RUN_IDS_SQL, session_id)
        run_ids = [_RUN_ID.validate_python(dict(row)).run_id for row in run_id_rows]
        await connection.execute("DELETE FROM sessions WHERE id = $1", session_id)
        if run_ids:
            await connection.execute("DELETE FROM messages WHERE run_id = ANY($1::text[])", run_ids)
        await connection.execute("DELETE FROM flags WHERE session_id = $1", session_id)
    return run_ids


async def _write_project(connection: ProjectionPool, *, project_id: str, root_path: str, name: str) -> None:
    await connection.execute(
        "INSERT INTO projects (id, root_path, name) VALUES ($1, $2, $3) ON CONFLICT (id) DO NOTHING", project_id, root_path, name
    )


async def _write_sessions(connection: ProjectionPool, rows: _Rows) -> None:
    if not rows:
        return
    await connection.executemany(
        """
        INSERT INTO sessions (id, project_id, title, started_at, last_activity_at, archived, turn_count,
                               input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost, unpriced, session_kind)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (id) DO UPDATE SET
            title = excluded.title,
            started_at = excluded.started_at,
            last_activity_at = excluded.last_activity_at,
            archived = excluded.archived,
            turn_count = excluded.turn_count,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            cache_read_tokens = excluded.cache_read_tokens,
            cache_write_tokens = excluded.cache_write_tokens,
            cost = excluded.cost,
            unpriced = excluded.unpriced,
            session_kind = excluded.session_kind
        """,
        rows,
    )


async def _write_turns(connection: ProjectionPool, rows: _Rows) -> None:
    if not rows:
        return
    await connection.executemany(
        """
        INSERT INTO turns (session_id, seq, run_id, entry, started_at, settled_at, outcome,
                            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (session_id, seq) DO UPDATE SET
            run_id = excluded.run_id,
            entry = excluded.entry,
            started_at = excluded.started_at,
            settled_at = excluded.settled_at,
            outcome = excluded.outcome,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            cache_read_tokens = excluded.cache_read_tokens,
            cache_write_tokens = excluded.cache_write_tokens
        """,
        rows,
    )


async def _write_messages(connection: ProjectionPool, rows: _Rows) -> None:
    if not rows:
        return
    await connection.executemany(
        """
        INSERT INTO messages (run_id, idx, kind, payload, text_content)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        ON CONFLICT (run_id, idx) DO UPDATE SET
            kind = excluded.kind,
            payload = excluded.payload,
            text_content = excluded.text_content
        """,
        rows,
    )


async def _write_flags(connection: ProjectionPool, rows: _Rows) -> None:
    if not rows:
        return
    await connection.executemany(
        """
        INSERT INTO flags (id, session_id, log_seq, target_kind, target, note, tool_call_id,
                            source, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (id) DO UPDATE SET
            session_id = excluded.session_id,
            target_kind = excluded.target_kind,
            target = excluded.target,
            note = excluded.note,
            tool_call_id = excluded.tool_call_id,
            source = excluded.source,
            created_at = excluded.created_at
        """,
        rows,
    )


async def _clear_session_turns(connection: ProjectionPool, session_id: str) -> None:
    """Remove a session's turn rows and their messages, so a rewrite leaves no forgotten turn behind."""
    run_id_rows = await connection.fetch(_SESSION_RUN_IDS_SQL, session_id)
    run_ids = [_RUN_ID.validate_python(dict(row)).run_id for row in run_id_rows]
    if run_ids:
        await connection.execute("DELETE FROM messages WHERE run_id = ANY($1::text[])", run_ids)
    await connection.execute("DELETE FROM turns WHERE session_id = $1", session_id)
