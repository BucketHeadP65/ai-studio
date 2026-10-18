"""Launching, pausing, stopping and resuming a run, on one path shared by the Run button, boot-time restoring and a resume.

The caller checks that nothing is already running, that the workflow validates and that its
parameters are filled in. This module owns the run log, the watchers, the background task and
the session fields the panels read.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from core.ai.workflows.contracts import (
    GateDecided,
    LibraryError,
    RetryExhaustedError,
    RetryFeedback,
    RunEvidence,
    RunFinished,
    RunLaunched,
    RunPaused,
    RunResumed,
    RunStopped,
    Workflow,
    WorkflowArguments,
    approved_gates,
    build_graph,
    moved_nodes,
    normalize_arguments,
    shapes_of,
    spec_identity,
    validate,
)
from core.observability import get_logger, span
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessagesTypeAdapter

from harness.adapters.contracts import hold_run_writes
from harness.domain import contracts as session_events
from harness.domain.contracts import SessionType, effective_gate_mode, session_type_of
from harness.features.running.approval import ChatApproval, combined_orchestrated_gates
from harness.features.running.domain.ended_run import EndedRun, RunArchiveRecord, checked_outcome, jsonable_values
from harness.features.running.domain.progress import GateVerdict, RunTopics, Watchers
from harness.features.running.exceptions import (
    AutomodeUnavailableError,
    RunEngineUnavailableError,
    RunGraphRefusedError,
    RunStateRefusedError,
    WorkflowNotRunnableError,
)
from harness.features.running.infrastructure import durable, run_events
from harness.features.running.infrastructure.run_log import (
    DEFAULT_GATE_TIMEOUT,
    DEFAULT_RUNNER,
    CarriedState,
    RunLog,
    carried_state,
    load_waiting,
    reviewed_values,
    revive_values,
)
from harness.features.running.spend import RunSpendHeader
from harness.rendering import render_for_review
from harness.session import bind_active, run_log_path, verdict_on

if TYPE_CHECKING:
    from core.ai.workflows.contracts import Outcome
    from core.cqrs.contracts import TopicSink
    from pydantic_ai.messages import ModelMessage

    from harness.adapters.contracts import RunIndexing
    from harness.domain.contracts import GateMode
    from harness.features.running.domain.ended_run import ReviewedValue
    from harness.features.running.internal_contracts import RunArchive
    from harness.features.running.records import RunRecords
    from harness.session import Runtime
    from harness.settings import RunSettings

_logger = get_logger(__name__)


_ENGINE_REACHABLE = True
"""Whether the durable engine answered at boot. Installed by the composition root, read by `start`."""


def install_engine_reachable(*, reachable: bool) -> None:
    """Record whether the durable engine answered when this process composed itself.

    Only the composition root calls this.

    Args:
        reachable: Whether the configured Temporal address answered.
    """
    global _ENGINE_REACHABLE  # noqa: PLW0603 (the one-process-one-engine composition global is the design)
    _ENGINE_REACHABLE = reachable


def engine_reachable() -> bool:
    """Whether the durable engine answered when this process composed itself.

    Returns:
        What the composition root installed, True until it installs anything.
    """
    return _ENGINE_REACHABLE


async def start(  # noqa: PLR0913 (a launch's knobs are one flat contract, a params object would just rename it)
    session: Runtime,
    *,
    records: RunRecords,
    topics: TopicSink,
    spec: Workflow,
    workflow_id: str,
    params: WorkflowArguments,
    run: RunSettings,
    memo: dict[str, object] | None = None,
    gate_timeout: float | None = DEFAULT_GATE_TIMEOUT,
    runner: str = DEFAULT_RUNNER,
    gate_mode: GateMode = "wait",
    auto_approve_gates: bool = False,
    run_title: str = "",
) -> None:
    """Start a run on whichever engine this deployment selected.

    Both engines leave the same session fields behind, so a caller never needs to know which one
    answered.

    Args:
        session: The session the run belongs to.
        records: Where this run's own records land.
        topics: Where the run says it moved.
        spec: The workflow to run.
        workflow_id: The workflow's id.
        params: The arguments, normalized against the workflow before either engine sees them.
        run: Which engine this deployment runs workflows on, and how to reach it.
        memo: Node outputs already settled, replayed rather than run again. The durable engine
            does not use it.
        gate_timeout: How long a gate waits for a person before rejecting. `None` waits forever, as
            a scheduled run under gate policy "wait" does. The durable engine does not use it.
        runner: The launcher the evidence record and the archive name. A schedule passes its own
            identity.
        gate_mode: What a person gate does. `"wait"` holds it, `"auto"` passes it with nobody asked,
            and `"orchestrate"` hands it to this run's own orchestrator.
        auto_approve_gates: Kept for old callers. True runs the gates as `"auto"`, and pairing it with
            `gate_mode="orchestrate"` is refused.
        run_title: What this run is called in front of a person, in the words of whoever started it.
            `""` is a run nobody named, and nothing here invents one.

    Raises:
        ValueError: `gate_mode` and `auto_approve_gates` were both given and disagree.
        ArgumentRefusedError: one argument its parameter's own schema will not admit, on either
            engine. A first-party selection is not checked here, but by whichever entry resolved
            the card.
        AutomodeUnavailableError: the resolved mode is `"orchestrate"` and this deployment runs on
            Temporal, which has no orchestrated gate.
        RunEngineUnavailableError: this deployment runs on Temporal and nothing served the
            configured address when the process composed itself. It never falls back to the
            in-process engine.
    """
    resolved = effective_gate_mode(gate_mode, auto_approve_gates=auto_approve_gates)
    params = normalize_arguments(spec, params)
    if run.run_engine == "temporal":
        if not engine_reachable():
            msg = f"nothing serves Temporal at {run.temporal_address}, so this deployment cannot start a durable run."
            raise RunEngineUnavailableError(msg)
        if resolved == "orchestrate":
            msg = "this deployment runs on Temporal, where an orchestrated gate has no counterpart yet. Start it in wait or auto mode."
            raise AutomodeUnavailableError(msg)
        # The durable engine takes no memo. A durable run never stops, so it has nothing to resume.
        await durable.launch(
            session,
            records=records,
            topics=topics,
            run=run,
            spec=spec,
            workflow_id=workflow_id,
            params=params,
            auto_approve_gates=resolved == "auto",
            runner=runner,
            run_title=run_title,
        )
        return
    launch(
        session,
        records=records,
        topics=topics,
        spec=spec,
        workflow_id=workflow_id,
        params=params,
        memo=memo,
        gate_timeout=gate_timeout,
        runner=runner,
        gate_mode=resolved,
        run_title=run_title,
    )


def launch(  # noqa: PLR0913, PLR0915 (the same flat contract `start` forwards verbatim, and the record fields it binds)
    session: Runtime,
    *,
    records: RunRecords,
    topics: TopicSink,
    spec: Workflow,
    workflow_id: str,
    params: WorkflowArguments,
    memo: dict[str, object] | None = None,
    carried: CarriedState | None = None,
    gate_timeout: float | None = DEFAULT_GATE_TIMEOUT,
    runner: str = DEFAULT_RUNNER,
    gate_mode: GateMode = "wait",
    auto_approve_gates: bool = False,
    run_title: str = "",
    session_kind: SessionType | None = None,
) -> None:
    """Start a run in the background on the in-process engine, or resume one when given a memo, logged end to end.

    A session holds one run log, so the caller must refuse a second concurrent run in the same
    session. The launch is recorded on `session.session_log`, so a project session's run lands on
    that project's timeline. What became of the run is written to the run's own event file. Each
    knowledge base the run wrote under run indexing once per run is indexed once when the run ends,
    fails or pauses, before its ending is recorded.

    Args:
        session: The session the run belongs to, which holds its run log.
        records: Where this run's own records land.
        topics: Where the run says it moved.
        spec: The workflow to run.
        workflow_id: The workflow's id.
        params: The arguments the run starts with.
        memo: Node outputs already settled, replayed rather than run again.
        carried: What a paused run handed this one. A resume owes the repairs that run owed and has
            spent the gate reaches it spent.
        gate_timeout: How long a gate waits for a person before rejecting. `None` waits forever.
        runner: The launcher the evidence record and the archive name.
        gate_mode: What a person gate does, resolved together with `auto_approve_gates` exactly as
            `start` resolves them.
        auto_approve_gates: Kept for old callers. True runs the gates as `"auto"`, and pairing it with
            `gate_mode="orchestrate"` is refused.
        run_title: What this run is called. It is written into the run log, so a resume or a restore
            of it carries the same name.
        session_kind: The kind of session the run log names the run as living in. `None` names the kind
            of `session`. A restore passes the logged kind, so a run adopted by another session keeps its own.

    Raises:
        ValueError: `gate_mode` and `auto_approve_gates` were both given and disagree.
    """
    resolved = effective_gate_mode(gate_mode, auto_approve_gates=auto_approve_gates)
    started_at = datetime.now(UTC)
    run_id = f"{started_at.strftime('%Y%m%dT%H%M%S%f')}-{uuid4().hex[:8]}"
    records.spend.begin(
        RunSpendHeader(
            run_id=run_id, workflow_id=workflow_id, runner=runner, session_id=session.session_id or "", started_at=started_at.isoformat()
        )
    )
    run_log = RunLog.begin(
        path=run_log_path(session),
        run_id=run_id,
        workflow_id=workflow_id,
        spec=spec,
        params=params,
        memo=memo,
        carried=carried,
        run_title=run_title,
        gate_mode=resolved,
        gate_timeout=gate_timeout,
        runner=runner,
        session_kind=session_kind or session_type_of(session.session_kind),
        started_at=started_at.isoformat(),
    )
    approval = ChatApproval(session=session, workflow=workflow_id, spec=spec, timeout_seconds=gate_timeout, topics=topics)
    graph = build_graph(
        spec,
        session.registry,
        approval=approval,
        observer=Watchers(session.active.progress, run_log, RunTopics(workflow_id=workflow_id, publish=topics.publish)),
    )

    async def run_graph() -> None:
        outcome: Literal["completed", "failed"] = "failed"
        failure = ""
        paused = False
        exhausted: RetryFeedback | None = None
        # Bound inside the task body, not around `asyncio.create_task` below: a task copies its
        # creator's context at creation, so this is what lets `composition.build_environment`'s shared
        # step closures resolve THIS session's `active` rather than the one they were built over.
        with bind_active(session.active), hold_run_writes(run_id) as held:
            try:
                with span("run {workflow}", workflow=workflow_id):
                    produced = await graph.run(state=run_log.state)
                outcome = "completed"
                session.active.result = f"Finished. Result ({type(produced).__name__}):\n{render_for_review(produced)}"
            except asyncio.CancelledError:
                # A cancelled run is a pause, not an ending: the run log stays as a process death
                # would leave it and nothing is recorded. `CancelledError` is a `BaseException`,
                # so without this arm the `finally` would record a failure and delete the run log.
                # Re-raised rather than swallowed, because a task that eats its cancellation cannot
                # be stopped.
                paused = True
                session.active.result = "Paused. The run log is on disk. Edit the graph and resume."
                _logger.info("run of {workflow} paused with its run log intact", workflow=workflow_id)
                raise
            except RetryExhaustedError as exc:
                # The one failure that is itself a result: the ending has the whole refusal. The run
                # log never will, because the retry step is unobserved and the log is deleted below.
                exhausted = exc.feedback
                failure = f"{type(exc).__name__}: {exc}"
                _logger.info("run of {workflow} ended holding a refusal it had no reach left to repair", workflow=workflow_id)
                session.active.result = f"The run stopped with an error: {failure}"
            except Exception as exc:  # a failed run is news, not a silent stop
                failure = f"{type(exc).__name__}: {exc}"
                _logger.exception("run of {workflow} failed", workflow=workflow_id)
                session.active.result = f"The run stopped with an error: {failure}"
            finally:
                # Indexed before the ending is recorded, so whatever reacts to the ending finds the
                # knowledge bases indexed. A pause indexes too, because its resume runs under a new run id.
                await index_held_writes(held)
                # Record what mechanically happened, never an opinion. Produced SHAPES, never values:
                # two honest runs over a live knowledge base produce different values and must still
                # read alike.
                if not paused:
                    auto = session.active.auto_gates()
                    orchestrated = combined_orchestrated_gates(session, spec, run_log.state.values)
                    gates_approved = tuple(g for g in approved_gates(spec, run_log.state.values) if g not in auto and g not in orchestrated)
                    gates_rejected = approval.rejections()
                    result = checked_outcome(
                        EndedRun(
                            spec=spec,
                            run_id=run_id,
                            status=outcome,
                            values=run_log.state.values,
                            params=params,
                            work_facts=list(run_log.state.work_facts.values()),
                            batches=run_log.state.batches,
                            gate_decisions=run_log.state.gate_decisions,
                            gate_generations=run_log.state.gate_generations,
                            exhausted=exhausted,
                        )
                    )
                    await records.evidence.append(
                        RunEvidence(
                            workflow_id=workflow_id,
                            # The id beside it is an address that changes when a library re-keys. The
                            # hash is a fact about the workflow and survives that.
                            spec_hash=spec_identity(spec),
                            # A library may mint one id per workflow NAME, so without the version an
                            # adapted child inherits the runs of the thing it replaced.
                            workflow_version=spec.version,
                            outcome=outcome,
                            result=result,
                            gates_approved=gates_approved,
                            gates_auto=auto,
                            gates_orchestrated=orchestrated,
                            gates_rejected=gates_rejected,
                            params=dict(params),
                            produced=shapes_of(run_log.state.values),
                            runner=runner,
                        )
                    )
                    archive_run(
                        session,
                        archive=records.archive,
                        outcome=outcome,
                        failure=failure,
                        runner=runner,
                        run_id=run_id,
                        spec=spec,
                        params=params,
                        values=run_log.state.values,
                        conversations={
                            name: conversation.messages for name, conversation in session.active.conversations.conversations.items()
                        },
                        gates_approved=gates_approved,
                        gates_rejected=gates_rejected,
                        gates_auto=auto,
                        gates_orchestrated=orchestrated,
                        result=result,
                        reviewed=run_log.reviewed(),
                    )
                    # A run log that outlives its run is precisely a waiting run, whether a pause or a
                    # process death left it there.
                    run_log.close()
                    run_events.record(session.state, RunFinished(run_id=run_id, workflow_id=workflow_id, status=outcome))
                # A pause ends the wire exactly as any other ending does: every line a viewer is
                # tailing closes now, rather than waiting on a resume that may open fresh lines much
                # later, or never.
                session.active.conversations.close_all()

    session.session_log.record(session_events.RunLaunched(workflow_id=workflow_id, run_id=run_id))
    run_events.record(session.state, RunLaunched(run_id=run_id, workflow_id=workflow_id))
    session.active.workflow_id = workflow_id
    session.active.run_title = run_title
    session.active.run_id = run_id
    session.active.result = ""
    session.active.stopping = False
    session.active.paused_by_restart = False
    session.active.progress.reset()
    session.active.values = run_log.state.values
    session.active.started_at = started_at.isoformat()
    session.active.gate_mode = resolved
    session.active.auto_approve = resolved == "auto"
    session.active.gate_timeout = approval.timeout_seconds
    session.active.auto_gates = approval.auto_gates
    session.active.orchestrated_gates = approval.orchestrated_gates
    session.active.conversations.reset()
    # This launch's `run_graph` settles its own run, so a chat-started predecessor's settlement task
    # must not read as this run's.
    session.active.settlement = None
    for node_id in run_log.state.memo:
        session.active.progress.replayed(node_id)  # the prefix is settled before the task even starts
    session.active.task = asyncio.create_task(run_graph())


async def index_held_writes(held: RunIndexing) -> None:
    """Index the knowledge bases a run held back for its end, absorbing a pause or stop that lands meanwhile.

    The pause or stop waits for the index. Once absorbed, a graph that had ended records that ending and a
    paused one still ends paused. A re-raised cancellation would leave an ended run reading as paused.

    Args:
        held: The writes the run held back.
    """
    try:
        await held.flush()
    except asyncio.CancelledError:
        if (task := asyncio.current_task()) is not None:
            task.uncancel()


def run_phase(session: Runtime) -> Literal["running", "paused", "waiting"] | None:
    """Where `session`'s run stands.

    Live means started and not finished: running, waiting at a gate, or paused with its run log on
    disk. Every reader asking whether a run is live in a session uses this, the run panel's cards
    and the agent's own look at the pool alike.

    Args:
        session: The session to read.

    Returns:
        `"running"`, `"waiting"` or `"paused"` for a live run, or `None` when nothing is live.
    """
    task = session.active.task
    if task is None:
        return "paused" if session.active.paused_by_restart else None
    if session.active.pending is not None:
        return "waiting"
    if not task.done():
        return "running"
    if task.cancelled():
        return "paused"
    return None


@dataclass(frozen=True)
class Resumption:
    """What a resume carried across from the paused run, and what it withdrew.

    A person reads both halves to see that the graph they changed is the graph that re-ran.
    """

    workflow_id: str
    replayed: tuple[str, ...]
    """Node ids seeding the new run's memo: settled work that will not be executed again."""
    withdrawn: tuple[str, ...]
    """Ids in the run log the new run will NOT replay, so every one of them runs fresh."""


