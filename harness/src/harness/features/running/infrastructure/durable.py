"""Launches a run on Temporal, mirrors it into the session, and carries a step conversation across turns.

This path keeps no run log. The run lives on the server, and its progress and evidence are read
out of the workflow with a query.

On this engine each turn of a multi-turn map is a separate activity, possibly on another worker,
so its step conversation is carried in the record. `DurableTurns` claims a turn, hands it the
exact checkpoint its predecessor committed, and settles it against the step the turn wrote.
"""

from __future__ import annotations

import asyncio

from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from core.ai.history.contracts import ContinuationCheckpointStore, ContinuationCommit, ContinuationUnsafeReplayError
from core.ai.workflows.contracts import (
    DurableWorkflowRun,
    GateVerdict,
    Outcome,
    RunEvidence,
    RunFinished,
    RunLaunched,
    RunRequest,
    StepConversationContinuation,
    connect,
    spec_identity,
)
from core.observability import get_logger, span
from pydantic_ai_harness.step_persistence import StepPersistence, StepStore

from harness.domain import contracts as session_events
from harness.features.running.approval import with_reviewers_note
from harness.features.running.domain.progress import PendingReview
from harness.features.running.infrastructure import run_events
from harness.features.running.records import RunCollaborators
from harness.features.running.spend import RunSpendHeader
from harness.rendering import render_for_review

if TYPE_CHECKING:
    from core.ai.workflows.contracts import Workflow, WorkflowArguments
    from core.cqrs.contracts import TopicSink
    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from temporalio.client import Client, WorkflowHandle

    from harness.features.running.records import RunRecords
    from harness.session import Runtime
    from harness.settings import RunSettings

_logger = get_logger(__name__)

_NOT_CHECKED = "This run was executed on the durable engine, which does not report a checked outcome."
"""Why a durable run's outcome is unknown: nothing reads its item dispositions back off the server."""

_POLL_SECONDS = 0.25
"""How often the session's view of a durable run is refreshed from the workflow, in seconds."""


@dataclass(frozen=True)
class DurableRunOutcome:
    """What a durable run was asked to do and how it ended: the record's facts beyond its own handle."""

    spec: Workflow
    """The workflow that ran, whose version the record files under."""
    params: WorkflowArguments
    """The arguments it ran with, as the record keeps them."""
    outcome: Literal["completed", "failed"]
    """How it ended, as the caller observed the result."""


@dataclass(frozen=True)
class ResumedTurn:
    """What one durable turn was handed: its place in the step conversation, and what it continues from.

    `settled` is the output an identical attempt already committed. When it is there the turn runs
    no model call at all and answers with exactly that payload. Otherwise `messages` is the history
    the checkpoint holds, empty on a step conversation's first turn.
    """

    continuation: StepConversationContinuation
    settled: ContinuationCommit | None
    messages: list[ModelMessage]


@dataclass(frozen=True)
class DurableTurns:
    """Where a durable step conversation lives: the checkpoints that say who may run, and the record its messages land in.

    Both sit on the record's own database. Nothing here holds process state, so two workers reach
    the same step conversation through two instances of this class and agree.
    """

    checkpoints: ContinuationCheckpointStore
    """Which turn may run next, and the checkpoint each settled turn committed."""
    record: StepStore
    """Where each turn's messages are written."""

    async def begin(self, continuation: StepConversationContinuation) -> ResumedTurn:
        """Claim this turn and hand it what it continues from.

        Args:
            continuation: The turn's place in its step conversation. Its `committed` is set when an
                identical attempt already settled.

        Returns:
            The output an identical attempt already committed, or otherwise the messages of the
            checkpoint this turn continues from.

        Raises:
            ContinuationInFlightError: another turn of this step conversation is unfinished.
            ContinuationConflictError: the step conversation has moved past the revision this turn was scheduled against.
            ContinuationUnsafeReplayError: this very turn began before and never settled, or the
                checkpoint it would resume from is not a settled one.
        """
        settled = await self.checkpoints.begin_attempt(continuation.attempt)
        if settled is not None:
            continuation.committed = settled.ref
            return ResumedTurn(continuation=continuation, settled=settled, messages=[])
        resumed = [] if continuation.given is None else list((await self.checkpoints.read_checkpoint(continuation.given)).messages)
        return ResumedTurn(continuation=continuation, settled=None, messages=resumed)

    def persistence(self, continuation: StepConversationContinuation) -> StepPersistence[None]:
        """The single-shot run this turn's messages are written under, named by the attempt itself.

        Args:
            continuation: The turn's place in its step conversation.

        Returns:
            The persistence over the record, under the attempt's own run id.
        """
        return StepPersistence[None](store=self.record, run_id=continuation.attempt.run_id)

    async def settle(self, resumed: ResumedTurn, output: JsonValue) -> ContinuationCommit:
        """Commit this turn against the exact step it just wrote, and carry the step conversation one revision forward.

        Args:
            resumed: What `begin` handed this turn. Its continuation's `committed` is set to the new commit.
            output: The turn's output, committed as its payload.

        Returns:
            The commit.

        Raises:
            ContinuationUnsafeReplayError: the run wrote no settled step, or left a tool call in flight.
            ContinuationConflictError: the step conversation moved under this commit.
            ContinuationPayloadTooLargeError: the output or the ref is past its ceiling.
        """
        held = await self.checkpoints.latest_checkpoint(run_id=resumed.continuation.attempt.run_id)
        if held is None:
            msg = f"run {resumed.continuation.attempt.run_id!r} wrote no settled step, so this turn has nothing to commit against"
            raise ContinuationUnsafeReplayError(msg)
        commit = await self.checkpoints.commit_attempt(
            resumed.continuation.attempt, snapshot_seq=held.snapshot_seq, checkpoint_digest=held.checkpoint_digest, output=output
        )
        resumed.continuation.committed = commit.ref
        return commit


