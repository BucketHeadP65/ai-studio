"""The ports of the sessions slice: the sessions a process holds open, the record's own deletion, and the projection's database."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.ai.history.contracts import Connection

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from pydantic_ai_harness.compaction import ContextUsage

    from harness.domain.contracts import SessionType
    from harness.session import Runtime, TurnHold


@runtime_checkable
class Sessions(Protocol):
    """Every session this process holds open, over the one agent it built."""

    @property
    def default(self) -> Runtime:
        """The process's own session."""
        ...

    async def get(self, session_id: str) -> Runtime:
        """The runtime of one session, hydrated from its stored record when it is not in memory.

        Args:
            session_id: The session's id.

        Returns:
            The session's runtime.

        Raises:
            KeyError: when no session on that id's own timeline exists, which is a caller's 404.
        """
        ...

    async def create(self, project_root: Path | None, *, session_kind: SessionType = "chat") -> Runtime:
        """Open a fresh session on a project's state home, or on the harness checkout's.

        Args:
            project_root: The project's folder, or `None` for the harness checkout.
            session_kind: The session type the session is opened as, written to its `SessionStarted` log entry.

        Returns:
            The new session's runtime.
        """
        ...

    async def begin_turn(self, session_id: str) -> TurnHold:
        """Hold one session for the turn about to run, hydrating it when it is not in memory.

        Args:
            session_id: The session's id.

        Returns:
            The hold, which the caller releases when the turn ends.

        Raises:
            KeyError: when no session on that id's own timeline exists, which is a caller's 404.
            SessionBusyError: when a turn is already in flight there, which is a caller's 409.
        """
        ...

    async def begin_default_turn(self) -> TurnHold:
        """Hold the process's own session for the turn about to run.

        Returns:
            The hold, which the caller releases when the turn ends.

        Raises:
            SessionBusyError: when a turn is already in flight in it.
        """
        ...

    def interrupt(self, session_id: str) -> bool:
        """Cancel the turn in flight in one session.

        Args:
            session_id: The session's id.

        Returns:
            Whether there was a turn to cancel.
        """
        ...

    def steering(self, session_id: str) -> Runtime | None:
        """The runtime whose turn is in flight in one session.

        Args:
            session_id: The session's id.

        Returns:
            The runtime, or `None` when no turn is in flight there.
        """
        ...

    def held_sessions(self) -> tuple[Runtime, ...]:
        """Every session this process is holding open.

        Returns:
            The runtimes, the process's own first.
        """
        ...

    def project_id_of(self, runtime: Runtime) -> str:
        """Which project's rows a session belongs to.

        Args:
            runtime: The session's runtime.

        Returns:
            The project id, or `""` when no project index can name one.
        """
        ...

    async def root_of_project(self, project_id: str) -> Path | None:
        """Where a project lives.

        Args:
            project_id: The project's id.

        Returns:
            The project's root, or `None` when no project index can name one.
        """
        ...

    def is_answering(self, session_id: str) -> bool:
        """Whether a turn is in flight in one session.

        Args:
            session_id: The session's id.

        Returns:
            True while a turn holds the session. False for a session this process is not holding.
        """
        ...

    def context_of(self, session_id: str) -> ContextUsage | None:
        """How full one session's context stands right now.

        Args:
            session_id: The session's id.

        Returns:
            The context reading, or `None` for a session this process is not holding.
        """
        ...

    def drop(self, session_id: str) -> bool:
        """Stop holding one session's runtime.

        Args:
            session_id: The session's id.

        Returns:
            Whether a runtime was let go.
        """
        ...

    def evict_idle(self, *, older_than_minutes: int = 30) -> None:
        """Drop every held runtime nobody has touched for a while.

        Args:
            older_than_minutes: How long a runtime must sit untouched before it is dropped.
        """
        ...


@runtime_checkable
class ConversationStore(Protocol):
    """A store that can delete one conversation from the record.

    A caller checks a store with `isinstance` before it deletes through it.
    """

    async def delete_conversation(self, conversation_id: str) -> None:
        """Remove one conversation's runs and messages, a cascade from its id.

        Args:
            conversation_id: The conversation's id, which is its session's id.
        """
        ...


@runtime_checkable
class ProjectionPool(Connection, Protocol):
    """The database connection the session projection reads and writes through.

    A row is a mapping of column names to values. Every read validates a row into a row type before a column is read.
    """

    async def fetch(self, query: str, *args: object) -> list[Mapping[str, object]]:
        """Run one statement and read every row it answers with.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The rows, in the order the statement answered with them.
        """
        ...

    async def fetchrow(self, query: str, *args: object) -> Mapping[str, object] | None:
        """Run one statement and read the first row it answers with.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The first row, or `None` when the statement answers with none.
        """
        ...

    async def fetchval(self, query: str, *args: object) -> object:
        """Run one statement and read the first column of its first row.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The value, or `None` when the statement answers with no row.
        """
        ...

    async def executemany(self, query: str, args: Sequence[Sequence[object]]) -> object:
        """Run one statement once per row of values.

        Args:
            query: The statement.
            args: One sequence of placeholder values per run.

        Returns:
            What the database answered with.
        """
        ...

    def transaction(self) -> AbstractAsyncContextManager[ProjectionPool]:
        """Open one unit of work.

        Returns:
            A block that yields a connection. Every statement run through it commits or rolls back together.
        """
        ...