async def pause(session: Runtime) -> bool:
    """Stop the running graph without ending it.

    The state a pause leaves is byte for byte what a restart already knows how to pick up. It
    returns only once the cancelled task has unwound, so the run log is quiescent and a resume
    never reads a half-written file. That includes indexing each knowledge base the run held back
    for its end, so a pause of such a run waits for those indexes. A run whose graph had already
    ended when the pause arrived records its own ending, and the pause answers False.

    Args:
        session: The session whose run to pause.

    Returns:
        True when there was a running graph to stop, False when nothing was running or the run ended on its own.
    """
    task = session.active.task
    if task is None or task.done():
        return False
    return await _cancelled(session, task)  # the pause is only real once the runner has finished unwinding, and said so


async def _cancelled(session: Runtime, task: asyncio.Task[None]) -> bool:
    """Cancel `task` and wait for it to unwind, answering False when it ended on its own and recorded that itself."""
    task.cancel()
    await asyncio.wait({task})
    if task.cancelled():
        return True
    # A run indexing what it held back for its end absorbs the cancel. A chat run records its ending in its settlement.
    if session.active.settlement is not None:
        await asyncio.wait({session.active.settlement})
    return False


async def stop(session: Runtime, *, records: RunRecords) -> bool:
    """End the running graph for good.

    It sets `stopping` before cancelling, so the cancellation arm that catches it says "stopped"
    rather than "paused". Once the task is quiescent the run log is read back, recorded as
    `outcome="stopped"`, archived and unlinked, and `run_phase` then reads the session as holding
    nothing live. The evidence and the archive record it writes carry no rejected gates, and name
    the launcher the run log names. It waits for the run to index each knowledge base it held back
    for its end, as `pause` does. A run whose graph had already ended when the stop arrived records
    its own ending and keeps its own result, and the stop records nothing and answers False.

    Args:
        session: The session whose run to stop.
        records: Where the ending is written.

    Returns:
        True when there was a running graph to end, or a paused run, which is ended from its run log.
        False when nothing was running or the run ended on its own.
    """
    task = session.active.task
    if task is None and session.active.paused_by_restart:
        pass
    elif task is not None and task.cancelled():
        pass  # a person paused it, so the run log is quiescent already
    elif task is None or task.done():
        return False
    else:
        session.active.stopping = True
        if not await _cancelled(session, task):
            session.active.stopping = False
            return False
    path = run_log_path(session)
    waiting = load_waiting(path)
    if waiting is not None:
        auto = session.active.auto_gates()
        orchestrated = combined_orchestrated_gates(session, waiting.spec, waiting.values)
        gates_approved = tuple(g for g in approved_gates(waiting.spec, waiting.values) if g not in auto and g not in orchestrated)
        reviewed = reviewed_values(path, waiting.run_id) if waiting.run_id else None
        result = checked_outcome(
            EndedRun(
                spec=waiting.spec,
                run_id=session.active.run_id or waiting.run_id,
                status="stopped",
                values=waiting.values,
                params=waiting.params,
                work_facts=waiting.work_facts,
                batches=waiting.batches,
                gate_decisions=waiting.gate_decisions,
                gate_generations=waiting.gate_generations,
            )
        )
        await records.evidence.append(
            RunEvidence(
                # `session.active.workflow_id`, not the run log's own field: the chat entry already
                # translates its prose source ("your draft") to the evidence's address ("draft")
                # there, and the archive record below reads the same translated field.
                workflow_id=session.active.workflow_id,
                spec_hash=spec_identity(waiting.spec),
                workflow_version=waiting.spec.version,
                outcome="stopped",
                result=result,
                gates_approved=gates_approved,
                gates_auto=auto,
                gates_orchestrated=orchestrated,
                params=dict(waiting.params),
                produced=shapes_of(waiting.values),
                runner=waiting.runner,
            )
        )
        archive_run(
            session,
            archive=records.archive,
            outcome="stopped",
            failure="",
            runner=waiting.runner,
            run_id=session.active.run_id,
            spec=waiting.spec,
            params=waiting.params,
            values=waiting.values,
            conversations={name: conversation.messages for name, conversation in session.active.conversations.conversations.items()},
            gates_approved=gates_approved,
            gates_orchestrated=orchestrated,
            gates_rejected={},
            gates_auto=auto,
            result=result,
            reviewed=reviewed.captured() if reviewed is not None else (),
        )
        if reviewed is not None:
            # The ending has the copies it needs, so this run's own captured values go with its log.
            reviewed.clear()
        # No `conversations.close_all()` here: `run_graph()`'s own `finally` closes every step
        # conversation's stream on every cancellation, pause or stop alike, and it already ran
        # before `await task` above returned.
    path.unlink(missing_ok=True)
    session.active.result = "Stopped. Ended for good, and there is nothing to resume."
    session.active.task = None
    session.active.paused_by_restart = False
    return True