def durable_run_id(handle: WorkflowHandle[DurableWorkflowRun, object], workflow_id: str) -> str:
    """The id a durable run's spend log file is opened under, and the one its own activities file rows against.

    It is the first execution's id. `activity_run` reads the same id only while a run has exactly
    one execution, which holds while nothing continues a workflow as new and nothing starts one with
    a retry policy. Both sides must change together: a row filed under an id no header opened is
    dropped, and the run then reads as cheaper than it was.

    Args:
        handle: The handle the start returned.
        workflow_id: The workflow's id, used when the handle names no run.

    Returns:
        The run id.
    """
    return handle.first_execution_run_id or handle.result_run_id or workflow_id


async def _client(run: RunSettings) -> Client:
    """A new client for the Temporal server `run` names, one per call."""
    return await connect(run.temporal_address, namespace=run.temporal_namespace)


async def launch(  # noqa: PLR0913 (the same flat contract `service.start` forwards verbatim)
    session: Runtime,
    *,
    records: RunRecords,
    topics: TopicSink,
    run: RunSettings,
    spec: Workflow,
    workflow_id: str,
    params: WorkflowArguments,
    auto_approve_gates: bool = False,
    runner: str = "app:harness+temporal",
    run_title: str = "",
) -> None:
    """Start the run on Temporal and follow it in the background.

    The Temporal workflow id is the workflow's id, so the caller must refuse a second concurrent
    run of one workflow. The run id is the server's id for the execution the start returned, the
    same id this run's activities read from their own context.

    Args:
        session: The session the run is followed into.
        records: Where this run's own records land.
        topics: Where the run says it moved.
        run: Which Temporal server to reach, its namespace and its task queue.
        spec: The workflow to run.
        workflow_id: The workflow's id, also the Temporal workflow id.
        params: The arguments the run starts with.
        auto_approve_gates: Whether every gate passes with nobody asked. It travels in the request,
            so it is a fact of the run's own history rather than of this process.
        runner: The launcher the spend log names, so a schedule's own runs are attributable to it.
        run_title: What this run is called in front of a person. It travels in the request, so the
            server's own history carries it.
    """
    client = await _client(run)
    handle = await client.start_workflow(
        DurableWorkflowRun.run,
        RunRequest(
            spec=spec, params=dict(params), workflow_id=workflow_id, run_title=run_title, auto_approve_gates=auto_approve_gates
        ),
        id=workflow_id,
        task_queue=run.temporal_task_queue,
    )
    run_id = durable_run_id(handle, workflow_id)
    session.active.run_id = run_id
    session.session_log.record(session_events.RunLaunched(workflow_id=workflow_id, run_id=run_id))
    run_events.record(session.state, RunLaunched(run_id=run_id, workflow_id=workflow_id))
    records.spend.begin(
        RunSpendHeader(
            run_id=run_id,
            workflow_id=workflow_id,
            runner=runner,
            session_id=session.session_id or "",
            started_at=datetime.now(UTC).isoformat(),
        )
    )
    session.active.workflow_id = workflow_id
    session.active.run_title = run_title
    session.active.result = ""
    session.active.paused_by_restart = False
    session.active.progress.reset()
    session.active.pending = None
    session.active.decision = None
    session.active.gate_mode = "auto" if auto_approve_gates else "wait"
    session.active.task = asyncio.create_task(
        _follow(session, handle, run=RunCollaborators(records=records, topics=topics, run_settings=run), spec=spec, params=params)
    )


async def decide(session: Runtime, run: RunSettings, *, approve: bool, feedback: str = "") -> bool:
    """Answer the waiting gate with a signal.

    The gate is addressed by workflow id, so the answer still reaches a run that kept waiting while
    this app restarted.

    Args:
        session: The session whose run is waiting.
        run: Which Temporal server to reach.
        approve: True releases the value, False refuses it.
        feedback: The reviewer's own words, sent with a refusal to the rerun that owes the repair.
            An approval releases the value whatever was typed beside it.

    Returns:
        True when a gate was waiting to be answered.
    """
    pending = session.active.pending
    if pending is None:
        return False
    client = await _client(run)
    handle = client.get_workflow_handle(session.active.workflow_id)
    await handle.signal(DurableWorkflowRun.decide, GateVerdict(gate_id=pending.gate_id, approved=approve, feedback=feedback))
    return True


