"""The agent tools that start, follow, reach into and stop workflow runs, and read what past runs recorded and cost.

A run is always a whole checked graph. To run a single step, run a one-node workflow.
"""

from __future__ import annotations

import asyncio
import json

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from core.ai.workflows.contracts import (
    AgentStep,
    LibraryError,
    MapNode,
    OrchestratorGate,
    PersonGate,
    RetryExhaustedError,
    RetryFeedback,
    RunEvidence,
    RunFinished,
    RunLaunched,
    RunStopped,
    StepIdentity,
    Workflow,
    WorkflowArguments,
    approved_gates,
    build_graph,
    performing_step,
    shapes_of,
    spec_identity,
    unsatisfied_parameters,
    validate,
)
from core.observability import get_logger, span
from pydantic import ValidationError

# Runtime imports, not TYPE_CHECKING: registering a function here as a tool makes pydantic-ai evaluate
# its annotations, so moving these under TYPE_CHECKING (ruff TC002) breaks app boot with a NameError.
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry

from harness.adapters.contracts import hold_run_writes
from harness.domain import contracts as session_events
from harness.domain.contracts import AdmissionRefused, admitted_arguments, presentation_of, session_type_of
from harness.features.running.approval import ChatApproval, combined_orchestrated_gates
from harness.features.running.domain.ended_run import EndedRun, checked_outcome
from harness.features.running.domain.progress import RunTopics, Watchers
from harness.features.running.exceptions import WorkflowNotRunnableError
from harness.features.running.feedback import settled_turn
from harness.features.running.infrastructure import run_events
from harness.features.running.infrastructure.run_log import RunLog, load_waiting
from harness.features.running.records import RunCollaborators, RunRecords
from harness.features.running.service import archive_run, index_held_writes, run_phase
from harness.features.running.service import stop as stop_active_run
from harness.features.running.spend import RunSpendHeader
from harness.features.running.spend_report import DEFAULT_SINCE_HOURS, SpendLine, SpendReporter, SpendUnavailable, parse_scope
from harness.rendering import render_for_review
from harness.session import PooledSessions, Runtime, bind_active, run_log_path

if TYPE_CHECKING:
    from decimal import Decimal

    from core.ai.workflows.contracts import Node

    from harness.features.running.domain.ended_run import RunArchiveRecord
    from harness.features.running.internal_contracts import RunArchiveQueries
    from harness.settings import RunSettings

_logger = get_logger(__name__)


RECENT_RUNS_LIMIT = 50
"""The most archived runs `recent_runs` lists, whatever `limit` asks for."""


@dataclass(frozen=True)
class Requested:
    """What a chat turn asked to be run: where the spec came from, the spec itself, and its arguments."""

    source: str
    """What the model named, a library id or the prose "your draft", as every sentence back to the
    conversation spells it."""
    spec: Workflow
    """The workflow that checked out here, which is what actually runs."""
    arguments: WorkflowArguments
    """The values it runs with, every declared parameter already answered and admitted."""

    @property
    def workflow_id(self) -> str:
        """The id a record is filed under: `"draft"` for the session's own draft, `source` otherwise."""
        return "draft" if self.source == "your draft" else self.source


