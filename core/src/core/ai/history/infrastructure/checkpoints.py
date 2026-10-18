"""Where one durable turn stands in one conversation: the attempt it is, and the checkpoint it continues from.

A conversation is carried forward one revision at a time. Each turn claims an attempt against the
revision it believes is current, runs, and commits its output against the exact step the record
wrote. A turn that is delivered twice reads its own settled attempt back rather than running again,
and a turn whose predecessor never settled is refused rather than replayed.

Nothing here carries a transcript. The attempt row names a run and a step sequence. The messages
themselves stay in the record's own `messages` and `steps` tables, which remain the only authority
on what the model was given. What crosses back to a caller is a `ContinuationRef`, bounded to
`CONTINUATION_REF_BYTES`, and the committed output, bounded to `CONTINUATION_OUTPUT_BYTES`.

This does not promise exactly-once external effects. It returns a stored output after a crash that
followed a commit, and it refuses an automatic second run while an earlier attempt is unfinished or
its tool effects are unresolved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelMessagesTypeAdapter

from core.ai.artifacts import artifact_digest
from core.ai.history.domain import (
    CONTINUATION_REF_BYTES,
    ContinuationCheckpoint,
    ContinuationCommit,
    ContinuationRef,
    canonical_output,
    output_digest,
)
from core.ai.history.exceptions import (
    ContinuationConflictError,
    ContinuationInFlightError,
    ContinuationPayloadTooLargeError,
    ContinuationUnsafeReplayError,
)
from core.ai.history.infrastructure import reads
from core.ai.history.infrastructure.queries import (
    ADVANCE_CONTINUATION_HEAD,
    COMPLETE_CONTINUATION_ATTEMPT,
    DELETE_CONVERSATION_CONTINUATION_ATTEMPTS,
    DELETE_CONVERSATION_CONTINUATION_HEADS,
    LOCK_CONTINUATION,
    OPEN_CONTINUATION_HEAD,
    START_CONTINUATION_ATTEMPT,
)
from core.ai.history.infrastructure.rows import AttemptIdRow, ContinuationAttemptRow, RevisionRow, row_as

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot

    from core.ai.history.domain import ContinuationAttempt
    from core.ai.history.internal_contracts import Connection, ContinuationCheckpointStore, Database


def checkpoint_digest(messages: Sequence[ModelMessage]) -> str:
    """The content address of one checkpoint: every message it holds, in the order the model was given them.

    The messages are dumped in JSON mode and digested as `core.ai.artifacts` spells a value, so a nonfinite number in a
    message reads as null.

    Args:
        messages: The messages the checkpoint's step holds.

    Returns:
        The 64-character lowercase hex digest.
    """
    rendered = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
    return artifact_digest(rendered)


async def delete_conversation_checkpoints(connection: Connection, conversation_id: UUID) -> None:
    """Drop one conversation's head and every attempt under it. Run it before deleting the runs the attempts name.

    Args:
        connection: The connection the caller's transaction runs on.
        conversation_id: The conversation.
    """
    await connection.execute(DELETE_CONVERSATION_CONTINUATION_HEADS, conversation_id)
    await connection.execute(DELETE_CONVERSATION_CONTINUATION_ATTEMPTS, conversation_id)


class PostgresContinuationStore:
    """The continuation checkpoints over the record's own database.

    Begin and commit each run inside one short transaction that holds a cooperative lock on the
    conversation and nothing else. No lock spans the model call: begin commits and lets go before
    the turn runs, and commit takes the lock again afterwards.
    """

    def __init__(self, database: Database) -> None:
        """Build the store over a database.

        Args:
            database: The record's database, where each move takes its connection.
        """
        self.database = database

    async def begin_attempt(self, attempt: ContinuationAttempt) -> ContinuationCommit | None:
        """Claim this turn against the revision it expects, under the conversation's lock.

        Args:
            attempt: The turn's claim on its conversation.

        Returns:
            None when the attempt is admitted and may run, or the commit an attempt with this very id already settled.

        Raises:
            ContinuationConflictError: the stored attempt under this id was begun for another run or
                another revision, or the conversation has moved past the revision this attempt expects.
            ContinuationUnsafeReplayError: this attempt began before and never settled, so whether
                its work happened is unknown.
            ContinuationInFlightError: a different attempt on this conversation is still unfinished.
        """
        async with self.database.acquire() as connection, connection.transaction():
            await connection.execute(LOCK_CONTINUATION, str(attempt.conversation_id))
            stored = await reads.continuation_attempt(
                connection, conversation_id=attempt.conversation_id, attempt_id=attempt.attempt_id
            )
            if stored is not None:
                _same_attempt(stored, attempt)
                if stored.status == "completed":
                    return _settled(stored)
                msg = (
                    f"attempt {stored.attempt_id!r} of conversation {stored.conversation_id} began under run "
                    f"{stored.run_id!r} and never settled, so this turn is not replayed automatically"
                )
                raise ContinuationUnsafeReplayError(msg)
            unfinished = await reads.started_continuation_attempts(connection, attempt.conversation_id)
            if unfinished:
                other = unfinished[0]
                msg = (
                    f"attempt {other.attempt_id!r} of conversation {other.conversation_id} is still running under run "
                    f"{other.run_id!r}, so {attempt.attempt_id!r} may not start"
                )
                raise ContinuationInFlightError(msg)
            head = await reads.continuation_head(connection, attempt.conversation_id)
            revision = head.revision if head is not None else 0
            if revision != attempt.expected_revision:
                msg = (
                    f"conversation {attempt.conversation_id} is at revision {revision}, and attempt "
                    f"{attempt.attempt_id!r} expected {attempt.expected_revision}"
                )
                raise ContinuationConflictError(msg)
            await connection.execute(
                START_CONTINUATION_ATTEMPT,
                attempt.conversation_id,
                attempt.attempt_id,
                attempt.expected_revision,
                attempt.run_id,
                datetime.now(UTC),
            )
            return None

    async def read_checkpoint(self, ref: ContinuationRef) -> ContinuableSnapshot:
        """Read the one step the ref names, by run and sequence, on one connection.

        Args:
            ref: Where the conversation stands.

        Returns:
            The step the ref names.

        Raises:
            ContinuationUnsafeReplayError: the run is unknown, belongs to another conversation, wrote
                no such step, wrote it interrupted, holds messages the digest does not address, or
                left a tool call unresolved.
        """
        async with self.database.acquire() as connection:
            found = await reads.run(connection, ref.run_id)
            if found is None:
                msg = f"run {ref.run_id!r} is not registered, so the checkpoint at seq {ref.snapshot_seq} cannot be read"
                raise ContinuationUnsafeReplayError(msg)
            if found.conversation_id != ref.conversation_id:
                msg = (
                    f"run {ref.run_id!r} belongs to conversation {found.conversation_id}, "
                    f"and the checkpoint names {ref.conversation_id}"
                )
                raise ContinuationUnsafeReplayError(msg)
            step = await reads.step_at(connection, run_id=ref.run_id, seq=ref.snapshot_seq)
            if step is None:
                msg = f"run {ref.run_id!r} wrote no step at seq {ref.snapshot_seq}"
                raise ContinuationUnsafeReplayError(msg)
            if step.state != "complete":
                msg = f"the step at seq {ref.snapshot_seq} of run {ref.run_id!r} was saved interrupted, so it is not continued from"
                raise ContinuationUnsafeReplayError(msg)
            snapshot = await reads.snapshot_of(connection, found, step)
            held = checkpoint_digest(snapshot.messages)
            if held != ref.checkpoint_digest:
                msg = f"the step at seq {ref.snapshot_seq} of run {ref.run_id!r} holds {held}, and the checkpoint names {ref.checkpoint_digest}"
                raise ContinuationUnsafeReplayError(msg)
            await _no_unresolved_effect(connection, ref.run_id)
            return snapshot

    async def commit_attempt(
        self, attempt: ContinuationAttempt, *, snapshot_seq: int, checkpoint_digest: str, output: JsonValue
    ) -> ContinuationCommit:
        """Settle this turn against the exact step it wrote and carry the conversation one revision forward.

        The output is canonicalized before the database is touched. Committing the same attempt twice is the same answer
        twice: the stored output comes back and nothing is written. Committing a different output over a settled attempt is
        refused.

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
        document = canonical_output(output)
        digest = output_digest(output)
        async with self.database.acquire() as connection, connection.transaction():
            await connection.execute(LOCK_CONTINUATION, str(attempt.conversation_id))
            stored = await reads.continuation_attempt(
                connection, conversation_id=attempt.conversation_id, attempt_id=attempt.attempt_id
            )
            if stored is None:
                msg = f"attempt {attempt.attempt_id!r} of conversation {attempt.conversation_id} was never begun, so there is nothing to settle"
                raise ContinuationConflictError(msg)
            _same_attempt(stored, attempt)
            if stored.status == "completed":
                if stored.output_digest != digest:
                    msg = (
                        f"attempt {attempt.attempt_id!r} already settled on output digest {stored.output_digest}, "
                        f"and this commit carries {digest}"
                    )
                    raise ContinuationConflictError(msg)
                return _settled(stored)
            head = await reads.continuation_head(connection, attempt.conversation_id)
            revision = head.revision if head is not None else 0
            if revision != attempt.expected_revision:
                msg = (
                    f"conversation {attempt.conversation_id} is at revision {revision}, and attempt "
                    f"{attempt.attempt_id!r} committed against {attempt.expected_revision}"
                )
                raise ContinuationConflictError(msg)
            await self._verify_step(connection, attempt, snapshot_seq=snapshot_seq, digest=checkpoint_digest)
            settled = await connection.fetchrow(
                COMPLETE_CONTINUATION_ATTEMPT,
                attempt.conversation_id,
                attempt.attempt_id,
                snapshot_seq,
                checkpoint_digest,
                document,
                digest,
                datetime.now(UTC),
            )
            if settled is None:
                msg = f"attempt {attempt.attempt_id!r} of conversation {attempt.conversation_id} was not left started, so it could not be settled"
                raise ContinuationConflictError(msg)
            row_as(AttemptIdRow, settled)
            await self._advance(connection, attempt)
            ref = _bounded(
                ContinuationRef(
                    conversation_id=attempt.conversation_id,
                    revision=attempt.expected_revision + 1,
                    run_id=attempt.run_id,
                    snapshot_seq=snapshot_seq,
                    checkpoint_digest=checkpoint_digest,
                )
            )
            return ContinuationCommit(ref=ref, output=output)

    async def latest_checkpoint(self, *, run_id: str) -> ContinuationCheckpoint | None:
        """Read the newest settled step of one run and digest the messages it holds.

        Args:
            run_id: The run.

        Returns:
            The step's sequence and digest, or None when the run is unknown or wrote no settled step.
        """
        async with self.database.acquire() as connection:
            found = await reads.run(connection, run_id)
            if found is None:
                return None
            step = await reads.latest_complete_step(connection, run_id)
            if step is None:
                return None
            snapshot = await reads.snapshot_of(connection, found, step)
            return ContinuationCheckpoint(snapshot_seq=step.seq, checkpoint_digest=checkpoint_digest(snapshot.messages))

    async def _advance(self, connection: Connection, attempt: ContinuationAttempt) -> None:
        """Carry the head to one past the revision this attempt committed against, refusing anything but one affected row."""
        moved = (
            await connection.fetchrow(OPEN_CONTINUATION_HEAD, attempt.conversation_id, attempt.attempt_id)
            if attempt.expected_revision == 0
            else await connection.fetchrow(
                ADVANCE_CONTINUATION_HEAD,
                attempt.conversation_id,
                attempt.expected_revision,
                attempt.expected_revision + 1,
                attempt.attempt_id,
            )
        )
        if moved is None:
            msg = (
                f"the head of conversation {attempt.conversation_id} is no longer at revision "
                f"{attempt.expected_revision}, so attempt {attempt.attempt_id!r} did not advance it"
            )
            raise ContinuationConflictError(msg)
        row_as(RevisionRow, moved)

    async def _verify_step(
        self, connection: Connection, attempt: ContinuationAttempt, *, snapshot_seq: int, digest: str
    ) -> None:
        """Refuse unless the named step is a settled step of this attempt's own run and conversation, holding what `digest` addresses."""
        found = await reads.run(connection, attempt.run_id)
        if found is None:
            msg = f"run {attempt.run_id!r} is not registered, so attempt {attempt.attempt_id!r} has no step to commit against"
            raise ContinuationUnsafeReplayError(msg)
        if found.conversation_id != attempt.conversation_id:
            msg = f"run {attempt.run_id!r} belongs to conversation {found.conversation_id}, and the attempt names {attempt.conversation_id}"
            raise ContinuationUnsafeReplayError(msg)
        step = await reads.step_at(connection, run_id=attempt.run_id, seq=snapshot_seq)
        if step is None:
            msg = f"run {attempt.run_id!r} wrote no step at seq {snapshot_seq}"
            raise ContinuationUnsafeReplayError(msg)
        if step.state != "complete":
            msg = f"the step at seq {snapshot_seq} of run {attempt.run_id!r} was saved interrupted, so it is not committed"
            raise ContinuationUnsafeReplayError(msg)
        snapshot = await reads.snapshot_of(connection, found, step)
        held = checkpoint_digest(snapshot.messages)
        if held != digest:
            msg = f"the step at seq {snapshot_seq} of run {attempt.run_id!r} holds {held}, and this commit names {digest}"
            raise ContinuationUnsafeReplayError(msg)
        await _no_unresolved_effect(connection, attempt.run_id)