async def _follow(
    session: Runtime,
    handle: WorkflowHandle[DurableWorkflowRun, object],
    *,
    run: RunCollaborators,
    spec: Workflow,
    params: WorkflowArguments,
) -> None:
    """Mirror the run into the session until it ends, then record what it did."""
    watching = asyncio.create_task(_mirror(session, handle, run.topics))
    outcome: Literal["completed", "failed"] = "failed"
    try:
        with span("durable run {workflow}", workflow=session.active.workflow_id):
            produced = await handle.result()
        outcome = "completed"
        session.active.result = f"Finished. Result ({type(produced).__name__}):\n{render_for_review(produced)}"
    except Exception as exc:  # a failed run is news, not a silent stop
        _logger.exception("durable run of {workflow} failed", workflow=session.active.workflow_id)
        session.active.result = f"The run stopped with an error: {type(exc).__name__}: {exc}"
    finally:
        watching.cancel()
        await _drain(watching)
        await _mirror_once(session, handle, run.topics)
        session.active.pending = None
        run.topics.publish(f"run.{session.active.workflow_id}")
        await _record_evidence(session, handle, records=run.records, ran=DurableRunOutcome(spec=spec, params=params, outcome=outcome))
        finished = RunFinished(run_id=durable_run_id(handle, session.active.workflow_id), workflow_id=session.active.workflow_id, status=outcome)
        run_events.record(session.state, finished)


async def _drain(task: asyncio.Task[None]) -> None:
    """Let the cancelled mirror finish, so its last poll cannot land after the run is recorded."""
    with suppress(asyncio.CancelledError):
        await task


async def _mirror(session: Runtime, handle: WorkflowHandle[DurableWorkflowRun, object], topics: TopicSink) -> None:
    """Poll the workflow's progress into the session for as long as the run lasts."""
    while True:
        await _mirror_once(session, handle, topics)
        await asyncio.sleep(_POLL_SECONDS)


async def _mirror_once(session: Runtime, handle: WorkflowHandle[DurableWorkflowRun, object], topics: TopicSink) -> None:
    """One refresh, applying transitions by cursor so the panel sees each one exactly once."""
    try:
        progress = await handle.query(DurableWorkflowRun.progress)
    except Exception:  # a closed or unserved workflow answers nothing, and the panel keeps its last view
        _logger.debug("progress query for {workflow} went unanswered", workflow=session.active.workflow_id)
        return
    moved = progress.transitions[len(session.active.progress.events) :]
    for transition in moved:
        if transition.finished:
            session.active.progress.finished(transition.node, failed=transition.failed)
        else:
            session.active.progress.started(transition.node)
    waiting = (
        None
        if progress.waiting is None
        else PendingReview(
            gate_id=progress.waiting.gate_id,
            workflow=session.active.workflow_id,
            reviewing=with_reviewers_note(render_for_review(progress.waiting.reviewing), progress.waiting.note),
            rejection_reruns=progress.waiting.rejection_reruns,
        )
    )
    changed = bool(moved) or (waiting is None) != (session.active.pending is None)
    session.active.pending = waiting
    if changed:
        topics.publish(f"run.{session.active.workflow_id}")


async def _record_evidence(
    session: Runtime, handle: WorkflowHandle[DurableWorkflowRun, object], *, records: RunRecords, ran: DurableRunOutcome
) -> None:
    """Append what the workflow says mechanically happened to the evidence, with its checked outcome as unknown."""
    try:
        evidence = await handle.query(DurableWorkflowRun.evidence)
        gates_approved = tuple(evidence.gates_approved)
        gates_auto = tuple(evidence.gates_auto_approved)
        gates_rejected = dict(evidence.gates_rejected)
        produced = dict(evidence.produced)
    except Exception:  # a run whose evidence cannot be read is still a run that happened
        _logger.warning("no evidence readable for {workflow}", workflow=session.active.workflow_id)
        gates_approved, gates_auto, gates_rejected, produced = (), (), {}, {}
    await records.evidence.append(
        RunEvidence(
            workflow_id=session.active.workflow_id,
            spec_hash=spec_identity(ran.spec),
            workflow_version=ran.spec.version,
            outcome=ran.outcome,
            result=Outcome(status="unknown", summary=_NOT_CHECKED),
            gates_approved=gates_approved,
            gates_auto=gates_auto,
            gates_rejected=gates_rejected,
            params=dict(ran.params),
            produced=produced,
            runner="app:harness+temporal",
        )
    )