async def run_workflow(
    ctx: RunContext[Runtime], workflow_id: str, arguments: WorkflowArguments, *, run: RunCollaborators, auto_approve_gates: bool = False
) -> str:
    """Start a saved workflow, or the current draft, running in the background. The graph runs it, you do not.

    Answers the moment the run is going. Independent steps run at the same time, a step consuming
    two others waits for both, and at a gate the run waits and the person is asked in the
    interface. A rejection reruns from the gate's rejection target rather than from the
    beginning. When the run settles (finished, failed, stopped, or paused) you get one
    message in this session saying how. Do not wait for it, poll in a loop, or start it again:
    tell the person the run is going and end your turn. `check_runs` shows progress when asked.
    `message_worker` reaches a worker while the run is waiting at a gate, and `stop_run` ends it.
    A session holding a paused run refuses a new one until the person resumes or stops it.

    Args:
        ctx: The run context. The session it carries supplies the library and the registry.
        workflow_id: A library id, or the word `draft` for the spec you last checked here.
        arguments: The workflow's parameters, one per declared parameter, each as the JSON value
            that parameter's own type asks for: a list where it declares a list, a number where it
            declares a number, true or false where it declares a boolean.
        run: Where this run's records land, where it announces, and the sessions it can reach.
        auto_approve_gates: Put every gate this run reaches on the record without asking anyone,
            on the person's explicit instruction only. Never counted as human approval.

    Returns:
        The sentence saying the run started, with its run id, its steps and its gates.

    Raises:
        ModelRetry: nothing is kept under `workflow_id`, it does not check out here, a declared
            parameter has no value, a value is not what its parameter declares, or the session is
            still running another workflow.
    """
    session = ctx.deps
    if workflow_id.strip().lower() == "draft":
        spec = session.conversation.last_workflow
        if spec is None:
            msg = "there is no checked draft to run. Call check_draft on a workflow first."
            raise ModelRetry(msg)
        source = "your draft"
    else:
        try:
            spec = await session.library.fetch_workflow(workflow_id)
        except (LibraryError, ValidationError) as exc:
            msg = f"could not load {workflow_id!r}: {exc}"
            raise ModelRetry(msg) from exc
        source = workflow_id

    report = validate(spec, session.registry)
    if not report.ok:
        refusal = WorkflowNotRunnableError(workflow_id, issues=report.issues)
        msg = f"refusing to run {source}. {refusal}. Do not substitute steps. Say what is missing."
        raise ModelRetry(msg) from refusal
    unsatisfied = unsatisfied_parameters(spec, arguments)
    if unsatisfied:
        msg = f"{source} needs values for: {', '.join(unsatisfied)}."
        raise ModelRetry(msg)

    if session.active.task is not None and not session.active.task.done():
        msg = f"{session.active.workflow_id} is still running in this session, and a session runs one workflow at a time. Wait for it, or tell the person."
        raise ModelRetry(msg)
    if (paused := session.active.paused_run_refusal()) is not None:
        raise ModelRetry(paused)
    try:
        # A draft has no card, so nothing declares a first-party selector over it and none is
        # checked: the step's own read is what refuses a knowledge base nothing answers.
        presentation = None if source == "your draft" else await presentation_of(session.library, workflow_id)
        admitted = await admitted_arguments(spec, arguments, presentation=presentation, names=run.knowledge_bases)
    except AdmissionRefused as exc:
        msg = f"{source} will not take those values: {exc}"
        raise ModelRetry(msg) from exc
    requested = Requested(source=source, spec=spec, arguments=admitted)
    return await _execute(session, run=run, requested=requested, auto_approve_gates=auto_approve_gates)


async def message_worker(ctx: RunContext[Runtime], node_id: str, message: str) -> str:
    """Send `message` to the worker running at `node_id` in this session's live run, only while waiting at a gate.

    Load `orchestrating-a-run` for what this does to the worker's history.

    Args:
        ctx: The run context. `ctx.deps` must be the session whose own live run holds the
            worker's conversation. A node from another session's run is refused exactly like an
            unknown one.
        node_id: The graph node running the worker, as shown on this session's live run.
        message: What to say to the worker.

    Raises:
        ModelRetry: the run is not waiting at a gate right now, `node_id` names a map, or no
            worker holds a conversation under that node in this session's live run.
    """
    session = ctx.deps
    if session.active.pending is None:
        msg = "the run is executing right now, message a worker while it waits at a gate."
        raise ModelRetry(msg)
    if isinstance(_live_node(session, node_id), MapNode):
        msg = (
            f"{node_id!r} is a map, it runs one worker per item, not one conversation to message. "
            "Message a single step, or reject the gate with feedback instead."
        )
        raise ModelRetry(msg)
    step_name = _live_step_name(session, node_id)
    if step_name is None:
        msg = f"no worker named {node_id!r} holds a conversation in this session's live run."
        raise ModelRetry(msg)
    conversation = session.active.conversations.conversations.get((node_id, ""))
    run_agent = session.registry.run_agent
    if conversation is None or run_agent is None or not isinstance(conversation.step, AgentStep):
        msg = f"no worker named {step_name!r} holds a conversation in this session's live run."
        raise ModelRetry(msg)
    # Bound around this call, not around the turn that reached here: the turn's own task carries no
    # binding of its own, and without one `composition.build_environment`'s shared closure would
    # write this exchange into the DEFAULT session's conversation line. The published step identity
    # is what keeps the reply landing on the SAME line it was read from: the node's own id, with
    # no item, exactly as the graph's own run of it would.
    with bind_active(session.active), performing_step(StepIdentity(node_id=node_id)):
        reply = await run_agent(conversation.step, conversation.output_type, message=message)
    return render_for_review(reply)