async def _no_unresolved_effect(connection: Connection, run_id: str) -> None:
    """Refuse, naming the calls, while any tool call of the run was still in flight when it last wrote."""
    unresolved = await reads.started_tool_effects(connection, run_id)
    if not unresolved:
        return
    calls = ", ".join(f"{effect.tool_name}/{effect.tool_call_id}" for effect in unresolved)
    msg = f"run {run_id!r} left {calls} in flight, so whether their effects happened is unknown and this turn is not replayed"
    raise ContinuationUnsafeReplayError(msg)


def _same_attempt(stored: ContinuationAttemptRow, attempt: ContinuationAttempt) -> None:
    """Refuse unless the stored row is this very attempt: the same run, against the same revision."""
    if stored.run_id != attempt.run_id:
        msg = f"attempt {attempt.attempt_id!r} is stored under run {stored.run_id!r}, and this call carries {attempt.run_id!r}"
        raise ContinuationConflictError(msg)
    if stored.expected_revision != attempt.expected_revision:
        msg = (
            f"attempt {attempt.attempt_id!r} is stored against revision {stored.expected_revision}, "
            f"and this call carries {attempt.expected_revision}"
        )
        raise ContinuationConflictError(msg)


def _settled(stored: ContinuationAttemptRow) -> ContinuationCommit:
    """One settled row as the commit a caller reads back, refusing a row settled without its step."""
    if stored.snapshot_seq is None or stored.checkpoint_digest is None:
        msg = f"attempt {stored.attempt_id!r} of conversation {stored.conversation_id} reads as settled without the step it settled on"
        raise ContinuationConflictError(msg)
    ref = _bounded(
        ContinuationRef(
            conversation_id=stored.conversation_id,
            revision=stored.expected_revision + 1,
            run_id=stored.run_id,
            snapshot_seq=stored.snapshot_seq,
            checkpoint_digest=stored.checkpoint_digest,
        )
    )
    return ContinuationCommit(ref=ref, output=stored.output)


def _bounded(ref: ContinuationRef) -> ContinuationRef:
    """The ref, refused with `ContinuationPayloadTooLargeError` past `CONTINUATION_REF_BYTES`."""
    size = len(ref.model_dump_json().encode())
    if size > CONTINUATION_REF_BYTES:
        msg = f"this continuation ref serializes to {size} bytes, past the {CONTINUATION_REF_BYTES} one may carry"
        raise ContinuationPayloadTooLargeError(msg)
    return ref


_proof: type[ContinuationCheckpointStore] = PostgresContinuationStore
"""Pyright fails this module the moment the store drifts from the port it is read through."""