def _run_id(session: Runtime) -> str:
    """The run this session launched, or empty when nothing in this slot ever minted one."""
    return session.active.run_id or ""


def set_session_automode(session: Runtime, run: RunSettings, *, on: bool) -> None:
    """Turn the live automode of the run in `session` on or off.

    It sets `gate_mode` together with `auto_approve`, as a launch does.

    Args:
        session: The session whose run is changed.
        run: The run settings, which name the engine.
        on: Whether automode is on.

    Raises:
        RunStateRefusedError: On the Temporal engine, where automode is chosen at launch, or when nothing is running.
    """
    if run.run_engine == "temporal":
        msg = "automode on Temporal is chosen when the run starts, not flipped here."
        raise RunStateRefusedError(msg)
    if session.active.task is None or session.active.task.done():
        msg = "nothing is running"
        raise RunStateRefusedError(msg)
    session.active.auto_approve = on
    session.active.gate_mode = "auto" if on else "wait"


async def decide_session_gate(session: Runtime, run: RunSettings, *, approve: bool, feedback: str) -> None:
    """Settle the gate the run in `session` is waiting at, on whichever engine is holding it, and file the decision.

    Args:
        session: The session whose run is waiting.
        run: The run settings, which name the engine.
        approve: Whether the gate is approved.
        feedback: What the decision says to the run.

    Raises:
        RunStateRefusedError: When nothing is waiting for a decision.
    """
    if run.run_engine == "temporal":
        if not await durable.decide(session, run, approve=approve, feedback=feedback):
            msg = "nothing is waiting for a decision"
            raise RunStateRefusedError(msg)
    else:
        decision = session.active.decision
        if decision is None or decision.done():
            msg = "nothing is waiting for a decision"
            raise RunStateRefusedError(msg)
        decision.set_result(GateVerdict(approve=approve, feedback=feedback))
    _logger.info("gate decided: {verdict}", verdict="approved" if approve else "rejected")
    pending = session.active.pending
    run_events.record(
        session.state,
        GateDecided(
            run_id=_run_id(session),
            workflow_id=session.active.workflow_id,
            gate_id=pending.gate_id if pending is not None else "",
            approved=approve,
            by="human",
        ),
    )


