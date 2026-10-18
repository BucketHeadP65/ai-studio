"""The ports the record runs on: its connections and rows, the continuation checkpoints and the request-parameters sink."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from pydantic import JsonValue
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot

    from core.ai.history.domain import ContinuationAttempt, ContinuationCheckpoint, ContinuationCommit, ContinuationRef


class Row(Protocol):
    """One row of a result, whatever type the driver hands it back as."""

    def items(self) -> Iterable[tuple[str, object]]:
        """Every column of the row, by name.

        Returns:
            Each column's name and value.
        """
        ...


class Connection(Protocol):
    """A connection that runs statements and opens transactions."""

    async def execute(self, query: str, *args: object) -> str:
        """Run one statement, or a whole script, and answer with its status.

        Args:
            query: The statement or the script.
            *args: The values the statement's numbered placeholders take.

        Returns:
            The status the database answered with.
        """
        ...

    async def fetch(self, query: str, *args: object) -> Sequence[Row]:
        """Run one statement and read every row it answers with.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The rows, in the order the statement answered with them.
        """
        ...

    async def fetchrow(self, query: str, *args: object) -> Row | None:
        """Run one statement and read the first row it answers with.

        Args:
            query: The statement.
            *args: The values its numbered placeholders take.

        Returns:
            The first row, or None when the statement answers with none.
        """
        ...

    def transaction(self) -> AbstractAsyncContextManager[object]:
        """Open a transaction on this connection.

        Returns:
            A block that commits when it ends cleanly and rolls back when it raises.
        """
        ...


class Database(Protocol):
    """A source of connections: one per block, given back when the block ends."""

    def acquire(self) -> AbstractAsyncContextManager[Connection]:
        """Take a connection for the duration of one block.

        Returns:
            The block, which hands out the connection and gives it back when it ends.
        """
        ...


class ContinuationCheckpointStore(Protocol):
    """The three moves one durable turn makes against its conversation, plus the checkpoint a commit needs."""

    async def begin_attempt(self, attempt: ContinuationAttempt) -> ContinuationCommit | None:
        """Claim one turn against the revision it expects.

        An attempt with this very id that already settled is answered with its stored commit, and the
        turn runs no model call at all.

        Args:
            attempt: The turn's claim on its conversation.

        Returns:
            None when the attempt is admitted and may run, or the commit an attempt with this id already settled.

        Raises:
            ContinuationConflictError: the stored attempt under this id was begun for another run or
                another revision, or the conversation has moved past the revision this attempt expects.
            ContinuationUnsafeReplayError: this attempt began before and never settled, so whether
                its work happened is unknown.
            ContinuationInFlightError: a different attempt on this conversation is still unfinished.
        """
        ...

    async def read_checkpoint(self, ref: ContinuationRef) -> ContinuableSnapshot:
        """Read the messages one turn continues from: the one step the ref names, and no other.

        The step is addressed by run and sequence, never by step index and never as the newest step, so
        a run that wrote several steps hands back the one that was committed.

        Args:
            ref: Where the conversation stands.

        Returns:
            The step the ref names, verified against its conversation and the digest it travelled under.

        Raises:
            ContinuationUnsafeReplayError: the run is unknown, belongs to another conversation, wrote
                no such step, wrote it interrupted, holds messages the digest does not address, or
                left a tool call unresolved.
        """
        ...

    async def commit_attempt(
        self, attempt: ContinuationAttempt, *, snapshot_seq: int, checkpoint_digest: str, output: JsonValue
    ) -> ContinuationCommit:
        """Settle one turn against the exact step it wrote, and carry the conversation one revision forward.

        Committing the same attempt twice is the same answer twice: the stored output comes back
        and nothing is written. Committing a different output over a settled attempt is refused.

        Args:
            attempt: The claim `begin_attempt` admitted, unchanged.
            snapshot_seq: The sequence of the settled step this turn wrote, in `attempt.run_id`.
            checkpoint_digest: The digest of the messages that step holds.
            output: What the turn produced, as JSON.

        Returns:
            The revision the conversation reached and the output committed there.

        Raises:
            ContinuationPayloadTooLargeError: the output or the ref it would return is past its ceiling.
            ContinuationConflictError: nothing was begun under this id, the stored attempt is another
                turn's, the conversation moved under this commit, or a settled attempt holds another output.
            ContinuationUnsafeReplayError: the named step is not a settled step of this attempt's own
                run and conversation, does not hold what the digest addresses, or the run left a tool
                call unresolved.
        """
        ...

    async def latest_checkpoint(self, *, run_id: str) -> ContinuationCheckpoint | None:
        """Read the newest settled step of one run.

        Args:
            run_id: The run.

        Returns:
            The step's sequence and the digest of the messages it holds, or None when the run is unknown or wrote no settled step.
        """
        ...


class RequestParametersSink(Protocol):
    """Receives what one model request was sent with beside its messages."""

    async def record_request_parameters(self, *, run_id: str, step_index: int, parameters: Mapping[str, JsonValue]) -> None:
        """Keep the parameters of one model request.

        Args:
            run_id: The run the request belongs to.
            step_index: The request's step number in that run.
            parameters: What the request was sent with, as JSON.
        """
        ...
