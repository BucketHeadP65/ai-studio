"""A step store holding snapshots in memory, with a scriptable torn read per run id."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.history.contracts import (
    ContinuationAttempt,
    ContinuationCheckpointStore,
    ContinuationCommit,
    ContinuationConflictError,
    ContinuationInFlightError,
    ContinuationRef,
    ContinuationUnsafeReplayError,
)
from core.ai.history.domain import ContinuationCheckpoint

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from pydantic import JsonValue
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent, ToolEffectRecord

    from harness.domain.ports import SessionTranscript


class FakeStepStore:
    """A transcript stand-in: one snapshot per run id, scriptable failures by run id.

    `latest_snapshot` answers `snapshots[run_id]` when there is one, raises when `run_id` is in
    `broken`, and answers `None` for anything else -- a run whose snapshot is not there yet.
    `list_snapshots` is that same one snapshot as the run's only step, so a reader of the whole run
    meets the same scripted failure. Every other method answers the protocol's empty shape.
    """

    def __init__(
        self,
        *,
        snapshots: dict[str, ContinuableSnapshot] | None = None,
        broken: frozenset[str] = frozenset(),
        runs: Sequence[RunRecord] = (),
    ) -> None:
        self.snapshots = snapshots or {}
        self.broken = broken
        self.runs = list(runs)

    async def register_run(self, record: RunRecord) -> None:
        """Keep the record, the way a real store would."""
        self.runs.append(record)

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        """Nothing is looked up by id here."""
        del run_id
        return None

    async def list_runs(self, *, parent_run_id: str | None = None, conversation_id: str | None = None) -> list[RunRecord]:
        """Every registered run, narrowed to one conversation when a caller names it."""
        del parent_run_id
        return [run for run in self.runs if conversation_id is None or run.conversation_id == conversation_id]

    async def append_event(self, event: StepEvent) -> None:
        """Events are not part of what the fold reads."""
        del event

    async def list_events(self, *, run_id: str) -> list[StepEvent]:
        """Events are not part of what the fold reads."""
        del run_id
        return []

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        """Keep the snapshot under its own run id."""
        self.snapshots[snapshot.run_id] = snapshot

    async def latest_snapshot(self, *, run_id: str, include_interrupted: bool = False) -> ContinuableSnapshot | None:
        """The snapshot under `run_id`, a torn read when it is scripted broken, else `None`."""
        del include_interrupted
        if run_id in self.broken:
            msg = f"torn read for {run_id}"
            raise RuntimeError(msg)
        return self.snapshots.get(run_id)

    async def list_snapshots(self, *, run_id: str) -> list[ContinuableSnapshot]:
        """The run's one snapshot as its only step, a torn read when it is scripted broken, else nothing."""
        held = await self.latest_snapshot(run_id=run_id)
        return [] if held is None else [held]

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        """Tool effects are not part of what the fold reads."""
        del record

    async def get_tool_effect(self, *, run_id: str, tool_call_id: str) -> ToolEffectRecord | None:
        """Tool effects are not part of what the fold reads."""
        del run_id, tool_call_id
        return None

    async def list_unresolved_tool_effects(self, *, run_id: str) -> list[ToolEffectRecord]:
        """Tool effects are not part of what the fold reads."""
        del run_id
        return []


_proof: type[SessionTranscript] = FakeStepStore
"""Pyright fails this module the moment `FakeStepStore` drifts from the `SessionTranscript` port."""


class FakeContinuationCheckpoints:
    """A continuation ledger in memory: one head and one attempt table, scriptable by public attributes.

    `settled` is what a completed attempt hands back, keyed by attempt id. `heads` is where each
    conversation stands. `checkpoints` is what `read_checkpoint` answers with for a ref, and
    `held` is what `latest_checkpoint` answers with for a run id. `begun` records every claim in
    the order it was made, and `committed` every settlement.
    """

    def __init__(self) -> None:
        self.settled: dict[str, ContinuationCommit] = {}
        self.heads: dict[UUID, int] = {}
        self.checkpoints: dict[str, ContinuableSnapshot] = {}
        self.held: dict[str, ContinuationCheckpoint] = {}
        self.begun: list[ContinuationAttempt] = []
        self.committed: list[ContinuationCommit] = []
        self.in_flight: str | None = None
        """An attempt id this ledger refuses every new claim for, standing in for an unfinished turn."""
        self.unwritten: set[str] = set()
        """Run ids this ledger reports as having written no settled step at all."""

    async def begin_attempt(self, attempt: ContinuationAttempt) -> ContinuationCommit | None:
        """Record the claim, hand back a settled attempt's output, or refuse while another is unfinished."""
        self.begun.append(attempt)
        if self.in_flight is not None and self.in_flight != attempt.attempt_id:
            msg = f"attempt {self.in_flight!r} is still running"
            raise ContinuationInFlightError(msg)
        settled = self.settled.get(attempt.attempt_id)
        if settled is not None:
            return settled
        if self.heads.get(attempt.conversation_id, 0) != attempt.expected_revision:
            msg = f"conversation {attempt.conversation_id} is at revision {self.heads.get(attempt.conversation_id, 0)}"
            raise ContinuationConflictError(msg)
        return None

    async def read_checkpoint(self, ref: ContinuationRef) -> ContinuableSnapshot:
        """The snapshot scripted for this ref's run, or a refusal when nothing was scripted."""
        held = self.checkpoints.get(ref.run_id)
        if held is None:
            msg = f"nothing scripted at {ref.run_id!r} seq {ref.snapshot_seq}"
            raise ContinuationUnsafeReplayError(msg)
        return held

    async def commit_attempt(
        self, attempt: ContinuationAttempt, *, snapshot_seq: int, checkpoint_digest: str, output: JsonValue
    ) -> ContinuationCommit:
        """Settle the attempt, advance its conversation by one revision, and keep what it committed."""
        commit = ContinuationCommit(
            ref=ContinuationRef(
                conversation_id=attempt.conversation_id,
                revision=attempt.expected_revision + 1,
                run_id=attempt.run_id,
                snapshot_seq=snapshot_seq,
                checkpoint_digest=checkpoint_digest,
            ),
            output=output,
        )
        self.settled[attempt.attempt_id] = commit
        self.heads[attempt.conversation_id] = attempt.expected_revision + 1
        self.committed.append(commit)
        return commit

    async def latest_checkpoint(self, *, run_id: str) -> ContinuationCheckpoint | None:
        """What the run is scripted to have written: nothing for an `unwritten` run, else a settled step at seq 0."""
        if run_id in self.unwritten:
            return None
        return self.held.get(run_id, ContinuationCheckpoint(snapshot_seq=0, checkpoint_digest="0" * 64))


_checkpoints: type[ContinuationCheckpointStore] = FakeContinuationCheckpoints
"""Pyright fails this module the moment the fake drifts from the port the composition reads it through."""