async def pause_session_run(session: Runtime, run: RunSettings) -> None:
    """Pause the run in `session` and file that it was paused.

    Args:
        session: The session whose run is paused.
        run: The run settings, which name the engine.

    Raises:
        RunStateRefusedError: On the Temporal engine, where the run outlives this process, or when nothing is running.
    """
    if run.run_engine == "temporal":
        msg = "this deployment runs on Temporal, where the run outlives this process, so there is nothing here to pause"
        raise RunStateRefusedError(msg)
    if not await pause(session):
        msg = "nothing is running"
        raise RunStateRefusedError(msg)
    run_events.record(session.state, RunPaused(run_id=_run_id(session), workflow_id=session.active.workflow_id))


async def stop_session_run(session: Runtime, run: RunSettings, records: RunRecords) -> None:
    """End the run in `session` for good and file that it was stopped.

    Args:
        session: The session whose run is stopped.
        run: The run settings, which name the engine.
        records: Where the stopped run is recorded and archived.

    Raises:
        RunStateRefusedError: On the Temporal engine unless the run is paused, or when nothing is running.
    """
    if run.run_engine == "temporal" and session.active.paused_run_refusal() is None:
        msg = "this deployment runs on Temporal, where the run outlives this process, so there is nothing here to stop"
        raise RunStateRefusedError(msg)
    if not await stop(session, records=records):
        msg = "nothing is running"
        raise RunStateRefusedError(msg)
    run_events.record(session.state, RunStopped(run_id=_run_id(session), workflow_id=session.active.workflow_id))