def _live_node(session: Runtime, node_id: str) -> Node | None:
    """The node `node_id` names in this session's live run's own spec, or `None` when there is no live run, or no such node."""
    waiting = load_waiting(run_log_path(session))
    if waiting is None:
        return None
    return next((candidate for candidate in waiting.spec.nodes if candidate.id == node_id), None)


def _live_step_name(session: Runtime, node_id: str) -> str | None:
    """The step name `node_id` names in this session's live run, or `None` for a gate, a map, or an unknown id."""
    node = _live_node(session, node_id)
    if node is None or isinstance(node, PersonGate | OrchestratorGate | MapNode):
        return None
    return node.work


async def _execute(  # noqa: PLR0915 (one reset line per session field the run panel reads, not a second job)
    session: Runtime, *, run: RunCollaborators, requested: Requested, auto_approve_gates: bool = False
) -> str:
    """Start the checked spec in `session`, setting the same session fields a button launch does."""
    # The session's asker, when its plugin has one: over ACP the gate arrives as an editor
    # permission prompt, and over HTTP it waits for the browser's /api/decide.
    approval = ChatApproval(session=session, workflow=requested.source, spec=requested.spec, asker=session.gate_asker, topics=run.topics)
    started_at = datetime.now(UTC)
    run_id = f"{started_at.strftime('%Y%m%dT%H%M%S%f')}-{uuid4().hex[:8]}"
    run.records.spend.begin(
        RunSpendHeader(
            run_id=run_id,
            workflow_id=requested.workflow_id,
            runner="app:chat",
            session_id=session.session_id or "",
            started_at=started_at.isoformat(),
        )
    )
    # In the run log like a button run: if the process dies while a gate is waiting, the next boot
    # restores it, and it then reports to the run panel rather than to this chat.
    run_log = RunLog.begin(
        path=run_log_path(session),
        run_id=run_id,
        workflow_id=requested.source,
        spec=requested.spec,
        params=dict(requested.arguments),
        gate_mode="auto" if auto_approve_gates else "wait",
        gate_timeout=approval.timeout_seconds,
        runner="app:chat",
        session_kind=session_type_of(session.session_kind),
        started_at=started_at.isoformat(),
    )
    graph = build_graph(
        requested.spec,
        session.registry,
        approval=approval,
        observer=Watchers(session.active.progress, run_log, RunTopics(workflow_id=requested.workflow_id, publish=run.topics.publish)),
    )
    outcome: Literal["completed", "failed", "stopped"] = "completed"
    failure: str | None = None
    cancelled = False
    produced: object = None
    exhausted: RetryFeedback | None = None

    session.session_log.record(session_events.RunLaunched(workflow_id=requested.workflow_id, run_id=run_id))
    run_events.record(session.state, RunLaunched(run_id=run_id, workflow_id=requested.workflow_id))
    session.active.workflow_id = requested.workflow_id
    session.active.run_id = run_id
    session.active.result = ""
    session.active.stopping = False
    session.active.paused_by_restart = False
    session.active.progress.reset()
    session.active.values = run_log.state.values
    session.active.started_at = started_at.isoformat()
    session.active.gate_mode = "auto" if auto_approve_gates else "wait"
    session.active.gate_timeout = approval.timeout_seconds
    session.active.auto_gates = approval.auto_gates
    session.active.orchestrated_gates = approval.orchestrated_gates
    session.active.conversations.reset()

    async def run_graph() -> None:
        nonlocal produced
        # Bound inside the task body, not around `asyncio.create_task` below: a task copies its
        # creator's context at creation, so this is what lets `composition.build_environment`'s shared
        # step closures resolve THIS session's `active` rather than the one they were built over.
        with bind_active(session.active), hold_run_writes(run_id) as held, span("run_workflow {workflow}", workflow=requested.source):
            try:
                produced = await graph.run(state=run_log.state)
            finally:
                # Indexed before `settle` records the ending. A pause indexes too, because its resume runs under a new run id.
                await index_held_writes(held)

    task = asyncio.create_task(run_graph())
    session.active.task = task

    async def settle() -> str:
        """Await the run in its own task, record and archive it unless it was cancelled, and return the outcome sentence."""
        nonlocal outcome, failure, cancelled, exhausted
        try:
            await task
        except asyncio.CancelledError:
            # The run surface cancelled the graph. A pause keeps the run log as a process death
            # would leave it and records no evidence. A stop records and archives the run itself.
            cancelled = True
            if session.active.stopping:
                session.active.result = "Stopped. Ended for good, and there is nothing to resume."
                return f"{requested.source} was stopped for good by the person from the run panel: nothing to resume, do not restart it."
            session.active.result = "Paused. The run log is on disk. Edit the graph and resume."
            return (
                f"{requested.source} was paused by the person from the run panel. Its run log is waiting on disk, "
                "so they can edit the graph and resume it there. Do not restart it."
            )
        except RetryExhaustedError as exc:
            # The one failure that is itself a result. The whole refusal is on the error, and the
            # run log never carries it, because the log is deleted on the way out.
            exhausted = exc.feedback
            outcome, failure = "failed", f"{type(exc).__name__}: {exc}"
            _logger.info("run_workflow of {workflow} ended holding a refusal it had no reach left to repair", workflow=requested.source)
        except Exception as exc:  # a failed run is news the model relays, never a dead turn
            outcome, failure = "failed", f"{type(exc).__name__}: {exc}"
            _logger.exception("run_workflow of {workflow} failed", workflow=requested.source)
        finally:
            # The chat entry records exactly as the button does (`service.run`), differing only in
            # `runner`, so the evidence says which plugin the run came through. A cancelled run
            # records nothing and keeps its run log: a run log on disk IS the waiting run a resume
            # or the next boot picks up.
            if not cancelled:
                auto = session.active.auto_gates()
                orchestrated = combined_orchestrated_gates(session, requested.spec, run_log.state.values)
                gates_approved = tuple(
                    g for g in approved_gates(requested.spec, run_log.state.values) if g not in auto and g not in orchestrated
                )
                gates_rejected = approval.rejections()
                result = checked_outcome(
                    EndedRun(
                        spec=requested.spec,
                        run_id=run_id,
                        status=outcome,
                        values=run_log.state.values,
                        params=dict(requested.arguments),
                        work_facts=list(run_log.state.work_facts.values()),
                        batches=run_log.state.batches,
                        gate_decisions=run_log.state.gate_decisions,
                        gate_generations=run_log.state.gate_generations,
                        exhausted=exhausted,
                    )
                )
                await run.records.evidence.append(
                    RunEvidence(
                        workflow_id=requested.workflow_id,
                        spec_hash=spec_identity(requested.spec),
                        workflow_version=requested.spec.version,
                        outcome=outcome,
                        result=result,
                        gates_approved=gates_approved,
                        gates_auto=auto,
                        gates_orchestrated=orchestrated,
                        gates_rejected=gates_rejected,
                        params=dict(requested.arguments),
                        produced=shapes_of(run_log.state.values),
                        runner="app:chat",
                    )
                )
                archive_run(
                    session,
                    archive=run.records.archive,
                    outcome=outcome,
                    failure=failure or "",
                    runner="app:chat",
                    run_id=run_id,
                    spec=requested.spec,
                    params=dict(requested.arguments),
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
                session.active.conversations.close_all()
                run_log.close()
                run_events.record(session.state, RunFinished(run_id=run_id, workflow_id=requested.workflow_id, status=outcome))
        if failure is not None:
            session.active.result = f"The run stopped with an error: {failure}"
            return f"{requested.source} failed while running: {failure}\nThe failure is recorded. Tell the person what happened. Do not silently retry."
        session.active.result = f"Finished. Result ({type(produced).__name__}):\n{render_for_review(produced)}"
        return _finished(requested.source, requested.spec, approval, produced)

    # The turn that started the run never waits for it: `settlement` waits the run out however
    # that turn ends, and `deliver` hands the outcome back to the conversation as one headless
    # turn of this session's own agent, waited for behind whatever turn is live then.
    settlement = asyncio.create_task(settle())
    session.active.settlement = settlement

    async def deliver() -> None:
        answer = await settlement
        await settled_turn(session, workflow=requested.source, prompt=_settled_prompt(requested.source, answer), sessions=run.sessions)

    delivery = asyncio.create_task(deliver())
    _DELIVERIES.add(delivery)
    delivery.add_done_callback(_DELIVERIES.discard)
    return _started(requested.source, requested.spec, run_id)


async def check_runs(ctx: RunContext[Runtime], *, sessions: PooledSessions) -> str:
    """What is running right now, in this session and every other one this app holds, one line each.

    Args:
        ctx: The run context. Its session is the one each line calls "this session".
        sessions: The pool of sessions this app holds.

    Returns:
        One line per run in flight, or a sentence saying nothing is running.
    """
    lines: list[str] = []
    for session in (await sessions.pool()).held_sessions():
        phase = run_phase(session)
        if phase is None:
            continue
        held_id = session.session_id
        where = "this session" if session is ctx.deps else f"session {held_id[:8]}" if held_id is not None else "the app's own session"
        active = session.active
        note = {
            "waiting": f"waiting at gate {active.pending.gate_id!r}, waiting for the person to decide in the interface"
            if active.pending is not None
            else "waiting at a gate",
            "running": f"running ({len(active.progress.done)} steps done, {', '.join(sorted(active.progress.running)) or 'between steps'} now)",
            "paused": "paused, run log on disk, resumable from the run panel",
        }[phase]
        lines.append(f"- {active.workflow_id} in {where}: {note}")
    if not lines:
        return "Nothing is running in any session right now."
    return "Runs in flight:\n" + "\n".join(lines)


async def stop_run(ctx: RunContext[Runtime], session: str = "", *, records: RunRecords, sessions: PooledSessions, run: RunSettings) -> str:
    """Stop a running workflow for good, the same ending the run page's Stop button gives it.

    The graph is cancelled, the record is archived as stopped, and nothing is resumable
    afterward, and work the run had not done stays pending for the next pass. With no argument this
    stops the run in this session (the one run_workflow or digest_now started here), and a session
    id or prefix as check_runs prints it stops that session's run instead. A paused run is stopped too.

    Args:
        ctx: The run context. The session it carries is the default target.
        session: A session id or its prefix from check_runs, "" for this session.
        records: Where the stopped run's own records land.
        sessions: Where a session named by its id is looked up.
        run: Which engine this deployment runs workflows on.

    Returns:
        A sentence saying what was stopped, or why nothing was.
    """
    target = ctx.deps if session == "" else await _held_session(sessions, session)
    if target is None:
        return f"no held session matches {session!r}. check_runs names the ones with runs in flight."
    if run.run_engine == "temporal" and target.active.paused_run_refusal() is None:
        return "this deployment runs its workflows on Temporal, where a run outlives this process. Stop it from its run page."
    workflow_id = target.active.workflow_id
    if not await stop_active_run(target, records=records):
        where = "this session" if target is ctx.deps else f"session {session!r}"
        return f"nothing is running in {where}. check_runs shows what is in flight where."
    run_events.record(target.state, RunStopped(run_id=target.active.run_id or "", workflow_id=workflow_id))
    return f"Stopped {workflow_id}. Its record is archived as stopped, and what it had not done stays pending for the next run."


async def _held_session(sessions: PooledSessions, prefix: str) -> Runtime | None:
    """The one held session whose id starts with `prefix`, or `None` when none (or several) do."""
    held = (await sessions.pool()).held_sessions()
    matches = [session for session in held if session.session_id is not None and session.session_id.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


async def recent_runs(archive: RunArchiveQueries, limit: int = 10) -> str:
    """The last `limit` archived runs across every workflow, newest first, one line each.

    Each line carries the run id, the workflow, how the run ended, what it checked out as, when it
    ended, and the params it ran with.

    Args:
        archive: The run archive to read.
        limit: How many runs to list, held between 1 and `RECENT_RUNS_LIMIT`.

    Returns:
        The lines under a header, or a sentence when nothing is archived.
    """
    records = archive.recent(max(1, min(limit, RECENT_RUNS_LIMIT)))
    if not records:
        return "No archived runs yet."
    lines = [
        f"- {r.run_id} {r.workflow_id} ran {r.outcome}, checked outcome {_checked(r)} ended {r.ended_at} params {json.dumps(r.params)}"
        for r in records
    ]
    return "Archived runs, newest first. 'ran' is how the execution ended, 'checked outcome' is what the work adds up to:\n" + "\n".join(
        lines
    )


def _checked(record: RunArchiveRecord) -> str:
    """One run's checked outcome in a phrase, saying plainly when nothing was checked."""
    if record.result is None:
        return "unknown (this run predates outcome reporting)"
    result = record.result
    if result.attempted == 0:
        return result.status
    return f"{result.status} ({result.succeeded}/{result.attempted} succeeded, {result.skipped} skipped, {result.failed} failed)"


async def run_values(archive: RunArchiveQueries, run_id: str, node: str = "") -> str:
    """One archived run's per-node values as JSON, or one node's.

    This is what the run recorded, not a check that the work was any good: recent_runs carries the
    checked outcome, and a value here being non-empty proves nothing about it. The values come back
    whole, however long.

    Args:
        archive: The run archive to read.
        run_id: The archived run to read.
        node: One node's id, or "" for every node.

    Returns:
        The values as indented JSON, or a sentence when the run or the node is unknown.
    """
    record = archive.get(run_id)
    if record is None:
        return f"No archived run {run_id!r}, recent_runs lists the ones on file."
    if node:
        if node not in record.values:
            return f"{run_id} has no node {node!r}, its nodes are: {', '.join(sorted(record.values))}"
        return json.dumps(record.values[node], indent=1, ensure_ascii=False)
    return json.dumps(record.values, indent=1, ensure_ascii=False)


def _money(value: Decimal) -> str:
    return f"${value:.4f}"


def _tokens(line: SpendLine) -> str:
    return f"{line.input_tokens} in, {line.output_tokens} out, {line.cache_read_tokens} cached"


async def spend_report(reporter: SpendReporter, scope: str, subject: str = "", since_hours: int = DEFAULT_SINCE_HOURS) -> str:
    """What something cost, from the telemetry, as lines a person reads.

    Args:
        reporter: The telemetry reader.
        scope: One of run, workflow, session, step, model or day. The first four are about one thing,
            named by `subject`.
        subject: A run id for run and step, a workflow id such as wf.digest.v9, or a session id.
            "" for model and day.
        since_hours: How many hours back to read.

    Returns:
        A total then one line per key, a sentence when nothing was priced or the scope is unknown,
        and the reason plus where the spend log on disk still answers when the telemetry cannot be
        reached.
    """
    try:
        answer = await reporter.report(parse_scope(scope), subject, since_hours)
    except ValueError as error:
        return str(error)
    if isinstance(answer, SpendUnavailable):
        return f"{answer.reason}. {answer.fallback}"
    about = f"{answer.scope} {answer.subject}".rstrip()
    if not answer.lines:
        return (
            f"No priced model calls for {about} in the last {answer.since_hours} hours. "
            "Widen since_hours, or check the id with recent_runs."
        )
    head = (
        f"Spend for {about} over the last {answer.since_hours} hours (telemetry) is {_money(answer.total.cost_usd)} "
        f"across {answer.total.calls} calls, {_tokens(answer.total)}."
    )
    return "\n".join([head, *(f"- {line.key} {_money(line.cost_usd)}, {line.calls} calls, {_tokens(line)}" for line in answer.lines)])


_DELIVERIES: set[asyncio.Task[None]] = set()
"""Every settlement delivery under way, held so the event loop is not its only reference."""


def _started(source: str, spec: Workflow, run_id: str) -> str:
    """The turn's account of a run that just started: what runs, where its gates land, what comes next."""
    steps = ", ".join(node.work for node in spec.nodes if node.kind == "run")
    gates = [node.id for node in spec.nodes if node.kind == "gate"]
    gate_note = f" Gates on the way: {', '.join(gates)}. Each waits for a decision in the interface." if gates else ""
    return (
        f"{source} is now running in the background (run {run_id}). Steps: {steps}.{gate_note}\n"
        "You will get one message in this session when it settles. Do not wait for it or poll in a "
        "loop: tell the person the run is going and end your turn."
    )


def _settled_prompt(source: str, answer: str) -> str:
    """What the settled turn is asked: the outcome, and the one job of relaying it."""
    return (
        f"The run of {source} you started earlier with run_workflow has settled.\n{answer}\n"
        "Tell the person the outcome in one short message. Do not start or restart anything unless they ask."
    )


def _finished(source: str, spec: Workflow, approval: ChatApproval, produced: object) -> str:
    """The turn's account of a finished run: the steps, the gates as they actually went with each rejection named, the result."""
    ran = ", ".join(node.work for node in spec.nodes if node.kind == "run")
    gates = [node.id for node in spec.nodes if node.kind == "gate"]
    gate_note = f" Gates crossed: {', '.join(gates)}." if gates else ""
    if rejected := approval.rejections():
        gate_note += " Rejected first: " + ", ".join(f"{gate_id} ({count}x, reran its upstream)" for gate_id, count in rejected.items())
    return f"{source} finished. Steps: {ran}.{gate_note}\nResult ({type(produced).__name__}):\n{render_for_review(produced)}"