async def resume_session_run(session: Runtime, *, spec_yaml: str, records: RunRecords, topics: TopicSink) -> Resumption:
    """Relaunch the waiting run in `session` and file that it was resumed.

    The graph is `spec_yaml`, else the draft the session settled on, else the workflow the waiting run
    was launched from. A run a restart paused skips the draft and resumes its own logged workflow.
    Whichever it is, it is validated before the run relaunches. A run a restart paused keeps its logged
    gate mode, any other keeps the session's live one.

    Args:
        session: The session whose run resumes.
        spec_yaml: The graph to resume with, or empty to fall back.
        records: Where the resumed run is recorded.
        topics: Where the resumed run announces that it moved.

    Returns:
        What was replayed and what was withdrawn.

    Raises:
        RunStateRefusedError: When the run is still running, or nothing is paused.
        RunGraphRefusedError: When no graph can be found to resume with.
        WorkflowNotRunnableError: When the graph does not validate against this process.
    """
    if session.active.task is not None and not session.active.task.done():
        msg = f"{session.active.workflow_id} is still running"
        raise RunStateRefusedError(msg)
    # The panel checks a graph with `verdict_on` too, so the two always agree.
    sends_graph = bool(spec_yaml.strip())
    logged = load_waiting(run_log_path(session)) if session.active.paused_by_restart and not sends_graph else None
    if logged is not None:
        # A restart paused this run, so nothing the person drafted since is what it was running.
        spec, issues = logged.spec, []
        report = validate(spec, session.registry)
        if not report.ok:
            raise WorkflowNotRunnableError(logged.workflow_id, issues=report.issues)
    else:
        spec, issues = verdict_on(spec_yaml if sends_graph else session.conversation.draft.spec_yaml, session.registry)
    if spec is None and not issues:
        # Neither the body nor the draft named a graph, as for a run started from a saved workflow.
        # Fall back to the workflow the waiting run was launched from. A restart empties
        # `active.workflow_id`, so a rehydrated run is named by its waiting file.
        workflow_id = session.active.workflow_id or _waiting_workflow_id(session)
        if not workflow_id:
            msg = "send a graph to resume with, or settle a draft first"
            raise RunGraphRefusedError(msg)
        try:
            spec = await session.library.fetch_workflow(workflow_id)
        except (LibraryError, ValidationError) as exc:
            msg = f"{workflow_id}: {exc}"
            raise RunGraphRefusedError(msg) from exc
        report = validate(spec, session.registry)
        if not report.ok:
            raise WorkflowNotRunnableError(workflow_id, issues=report.issues)
    if spec is None or issues:
        draft_name = "draft"
        raise WorkflowNotRunnableError(draft_name, problems=issues)
    resumed = resume_with(
        session, records=records, topics=topics, spec=spec, gate_mode=None if session.active.paused_by_restart else session.active.gate_mode
    )
    if resumed is None:
        msg = "nothing is paused"
        raise RunStateRefusedError(msg)
    run_events.record(session.state, RunResumed(run_id=_run_id(session), workflow_id=resumed.workflow_id))
    return resumed


def _waiting_workflow_id(session: Runtime) -> str:
    """The workflow `session`'s waiting-run file names, or empty when there is none."""
    waiting = load_waiting(run_log_path(session))
    return waiting.workflow_id if waiting is not None else ""


def resume_with(
    session: Runtime, *, records: RunRecords, topics: TopicSink, spec: Workflow, gate_mode: GateMode | None = None
) -> Resumption | None:
    """Relaunch the paused run against `spec`, replaying every logged value the edit did not move.

    A value is withdrawn when `moved_nodes` says its node moved, when the new graph lacks its node,
    or when it no longer types against its step. It always launches on the in-process engine, as
    boot-time restoring does. The resumed run keeps the gate timeout and runner the log names, and the gate mode unless one is given.

    Args:
        session: The session holding the paused run's log.
        records: Where the new run's records land.
        topics: Where the new run says it moved.
        spec: The edited workflow to run.
        gate_mode: The gate mode to resume with, or `None` for the one the run log names.

    Returns:
        What was replayed and what was withdrawn, or `None` when no run log is on disk.
    """
    path = run_log_path(session)
    waiting = load_waiting(path)
    if waiting is None:
        return None
    moved, present = set(moved_nodes(waiting.spec, spec)), {node.id for node in spec.nodes}
    kept = frozenset(node_id for node_id in present if node_id not in moved)
    replayable = {node_id: value for node_id, value in waiting.values.items() if node_id in kept}
    # Typed against the steps of the NEW graph, so a value whose node kept its id but changed its
    # output type drops out too. A dropped value means the node re-runs, which is never wrong.
    memo = revive_values(spec, session.registry, replayable)
    paused_values = reviewed_values(path, waiting.run_id) if waiting.run_id else None
    carried = carried_state(waiting, kept=kept, reviewed=paused_values.captured() if paused_values is not None else ())
    _logger.info("resuming {workflow}: {n} logged steps replay", workflow=waiting.workflow_id, n=len(memo))
    launch(
        session,
        records=records,
        topics=topics,
        spec=spec,
        workflow_id=waiting.workflow_id,
        params=waiting.params,
        memo=memo,
        carried=carried,
        run_title=waiting.run_title,
        gate_mode=waiting.gate_mode if gate_mode is None else gate_mode,
        gate_timeout=waiting.gate_timeout,
        runner=waiting.runner,
        session_kind=waiting.session_kind,
    )
    if paused_values is not None:
        # The run that replaced it has re-filed everything it still owes a repair over, so the
        # paused run's own subtree has no reader left.
        paused_values.clear()
    return Resumption(workflow_id=waiting.workflow_id, replayed=tuple(memo), withdrawn=tuple(sorted(set(waiting.values) - set(memo))))


def archive_run(  # noqa: PLR0913 (mirrors the evidence both ending paths already build, and every field is a distinct fact of the run)
    session: Runtime,
    *,
    archive: RunArchive,
    outcome: Literal["completed", "failed", "stopped"],
    failure: str,
    runner: str,
    spec: Workflow,
    params: WorkflowArguments,
    values: dict[str, object],
    conversations: dict[tuple[str, str], list[ModelMessage]],
    gates_approved: tuple[str, ...],
    gates_rejected: dict[str, int],
    gates_auto: tuple[str, ...],
    gates_orchestrated: tuple[str, ...] = (),
    result: Outcome | None = None,
    reviewed: tuple[ReviewedValue, ...] = (),
    run_id: str | None = None,
) -> None:
    """File an ended run whole in the run archive.

    It does nothing when `archive` has archiving turned off. The workflow id, the run title, the
    started stamp and the transition log come from `session.active`, so a record is named by the
    launch rather than by whatever session hosted it.

    Args:
        session: The session the run ended in.
        archive: Where the record lands, and how long it rests there.
        outcome: How the run ended.
        failure: The error the run stopped with, `""` when it did not fail.
        runner: The launcher that started the run.
        spec: The workflow that ran.
        params: The arguments it ran with.
        values: Each node's recorded output.
        conversations: Each worker step's own messages at this same moment, by node and item.
        gates_approved: The gates a person approved.
        gates_rejected: How many times each gate was refused, by gate id.
        gates_auto: The gates passed with nobody asked.
        gates_orchestrated: The gates the run's own orchestrator decided.
        result: What the run's own facts say it did.
        reviewed: The values the run's gates saw.
        run_id: The id the launch minted, the one the spend log files under. When `None`, one is
            minted from the ended-at time and eight hex characters, so it sorts chronologically
            and never collides.
    """
    if archive.retention_days <= 0:
        return
    ended_at = datetime.now(UTC)
    record = RunArchiveRecord(
        run_id=run_id or f"{ended_at.strftime('%Y%m%dT%H%M%S%f')}-{uuid4().hex[:8]}",
        workflow_id=session.active.workflow_id,
        run_title=session.active.run_title,
        outcome=outcome,
        failure=failure,
        started_at=session.active.started_at,
        ended_at=ended_at.isoformat(),
        runner=runner,
        session_id=session.session_id or "",
        params=dict(params),
        spec=spec.model_dump(mode="json"),
        values=jsonable_values(values),
        conversations=_jsonable_conversations(conversations),
        events=list(session.active.progress.events),
        gates_approved=gates_approved,
        gates_rejected=dict(gates_rejected),
        gates_auto=gates_auto,
        gates_orchestrated=gates_orchestrated,
        result=result,
        reviewed=reviewed,
    )
    archive.put(record)


def _jsonable_conversations(conversations: dict[tuple[str, str], list[ModelMessage]]) -> dict[str, list[dict[str, object]]]:
    """Each worker's messages as plain JSON under its `node[item]` key, skipping one that cannot convert."""
    jsonable: dict[str, list[dict[str, object]]] = {}
    for (node, item), messages in conversations.items():
        try:
            jsonable[f"{node}[{item}]"] = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
        except ValueError:
            continue
    return jsonable
