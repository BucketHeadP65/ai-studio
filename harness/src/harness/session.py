"""The state every slice touches, one runtime per session, and the accessors for them.

A `Runtime` is one conversation, one draft and one run.
A `SessionPool` is every runtime this process is holding open, over the one agent it built.
This module imports only the slices' contracts, never their routes or agents.
"""

from __future__ import annotations

import asyncio
import time

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import yaml

from core.ai.workflows.contracts import StepRegistry, Workflow, WorkflowLibrary, validate
from core.observability import get_logger
from pydantic import ValidationError
from pydantic_ai.capabilities import on_event
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.compaction import (
    DEFAULT_CONTEXT_WINDOW,
    ContextUsage,
    ContextUsageEvent,
    ReportContextUsage,
    estimate_context_tokens,
)
from pydantic_ai_harness.conversation_search import ConversationSearch
from pydantic_ai_harness.step_persistence import InMemoryStepStore, RunRecord, StepStore, continue_run

from harness.adapters.contracts import ModelTiers, RecordHistorySource, activity_run, tier_context_window
from harness.domain.contracts import ConversationForgotten, SessionDeleted, SessionStarted, session_spans
from harness.features.authoring.contracts import Draft
from harness.features.running.contracts import GateVerdict, LiveProgress, PendingReview, StepConversations
from harness.features.selfauthoring.contracts import FileCustomCapabilities
from harness.settings import CHECKOUT, StateSettings
from harness.workspace import open_workspace

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime
    from pathlib import Path

    from core.ai.workflows.contracts import OrchestratorDecider
    from core.cqrs.contracts import TopicSink
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.models import ModelRequestContext
    from pydantic_ai.tools import DeferredToolRequests, RunContext
    from pydantic_ai_harness.capability_creation import CapabilityStore

    from harness.domain.contracts import GateMode, Recorded, SessionEvent, SessionEvents, SessionTranscript
    from harness.features.running.contracts import GateAsker
    from harness.workspace import Workspace

_logger = get_logger(__name__)

WAITING_RUN_SUFFIX = ".waiting-run.json"
"""The end of a pooled session's run log file name, after the session's id."""


def _default_custom_capabilities() -> CapabilityStore:
    """This checkout's own custom capabilities, read-only, for a session built with no store of its own."""
    return FileCustomCapabilities.read_only(CHECKOUT / "custom" / "capabilities")


@dataclass
class NoSessionLog:
    """A session log that records nothing, for a composition with no home to record into.

    A caller passes it on purpose. `Runtime.session_log` has no default, so no session records into
    this by accident.
    """

    def record(self, event: SessionEvent) -> Recorded | None:
        """Record nothing, anywhere.

        Args:
            event: The event, dropped.

        Returns:
            Always `None`.
        """
        del event
        return None

    def events(self) -> tuple[Recorded, ...]:
        """Nothing was ever recorded here.

        Returns:
            An empty tuple.
        """
        return ()

    def boot_sweep(self) -> None:
        """Nothing to bring up to date."""


@dataclass
class Conversation:
    """What the authoring slice remembers: the dialogue, and the draft under discussion."""

    history: list[ModelMessage] = field(default_factory=list)
    draft: Draft = field(default_factory=Draft)
    last_workflow: Workflow | None = None
    last_issues: tuple[str, ...] = ()


def _no_auto_gates() -> tuple[str, ...]:
    """The field default before any plugin has launched a run into this slot: automode passed nothing."""
    return ()


def _no_orchestrated_gates() -> tuple[str, ...]:
    """The field default before any plugin has launched a run into this slot: nothing was orchestrated."""
    return ()


@dataclass
class ActiveRun:
    """What the running slice owns: the one background run, and the gate it may be waiting at."""

    pending: PendingReview | None = None
    decision: asyncio.Future[GateVerdict] | None = None
    """Resolved by the person clicking Approve or Reject, and awaited inside the gate."""
    auto_approve: bool = False
    """Whether every gate this run reaches passes itself, with nobody asked. The record never counts
    such a pass as human approval. Set at launch from the launcher's own choice (the Run button's
    checkbox, a schedule's own setting, the agent's own argument) and changeable mid-run through
    `/api/run/automode`, so a gate reads it live off this field, never off a value the approval was
    built with. Kept for old callers. It is true exactly when `gate_mode` is `"auto"`."""
    gate_mode: GateMode = "wait"
    """The mode this run's own gates decide under. `"wait"` holds a person gate, `"auto"` passes it
    as `auto_approve` does, and `"orchestrate"` hands it to this run's own orchestrator. Only a gate
    that would otherwise wait on a person reads it. A declared orchestrator gate decides the same way
    whatever this holds. Set at launch and read live, like `auto_approve`."""
    gate_timeout: float | None = 900.0
    """How long this run's own gates wait before giving up, the same figure the run's approval was
    built with. A declared orchestrator gate reads it to bound how long its decider waits for a busy
    session's turn lock, never the decider's own model call. `None` waits forever. Set at launch."""
    conversations: StepConversations = field(default_factory=StepConversations)
    """Every worker step's own conversation in this run, the same instance `composition.build_environment`
    closed over. Writing it here writes the conversation a rerun of the step sees. Reset at every
    launch, never carried over from the last run in this slot."""
    task: asyncio.Task[None] | None = None
    """The live run's own task, whichever entry started it: the Run button's background launch,
    or the agent's `run_workflow` starting it inside a chat turn."""
    settlement: asyncio.Task[str] | None = None
    """The chat entry's settling task, answering the outcome sentence the settled turn carries back
    into the conversation. It awaits `task`, then writes the evidence, the archive record and the
    session's result line. `None` for a run a button or a schedule started, whose own `run_graph`
    settles it inline. Await it to observe the run settled. The run's ending never depends on
    anyone awaiting it."""
    stopping: bool = False
    """Set by `service.stop` before it cancels `task`, so the handler that catches the cancellation
    can tell a stop from a pause. Reset to `False` at every launch, never carried over from a
    previous run in this slot."""
    paused_by_restart: bool = False
    """A run a restart left in this session's run log for the person to resume. Nothing runs until they do."""
    workflow_id: str = ""
    run_title: str = ""
    """What this run is called, as whoever started it named it. Separate from the session's own
    title: one session may host several runs, and each is named for the work it was started to do.
    `""` for a run nobody named, which every reader shows as it sees fit rather than filling in."""
    run_id: str | None = None
    """This run's own id, minted at launch and shared by its evidence and its archive record.
    `None` until the first launch into this slot mints one."""
    result: str = ""
    progress: LiveProgress = field(default_factory=LiveProgress)
    values: dict[str, object] = field(default_factory=dict)
    """What each node produced, by node id: the run log's own state. The inspector reads a node's
    output here while the run goes and after it ends. Replaced with a new dict at each launch,
    never cleared in place. The graph writes into it as steps finish."""
    started_at: str = ""
    """This run's UTC start, stamped by whichever entry launched it. The archive record carries it
    beside the moment the run ended."""
    auto_gates: Callable[[], tuple[str, ...]] = field(default=_no_auto_gates)
    """A live callback answering the gate ids automode passed in this run, bound at launch by
    whichever entry started it. Every writer that ends the run reads the passed gates here, so a gate
    automode approved never gets evidence or an archive entry as one a person approved."""
    orchestrated_gates: Callable[[], tuple[str, ...]] = field(default=_no_orchestrated_gates)
    """A live callback answering the person gates this run handed to its orchestrator under
    `gate_mode="orchestrate"`, bound at launch like `auto_gates`. A declared orchestrator gate is not
    among them. Each writer that ends the run reads those from the workflow and this run's values."""

    def paused_run_refusal(self) -> str | None:
        """The sentence that refuses a new run while this run is paused, or `None` when a new run may start.

        A run a person paused or a restart paused keeps its run log for a resume, and a new run would overwrite it.

        Returns:
            The refusal, or `None` when this run is not paused.
        """
        task = self.task
        if self.pending is not None or not (self.paused_by_restart or (task is not None and task.cancelled())):
            return None
        return f"{self.workflow_id} is paused in this session. The person must resume or stop it before another run starts."


ACTIVE_RUN: ContextVar[ActiveRun | None] = ContextVar("harness_active_run", default=None)
"""Which `ActiveRun` the task now executing is running for, when one is bound.

A step closure reads `ACTIVE_RUN.get() or active`, where `active` is the run it captured at
composition. The bound run wins, and an unbound call, such as a test's or the process's own
session's, falls back to the captured one. `bind_active` sets it."""


@contextmanager
def bind_active(active: ActiveRun) -> Generator[None]:
    """Scope `active` as `ACTIVE_RUN` for as long as this block runs.

    Call this from inside the coroutine that executes the run. Bound around `asyncio.create_task(...)`
    instead, it leaves the task unbound, because a task copies its creator's context when it is created.

    Args:
        active: The run the block works for.

    Yields:
        Control to the block, with `active` bound.
    """
    token = ACTIVE_RUN.set(active)
    try:
        yield
    finally:
        ACTIVE_RUN.reset(token)


ACTIVE_WORKSPACE_ROOT: ContextVar[Path | None] = ContextVar("harness_active_workspace_root", default=None)
"""Which session's project the turn now executing belongs to, when one is bound.

A tool built as a plain closure, with no `RunContext` to read `Runtime.workspace` off, reads the
session's project root here. `bind_workspace_root` sets it for the turn. Unset, the default, means
no session's turn is bound, which a reader treats the same as a session with no workspace of its own."""


@contextmanager
def bind_workspace_root(root: Path | None) -> Generator[None]:
    """Scope `root` as `ACTIVE_WORKSPACE_ROOT` for as long as this block runs.

    Wrap the model and tool call work a turn does, not only the building of a lazily drawn result.
    A streamed turn's tokens are drawn after the route handler returns, in whatever task draws them,
    so the binding must wrap that draw for the tool calls inside it to see it.

    Args:
        root: The session's own `Runtime.workspace.root`, or `None` for a session with no workspace,
            such as the harness checkout's own.

    Yields:
        Control to the block, with `root` bound.
    """
    token = ACTIVE_WORKSPACE_ROOT.set(root)
    try:
        yield
    finally:
        ACTIVE_WORKSPACE_ROOT.reset(token)


ANSWERABLE: ContextVar[bool] = ContextVar("harness_answerable", default=False)
"""Whether the turn now running has someone who can answer an approval.

A tool that waits for the person is offered to the model only while this is true. Plugins with an
approval surface (the session-scoped chat stream, the ACP entry) run their turn inside
`answerable`. A turn nobody attends runs inside `not_answerable`. The default is False, so a
surface that says nothing counts as one that cannot answer."""


@contextmanager
def _marked(*, answerable_now: bool) -> Generator[None]:
    """Hold `ANSWERABLE` at `answerable_now` for the block, then put back whatever it held before."""
    previous = ANSWERABLE.get()
    ANSWERABLE.set(answerable_now)
    try:
        yield
    finally:
        # A token reset fails here: an async generator's block can be finalized in a copy of the context it started in.
        ANSWERABLE.set(previous)


def answerable() -> AbstractContextManager[None]:
    """Run this block as a turn whose deferred end has someone to answer it.

    Inside the block the verbs that wait for the person are offered to the model.

    Returns:
        A context manager holding `ANSWERABLE` true for the block.
    """
    return _marked(answerable_now=True)


def not_answerable() -> AbstractContextManager[None]:
    """Run this block as a turn nobody attends, whatever the surface around it said.

    Inside the block the verbs that wait for the person are not offered at all, so a turn whose
    result nobody reads cannot ask for what nobody would answer.

    Returns:
        A context manager holding `ANSWERABLE` false for the block.
    """
    return _marked(answerable_now=False)


@dataclass(frozen=True)
class RunIdentity:
    """Which run a step is performing for, wherever it executes."""

    run_id: str
    workflow_id: str


def run_identity(active: ActiveRun) -> RunIdentity | None:
    """The run a step is writing for: a Temporal activity's own ids first, else the bound in-process run's.

    Args:
        active: The in-process run to fall back on.

    Returns:
        The run's identity, or `None` when neither names a run.
    """
    inside = activity_run()
    if inside is not None:
        return RunIdentity(run_id=inside[0], workflow_id=inside[1])
    if active.run_id is not None:
        return RunIdentity(run_id=active.run_id, workflow_id=active.workflow_id)
    return None


def current_run_id() -> str | None:
    """The id of the run the code now executing works for: a Temporal activity's own run first, else the run `ACTIVE_RUN` binds.

    Returns:
        The run's id, or `None` when no run is bound, such as during a chat turn.
    """
    inside = activity_run()
    if inside is not None:
        return inside[0]
    active = ACTIVE_RUN.get()
    return active.run_id if active is not None else None


def _run_is_live(active: ActiveRun) -> bool:
    """Whether `active`'s run is running, waiting at a gate, or paused by a person or a restart, none of which holds a turn lock."""
    task = active.task
    return (task is not None and (not task.done() or task.cancelled())) or active.pending is not None or active.paused_by_restart


@dataclass
class Runtime:
    """One conversation and one run, grouped by which slice owns which half."""

    registry: StepRegistry
    """What this run can execute: steps registered here, plus whether it can perform a briefing.

    A workflow may name the registered ones and may bring more of its own."""
    library: WorkflowLibrary
    """The workflow library, held as the protocol. The composition root picks the backend."""
    agent: Agent[Runtime, str | DeferredToolRequests]
    """The one agent this app has, built by `build_agent` with the workflow process as a capability.

    Every entry gets the same shape: plain prose, or a turn ending deferred with the browser's
    verbs or an approval outstanding, which the chat entries settle on the next request. A consumer
    narrows with `isinstance` to tell the two apart, never to tell plugins apart."""
    conversation: Conversation = field(default_factory=Conversation)
    active: ActiveRun = field(default_factory=ActiveRun)
    gate_asker: GateAsker | None = None
    """The plugin's own way of asking a gate question, set by a plugin that has one.

    `None` is the browser: a gate waits on `active` and `/api/decide` answers it. A plugin sets it
    after composition, as the ACP entry does once the editor's session is live."""
    custom_capabilities: CapabilityStore = field(default_factory=_default_custom_capabilities)
    """Where the agent's own custom capabilities live. A chat turn passes `load_active()` from here
    to `agent.run(capabilities=...)`, so an owner's `harness activate` takes effect on the very next
    turn. A caller may swap in a store rooted elsewhere.

    Defaults to the same root `build_agent` points its capability creation at, so a session built
    with no override still reads what the agent itself wrote."""
    session_id: str | None = None
    """Which session the live slot belongs to, `None` until a turn mints one or resume restores
    it. Set by `features.authoring.service.ensure_session_id`, cleared by Forget."""
    session_log: SessionEvents = field(kw_only=True)
    """Where this session's events get appended. A capability's tool closure records through
    `ctx.deps.session_log`, so a workspace session's own file and this deployment's own are
    interchangeable to the caller.

    Required, with no default. Whoever composes the session says where its events go,
    `NoSessionLog` included."""
    store: SessionTranscript = field(default_factory=InMemoryStepStore)
    """This session's transcript: the one record every session in this deployment writes through.

    Which runs are this session's is the conversation id its turns run under, never where the
    record lives. The in-memory default serves a session nobody wired a record to, whose history
    then dies with the process."""
    state: StateSettings = field(kw_only=True)
    """Where this session's own unscoped files live, the kept draft and the waiting run, for a
    session that opened against no project. A pooled session names both files outright instead.

    Required, with no default. Whoever composes the session says where they go."""
    workspace: Workspace | None = None
    """The project this session opened against, or `None` for the harness checkout's own home.
    A workspace session's composition root points `session_log` at `workspace.session_log`. A
    session with no project records into whatever log it was composed with."""
    draft_file: Path | None = None
    """This session's own kept proposal, under its state home's `sessions/` directory. `None` is
    the checkout's single `draft.json`, which the process's own session keeps: every unscoped
    route reads and writes that one file, so it cannot move under them."""
    waiting_run_file: Path | None = None
    """This session's own waiting workflow run, under its state home's `sessions/` directory. `None`
    is the checkout's single `run-log.json`, which the process's own session keeps: the unscoped
    run entries and the boot-time restore read that one file, so it cannot move under them."""
    context_usage: ContextUsage | None = None
    """The latest context reading from the capability `build_agent` mounts, `None` before the first
    model request. The capability's event listener writes it. `SessionPool.context_of` reads it only
    while a turn is in flight here, because outside a turn it is one request behind."""
    session_kind: str = "chat"
    """What this session is for: the value `SessionStarted` recorded. `"master"` is AI Factory
    Studio's follow-me companion."""
    design_only: bool = False
    """Whether the turn in flight asked for a design and nothing else.

    Set by the entry that admits the turn, before anything runs, and read by a capability deciding
    what to mount. On such a turn a surface that starts work is not offered at all. It holds for one
    turn only, so each turn's entry sets it."""
    steer_inbox: list[str] = field(default_factory=list)
    """Messages the person sent while a turn was in flight, waiting for that turn to pick them
    up. The steer route appends. The steering capability drains it into the run's own
    pending-message queue. A turn clears leftovers when it starts, so a message a dead turn
    never read cannot surface inside a later, unrelated one."""
    live_prompts: list[str] = field(default_factory=list)
    """What the person has said into the turn still running: the message that opened it, then
    every steer. The settled projection cannot know these until the turn ends, so the transcript
    route overlays them while the turn's hold is live, and a reload mid-turn still shows the
    person their own words. Each turn start replaces the list."""

    # Nothing here renders this environment for a prompt. The registry is held only so the machinery
    # can resolve names against it.


class SessionRefresh(Protocol):
    """How one session's projected rows are brought up to date after a settle.

    The pool takes it, the container serves it, and a slice's session projection implements it.
    """

    async def __call__(self, runtime: Runtime) -> None:
        """Bring one session's projected rows up to date.

        Args:
            runtime: The session whose rows are refreshed.
        """
        ...

type DecideGateFactory = Callable[[Runtime], "OrchestratorDecider"]
"""How one runtime's own orchestrator decider is built. The pool takes it, and a slice provides it."""


@dataclass(frozen=True)
class PoolComposition:
    """What the process lends every session the pool composes, gathered as one thing the pool is handed.

    Every part optional: a process that records nowhere, decides no gate of its own and announces
    on no channel composes its sessions just the same.
    """

    refresh: SessionRefresh | None = None
    """How a settled turn narrates itself."""
    decide_gate: DecideGateFactory | None = None
    """How a pooled runtime's own orchestrator decider is built."""
    topics: TopicSink | None = None
    """Where a project workspace this pool opens announces what changed. `None` for a process with
    no change channel behind it."""
    tiers: ModelTiers | None = None
    """How the orchestrator tier's model is resolved, whose window a session's context reading is
    measured against. `None` leaves every reading on the default window."""


class PooledSessions(Protocol):
    """Where this process holds its sessions, asked for when something needs it.

    Ask for the pool when a tool runs, never when the tool is built. The pool is built over the
    process's own session, which holds the agent that hosts those tools.
    """

    async def pool(self) -> SessionPool:
        """The pool this process is holding its sessions in.

        Returns:
            The pool.
        """
        ...


_BEHIND: set[asyncio.Task[None]] = set()
"""Live write-behind tasks, held so the event loop is not their only reference."""


def project_root(runtime: Runtime) -> Path:
    """The checkout this session belongs to: its project's root, or the harness's own.

    Args:
        runtime: The session.

    Returns:
        The checkout root.
    """
    return CHECKOUT if runtime.workspace is None else runtime.workspace.root


def workspace_root(runtime: Runtime) -> Path | None:
    """This session's own project root, or `None` for a session with no workspace of its own.

    A turn binds it as `ACTIVE_WORKSPACE_ROOT` around its model and tool call work. Unlike
    `project_root`, a session with no workspace answers `None` here, never the harness checkout's root.

    Args:
        runtime: The session.

    Returns:
        The project root, or `None`.
    """
    return None if runtime.workspace is None else runtime.workspace.root


def timeline(runtime: Runtime) -> tuple[Recorded, ...]:
    """Every readable event in this session's own session log file, in order.

    Args:
        runtime: The session.

    Returns:
        The recorded events, in the order they were recorded.
    """
    return runtime.session_log.events() if runtime.workspace is None else runtime.workspace.session_log.events()


def draft_path(runtime: Runtime) -> Path:
    """Where `runtime`'s kept proposal lives: its own file, or its home's standing one.

    Args:
        runtime: The session.

    Returns:
        The draft file's path.
    """
    return runtime.draft_file if runtime.draft_file is not None else runtime.state.draft


def run_log_path(runtime: Runtime) -> Path:
    """Where `runtime`'s waiting workflow run lives: its own file, or its home's standing one.

    A pooled session waits under its own id, so two sessions never overwrite each other's waiting
    run. A session with no file of its own keeps the single path under its home, which a restart
    restores from. The file's folder is created when missing.

    Args:
        runtime: The session.

    Returns:
        The waiting run's file path.
    """
    if runtime.waiting_run_file is not None:
        runtime.waiting_run_file.parent.mkdir(parents=True, exist_ok=True)
        return runtime.waiting_run_file
    project = runtime.workspace
    home = project.state if project is not None else runtime.state
    home.home.mkdir(parents=True, exist_ok=True)
    return home.waiting_run(project=project is not None)


@dataclass
class SessionConversationSearch(ConversationSearch[Runtime]):
    """Recall reading, per run, the conversation this turn belongs to.

    A search answers out of the asking session's own history, narrowed by conversation id, and never
    out of another's.
    """

    async def for_run(self, ctx: RunContext[Runtime]) -> AbstractCapability[Runtime]:
        """Point the search's source at the conversation this turn belongs to.

        Args:
            ctx: The run context of the turn.

        Returns:
            A copy of this capability reading that conversation from the session's transcript.
        """
        return replace(self, source=RecordHistorySource(ctx.deps.store, conversation_id=ctx.conversation_id))


@dataclass
class SessionContextUsage(ReportContextUsage[Runtime]):
    """The context gauge, whose every reading lands on the runtime of the session whose turn it measured."""

    tiers: ModelTiers | None = field(kw_only=True, default=None)
    """How the orchestrator tier's model is resolved, whose window the gauge reads. `None` leaves
    the base reading, for a session built with no model factory to reach."""

    @on_event(ContextUsageEvent)
    async def _stash_reading(self, ctx: RunContext[Runtime], event: ContextUsageEvent) -> None:
        """Keep the reading on the runtime of the session whose turn it measured."""
        ctx.deps.context_usage = ContextUsage(used_tokens=event.used_tokens, window_tokens=event.window_tokens, resolved=event.resolved)

    def _measure(self, ctx: RunContext[Runtime], request_context: ModelRequestContext) -> ContextUsage:
        """The base reading, its window taken from the orchestrator tier's model when that window is known."""
        reading = super()._measure(ctx, request_context)
        window = None if self.tiers is None else tier_context_window(self.tiers, "orchestrator")
        if window is None:
            return reading
        return ContextUsage(used_tokens=reading.used_tokens, window_tokens=window, resolved=True)


def _history_context_usage(history: Sequence[ModelMessage], tiers: ModelTiers | None) -> ContextUsage:
    """A reading for `history` against the model the next turn would run on."""
    used = estimate_context_tokens(history, model_request_parameters=None)
    window = None if tiers is None else tier_context_window(tiers, "orchestrator")
    return ContextUsage(
        used_tokens=used, window_tokens=window if window is not None else DEFAULT_CONTEXT_WINDOW, resolved=window is not None
    )


async def resume(
    session: Runtime,
    store: StepStore,
    *,
    run_id: str | None = None,
    run_ids: Sequence[str] = (),
    forgotten_after: datetime | None = None,
) -> str | None:
    """Reload the last conversation into `session`, and say which run it came from.

    A missing directory, a first run, or an unreadable or outdated snapshot never raises. The
    conversation starts empty instead. The pooled counterpart is `continue_session`, which reads one
    session's own runs.

    Args:
        session: The runtime to reload into.
        store: The transcript to read.
        run_id: The run the session log remembers as live. Tried first, whatever `forgotten_after` says.
        run_ids: Every run this home's own session log credits to it. When `run_id` is absent or unreadable,
            the newest of these started after `forgotten_after` is reloaded. Nothing outside them is read,
            and with none given nothing is reloaded.
        forgotten_after: The Forget watermark, read off the session log by the composition root.

    Returns:
        The id of the run the conversation came from, or `None` when nothing was reloaded.
    """
    if run_id is not None:
        messages = await _continuable(store, run_id)
        if messages is not None:
            restore_conversation(session.conversation, messages, session.registry, draft_file=draft_path(session))
            return run_id
    known = frozenset(run_ids)
    if not known:
        return None
    try:
        runs = [record for record in await store.list_runs() if record.run_id in known]
    except Exception:
        _logger.warning("could not read the record, starting with an empty conversation")
        return None
    for record in resumable(runs, forgotten_at=forgotten_after):
        messages = await _continuable(store, record.run_id)
        if messages:
            restore_conversation(session.conversation, messages, session.registry, draft_file=draft_path(session))
            return record.run_id
    return None


async def continue_session(runtime: Runtime, *, run_ids: Sequence[str]) -> str | None:
    """Reload the newest readable run among this session's own, and say which one it was.

    The session-scoped counterpart to `resume`. Nothing outside `run_ids` is read, so a pooled
    session never inherits another's conversation.

    Args:
        runtime: The session to reload into.
        run_ids: The runs the session log credits to this session, oldest first.

    Returns:
        The id of the run reloaded, or `None` when none was readable.
    """
    store = runtime.store
    for run_id in reversed(tuple(run_ids)):
        messages = await _continuable(store, run_id)
        if messages is not None:
            restore_conversation(runtime.conversation, messages, runtime.registry, draft_file=draft_path(runtime))
            return run_id
    return None


async def _continuable(store: StepStore, run_id: str) -> list[ModelMessage] | None:
    """`run_id`'s messages, interrupted snapshots included, with open calls closed as cut off, or `None` when none are readable."""
    try:
        messages = await continue_run(store, run_id=run_id, include_interrupted=True)
    except Exception:
        return None
    return rescue_history(messages, ended=TURN_CUT_OFF) if messages else None


def restore_conversation(
    conversation: Conversation, messages: Sequence[ModelMessage], registry: StepRegistry, *, draft_file: Path
) -> None:
    """Rebuild a whole conversation from its messages: the dialogue, the draft, and the verdict.

    The draft is checked again against `registry`, so the verdict matches the environment as it is
    now. The draft comes from the kept file only, never from the transcript. No file kept means no draft.

    Args:
        conversation: The conversation to overwrite.
        messages: The dialogue to restore.
        registry: The steps the draft is checked against.
        draft_file: The session's own kept proposal.
    """
    conversation.history = list(messages)
    draft = remembered_draft(path=draft_file)
    if draft is None:
        return
    conversation.draft = draft
    spec, issues = verdict_on(draft.spec_yaml, registry)
    conversation.last_workflow = spec if not issues else None
    conversation.last_issues = tuple(issues)


TURN_FAILED = "the turn ended here ({error})."
"""`rescue_history`'s `ended` for a turn that raised: fill `{error}` with the plain one-line
reason, the same text the session log's `TurnFailed.error` keeps."""

TURN_CUT_OFF = "the turn was cut off here."
"""`rescue_history`'s `ended` for a turn nobody stayed to finish: the client hung up, or the
owner started the next message before this one settled. Boot rescue always uses this one."""


def rescue_history(messages: Sequence[ModelMessage], *, ended: str) -> list[ModelMessage]:
    """`messages` with every tool call a failed or cut-off turn left open closed and explained.

    A call is open when no later `ToolReturnPart` or tool-bound `RetryPromptPart` answers its
    `tool_call_id`, the pairing rule `is_provider_valid` checks. Each open call gets one
    `ToolReturnPart`, in call order, saying it went unanswered, why, and that the owner's next words
    come first. The returns go in one appended `ModelRequest`, or join the interrupted request
    `messages` ends on, which is then marked complete.

    Args:
        messages: The history to close.
        ended: Why the calls went unanswered, `TURN_FAILED` filled in or `TURN_CUT_OFF`.

    Returns:
        A new list, holding the same messages when nothing was open.
    """
    open_calls: dict[str, ToolCallPart] = {}
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    open_calls[part.tool_call_id] = part
        else:
            for part in message.parts:
                if isinstance(part, ToolReturnPart) or (isinstance(part, RetryPromptPart) and part.tool_name is not None):
                    open_calls.pop(part.tool_call_id, None)
    if not open_calls:
        return list(messages)
    returns = [
        ToolReturnPart(
            tool_name=call.tool_name,
            content=f"not answered, {ended} The owner's next message follows, answer it before calling this again.",
            tool_call_id=call.tool_call_id,
            outcome="interrupted",
        )
        for call in open_calls.values()
    ]
    last = messages[-1] if messages else None
    if isinstance(last, ModelRequest) and last.state == "interrupted":
        return [*messages[:-1], replace(last, parts=[*last.parts, *returns], state="complete")]
    return [*messages, ModelRequest(parts=returns)]


def verdict_on(spec_yaml: str, registry: StepRegistry) -> tuple[Workflow | None, list[str]]:
    """This run's verdict on one draft: the parsed workflow if it is one, and every finding at once.

    The panel and boot both call it, so the screen and the process never disagree. An empty draft
    has no finding.

    Args:
        spec_yaml: The draft's YAML.
        registry: The steps the workflow is validated against.

    Returns:
        The parsed workflow, `None` when the draft is empty or does not parse, and every finding. A draft that does not parse gives one finding that can run over several lines.
    """
    if not spec_yaml.strip():
        return None, []
    try:
        spec = Workflow.model_validate(yaml.safe_load(spec_yaml))
    except (ValidationError, yaml.YAMLError) as exc:
        return None, [str(exc)[:900]]
    report = validate(spec, registry)
    return spec, [f"[{issue.kind}] {issue.where}: {issue.detail}" for issue in report.issues]


def resumable(records: Sequence[RunRecord], *, forgotten_at: datetime | None) -> list[RunRecord]:
    """Newest first, minus everything from before the forget watermark.

    Forgetting is a watermark, not an erasure. The log stays append-only.

    Args:
        records: The runs to filter.
        forgotten_at: The Forget watermark, or `None` to keep every run.

    Returns:
        The runs started after the watermark, newest first.
    """
    kept = [record for record in records if forgotten_at is None or record.started_at > forgotten_at]
    return sorted(kept, key=lambda record: record.started_at, reverse=True)


def hydration_facts(events: Sequence[Recorded], session_id: str) -> tuple[str, tuple[str, ...]]:
    """This session's `session_kind`, and the run ids a hydration may resume: everything since its last forget.

    The `session_kind` comes from the whole timeline, because a forget clears a conversation, never
    what the session is for. The run ids honour the forget watermark, so a hydrated runtime never
    resumes a forgotten conversation.

    Args:
        events: The session log's events.
        session_id: The session.

    Returns:
        The session's `session_kind`, `"chat"` when no span names one, and its resumable run ids in log order.
    """
    spans = [span for span in session_spans(events) if span.session_id == session_id]
    forgotten_at = max(
        (record.at for record in events if isinstance(record.event, ConversationForgotten) and record.event.session_id == session_id),
        default=None,
    )
    kept = spans if forgotten_at is None else [span for span in spans if span.ended_at is None or span.ended_at > forgotten_at]
    kind = next((span.session_kind for span in spans), "chat")
    return kind, tuple(run_id for span in kept for run_id in span.run_ids)


def remember_draft(draft: Draft, *, path: Path) -> None:
    """Keep the settled proposal across a restart, in `path`.

    Args:
        draft: The settled proposal.
        path: The file to write, its folder created when missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft.model_dump_json(), encoding="utf-8")


def remembered_draft(*, path: Path) -> Draft | None:
    """The proposal a previous process settled on at `path`, or None if there is none to read.

    An older `Draft` shape or a half-written file answers `None`, never an error.

    Args:
        path: The kept draft file.

    Returns:
        The kept proposal, or `None`.
    """
    try:
        return Draft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


async def newest_run_id(runtime: Runtime, *, conversation_id: str) -> str | None:
    """The run this session's just-settled turn wrote, read from that session's own transcript.

    A failed listing, or a run the store never registered under this id, answers `None` and never
    fails the settle.

    Args:
        runtime: The session whose transcript is read.
        conversation_id: The id the turn ran under, which every chat entry sets to the session's id. Two turns
            overlapping in two sessions never stamp each other's run.

    Returns:
        The newest run's id, or `None`.
    """
    try:
        runs = await runtime.store.list_runs(conversation_id=conversation_id)
    except Exception:
        _logger.warning("could not read the transcript. The settled turn records no run id")
        return None
    return max(runs, key=lambda record: record.started_at).run_id if runs else None


def refresh_projection(runtime: Runtime, refresh: SessionRefresh | None) -> None:
    """Schedule this session's projection refresh behind the turn, and never wait for it.

    The answer the caller is about to send depends on nothing the refresh writes. Nothing is
    scheduled when `refresh` is `None`, or when there is no running loop to schedule on.

    Args:
        runtime: The session to refresh.
        refresh: The refresh to run, or `None`.
    """
    if refresh is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover (every caller settles inside the server's loop)
        return
    task = loop.create_task(refresh(runtime))
    _BEHIND.add(task)
    task.add_done_callback(_BEHIND.discard)


class SessionBusyError(RuntimeError):
    """A turn is already in flight in this session, so a second one is refused rather than queued."""

    def __init__(self, session_id: str) -> None:
        """Carry the busy session's id.

        Args:
            session_id: The session a turn is already in flight in.
        """
        super().__init__(f"session {session_id!r} is mid-turn")
        self.session_id = session_id


class MissingProjectRootError(RuntimeError):
    """A session names a project root this machine cannot open, so the session cannot be answered.

    Carries the root so a plugin can say which one. Nothing falls back to another state home when it
    is raised, so no turn is answered into a timeline that is not its own.
    """

    def __init__(self, root: Path) -> None:
        """Carry the root that cannot be opened.

        Args:
            root: The project root.
        """
        super().__init__(f"project root {str(root)!r} is not available here")
        self.root = root


class ProjectIndex(Protocol):
    """What the pool knows about projects and sessions it did not open itself.

    A pool built without one holds only what this process opened. A session from an earlier run of
    the server is answerable only through an index.
    """

    def id_of(self, root: Path) -> str:
        """The project id `root` belongs to.

        Args:
            root: A project's checkout root.

        Returns:
            The project's id.
        """
        ...

    async def root_of(self, project_id: str) -> Path | None:
        """`project_id`'s root, or `None` when no project has that id.

        Args:
            project_id: The project.

        Returns:
            The project's checkout root, or `None`.
        """
        ...

    async def root_of_session(self, session_id: str) -> Path | None:
        """The project root `session_id` was opened against, or `None` when nothing recorded one.

        Args:
            session_id: The session.

        Returns:
            The project's checkout root, or `None`.
        """
        ...


@dataclass
class _Slot:
    """One runtime the pool is holding: the turn over it, and when it was last handed out."""

    runtime: Runtime
    touched: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    hold: TurnHold | None = None
    """The turn holding this slot right now, `None` when the session is idle."""
    turn_task: asyncio.Task[object] | None = None
    """The task doing that turn's work, so an interrupt has something to cancel."""


class TurnHold:
    """One turn's exclusive hold on a session, released exactly once however the turn ends.

    A streamed turn outlives the request handler that started it: the handler takes the hold and the
    response's own teardown gives it back.
    """

    def __init__(self, slot: _Slot) -> None:
        """Hold `slot` for one turn.

        Args:
            slot: The pool's slot for the session, its lock already taken.
        """
        self._slot = slot
        self._released = False

    @property
    def runtime(self) -> Runtime:
        """The session this turn answers into."""
        return self._slot.runtime

    def driven_by(self, task: asyncio.Task[object] | None) -> None:
        """Name the task doing this turn's work, so an interrupt reaches the work and not the wrapper.

        Call it from whatever drives the turn. For a streamed turn that is the task iterating the
        response body, not the one that returned the response.

        Args:
            task: The task doing the work, or `None` to name none.
        """
        self._slot.turn_task = task

    def cancel(self) -> bool:
        """Cancel the work this turn is doing, and say whether there was any to cancel.

        Returns:
            True when a running task was cancelled.
        """
        task = self._slot.turn_task
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def release(self) -> None:
        """Give the session back. Idempotent, because a turn's teardown paths overlap."""
        if self._released:
            return
        self._released = True
        self._slot.turn_task = None
        self._slot.hold = None
        self._slot.lock.release()


class SessionPool:
    """Every session this process holds open, over the one agent it built.

    A pooled session owns its conversation, its draft, its active run, its session log and its
    workspace, and nothing else. The agent, the registry of steps, the library and the
    custom-capability store are the process's, built once and shared by every runtime here.

    `default` is the process's own session, what every unscoped entry acts on. It is never evicted
    and never rebuilt.
    """

    def __init__(
        self,
        default: Runtime,
        *,
        clock: Callable[[], float] = time.monotonic,
        projects: ProjectIndex | None = None,
        composition: PoolComposition | None = None,
    ) -> None:
        """Build a pool around the process's own session.

        Args:
            default: The process's own session, what every unscoped entry acts on.
            clock: The clock idle times are measured with, in seconds.
            projects: The index of projects and sessions this process did not open, or `None` to hold only what it opens.
            composition: What the process lends every session the pool composes. `None` lends nothing.
        """
        self._default = _Slot(runtime=default, touched=clock())
        self._slots: dict[str, _Slot] = {}
        self._homes: dict[str, Workspace | None] = {}
        """Which state home each session id was opened against, kept past eviction so a dropped
        runtime is rebuilt from the same session log and transcript."""
        self._clock = clock
        self._projects = projects
        self._composition = composition if composition is not None else PoolComposition()
        self._hydrating: dict[str, asyncio.Lock] = {}
        """One hydration lock per session id, so two requests arriving together on a session nobody
        holds yet end up on one runtime, while a request for any other session waits for nothing."""

    @property
    def default(self) -> Runtime:
        """The process's own session."""
        return self._default.runtime

    async def get(self, session_id: str) -> Runtime:
        """The runtime for `session_id`, hydrated from its stored record when it is not in memory.

        Args:
            session_id: The session.

        Returns:
            The session's runtime.

        Raises:
            KeyError: No session on that id's own timeline, or the timeline records it as deleted. Either is a caller's 404.
            MissingProjectRootError: The session's recorded project root is gone from this machine. The session is refused,
                never answered out of the checkout's timeline.
        """
        slot = self._slot(session_id)
        if slot is not None:
            slot.touched = self._clock()
            return slot.runtime
        async with self._hydrating.setdefault(session_id, asyncio.Lock()):
            # Asked again inside the gate: whoever held it may have hydrated this very session.
            held = self._slot(session_id)
            if held is not None:
                held.touched = self._clock()
                return held.runtime
            return await self._hydrate(session_id)

    async def create(self, project_root: Path | None, *, session_kind: str = "chat") -> Runtime:
        """Open a fresh session on `project_root`'s state home, or the harness checkout's.

        It mints the id and records `SessionStarted` through the new runtime's own session log, so
        the session is on its timeline before its first turn.

        Args:
            project_root: The project to open against, or `None` for the harness checkout.
            session_kind: What the session is for, recorded on `SessionStarted`. `"chat"` for the authoring
                conversation, `"runner"` for one opened to drive a workflow unattended.

        Returns:
            The new session's runtime.

        Raises:
            MissingProjectRootError: `project_root` is not a directory here, or its state home cannot be opened.
        """
        workspace = self._home_for(project_root)
        session_id = str(uuid4())
        runtime = self._runtime_for(session_id, workspace, session_kind=session_kind)
        runtime.session_log.record(SessionStarted(session_id=session_id, session_kind=session_kind))
        self._keep(session_id, runtime, workspace)
        return runtime

    async def begin_turn(self, session_id: str) -> TurnHold:
        """Hold `session_id` for the turn about to run, hydrating it when it is not in memory.

        Args:
            session_id: The session.

        Returns:
            The turn's hold on the session.

        Raises:
            KeyError: No session on that id's own timeline, or the timeline records it as deleted. Either is a caller's 404.
            MissingProjectRootError: The session's recorded project root is gone from this machine. The session is refused,
                never answered out of the checkout's timeline.
            SessionBusyError: A turn is already in flight there, which is a caller's 409.
        """
        await self.get(session_id)
        slot = self._slot(session_id)
        if slot is None:  # pragma: no cover (`get` either held the slot or raised)
            msg = f"session {session_id!r} was not held after hydration"
            raise KeyError(msg)
        return await self._begin(slot)

    async def begin_default_turn(self) -> TurnHold:
        """Hold the process's own session for the turn about to run.

        Returns:
            The turn's hold on the session.

        Raises:
            SessionBusyError: A turn is already in flight in it, scoped or unscoped. Both entries read this one lock,
                so two turns never answer into one conversation.
        """
        return await self._begin(self._default)

    def interrupt(self, session_id: str) -> bool:
        """Cancel the turn in flight in `session_id`, and say whether there was one to cancel.

        The work is cancelled first, then the session is handed back without waiting for the turn to
        unwind, so the next message into it is admitted. The cancelled turn's own teardown releases
        again, which does nothing, and records the abandonment on the timeline.

        Args:
            session_id: The session.

        Returns:
            True when a turn was in flight.
        """
        slot = self._slot(session_id)
        hold = slot.hold if slot is not None else None
        if hold is None:
            return False
        hold.cancel()
        hold.release()
        return True

    def steering(self, session_id: str) -> Runtime | None:
        """The runtime whose turn is in flight in `session_id`, or `None` when none is.

        The steer route's guard: a steer has to reach a running turn, and only a held slot has
        one. The process's own unscoped session is not steerable by id, same as `interrupt`.

        Args:
            session_id: The session.

        Returns:
            The runtime with a turn in flight, or `None`.
        """
        slot = self._slot(session_id)
        if slot is None or slot.hold is None:
            return None
        return slot.runtime

    def held_sessions(self) -> tuple[Runtime, ...]:
        """Every session this process is holding open, the process's own first.

        Returns:
            The held runtimes.
        """
        return (self._default.runtime, *(slot.runtime for slot in self._slots.values()))

    def project_id_of(self, runtime: Runtime) -> str:
        """Which project's rows this session belongs to, or `""` when no index can name one.

        Args:
            runtime: The session.

        Returns:
            The project's id, or `""`.
        """
        return "" if self._projects is None else self._projects.id_of(project_root(runtime))

    async def root_of_project(self, project_id: str) -> Path | None:
        """`project_id`'s root, or `None` when no index can name one.

        Args:
            project_id: The project.

        Returns:
            The project's checkout root, or `None`.
        """
        return None if self._projects is None else await self._projects.root_of(project_id)

    def is_answering(self, session_id: str) -> bool:
        """Whether a turn is in flight for `session_id`, in which case a chat entry refuses a second.

        False for a session this process is not holding: nobody is mid-turn on a runtime that is
        not in memory, and one hydrated on the next request starts idle.

        Args:
            session_id: The session.

        Returns:
            True while a turn holds the session's lock.
        """
        slot = self._slot(session_id)
        return slot is not None and slot.lock.locked()

    def context_of(self, session_id: str) -> ContextUsage | None:
        """`session_id`'s context reading, `None` for a session this process is not holding.

        Computed fresh from the session's own history against the model the next turn would run on.
        While a turn is in flight, the reading the turn's last request left is answered instead,
        because the history is not settled yet.

        Args:
            session_id: The session.

        Returns:
            The context reading, or `None`.
        """
        slot = self._slot(session_id)
        if slot is None:
            return None
        if self.is_answering(session_id) and slot.runtime.context_usage is not None:
            return slot.runtime.context_usage
        return _history_context_usage(slot.runtime.conversation.history, self._composition.tiers)

    def drop(self, session_id: str) -> bool:
        """Stop holding `session_id`'s runtime, and say whether one was let go.

        Call it for a deleted session, because a runtime still held answers turns into rows and files
        its route has already removed. It refuses the process's own session and a session with a
        turn in flight.

        Args:
            session_id: The session.

        Returns:
            True when the runtime was let go.
        """
        if session_id == self._default.runtime.session_id:
            return False
        slot = self._slots.get(session_id)
        if slot is None or slot.lock.locked():
            return False
        del self._slots[session_id]
        self._homes.pop(session_id, None)
        return True

    def evict_idle(self, *, older_than_minutes: int = 30) -> None:
        """Drop every pooled runtime nobody has touched for `older_than_minutes`.

        A dropped runtime costs its next caller one hydration from the transcript its session log
        names. It keeps the process's own session, any runtime holding its turn lock, and any whose
        run is still live, waiting at a gate or paused, even one the Run button launched with no turn lock.

        Args:
            older_than_minutes: How long a runtime must sit untouched before it is dropped.
        """
        cutoff = self._clock() - older_than_minutes * 60
        for session_id, slot in list(self._slots.items()):
            if slot.touched <= cutoff and not slot.lock.locked() and not _run_is_live(slot.runtime.active):
                del self._slots[session_id]

    async def _begin(self, slot: _Slot) -> TurnHold:
        """Take `slot`'s turn lock, or raise `SessionBusyError` at once when it is held, never waiting."""
        if slot.lock.locked():
            raise SessionBusyError(slot.runtime.session_id or "")
        # No await between the check and the take: acquiring a free lock never suspends.
        await slot.lock.acquire()
        hold = TurnHold(slot)
        slot.hold = hold
        slot.turn_task = asyncio.current_task()
        slot.touched = self._clock()
        return hold

    def _slot(self, session_id: str) -> _Slot | None:
        """The held slot for `session_id`, the process's own session included, else `None`."""
        if self._default.runtime.session_id == session_id:
            return self._default
        return self._slots.get(session_id)

    def _home_for(self, root: Path | None) -> Workspace | None:
        """The state home for `root`: a project's own `.aifactory/state`, or `None` for the checkout's standing one."""
        if root is None or root.resolve() == CHECKOUT.resolve():
            return None
        # Opening creates the tree, so without this check a moved or deleted project would come back empty.
        if not root.is_dir():
            raise MissingProjectRootError(root)
        try:
            return open_workspace(root, topics=self._composition.topics)
        except OSError as exc:
            raise MissingProjectRootError(root) from exc

    async def _recorded_home(self, session_id: str) -> Workspace | None:
        """The state home the index remembers for `session_id`, or the checkout's when no index answers."""
        if self._projects is None:
            return None
        try:
            root = await self._projects.root_of_session(session_id)
        except Exception:
            _logger.warning("could not look up the home of session {session_id}", session_id=session_id)
            return None
        return self._home_for(root)

    async def _hydrate(self, session_id: str) -> Runtime:
        """Rebuild `session_id`'s runtime from its own state home and runs, or raise `KeyError` for a missing or deleted session."""
        workspace = self._homes[session_id] if session_id in self._homes else await self._recorded_home(session_id)
        runtime = self._runtime_for(session_id, workspace)
        events = timeline(runtime)
        if any(isinstance(record.event, SessionDeleted) and record.event.session_id == session_id for record in events):
            msg = f"session {session_id!r} was deleted"
            raise KeyError(msg)
        spans = [span for span in session_spans(events) if span.session_id == session_id]
        if not spans:
            msg = f"no session {session_id!r} on the timeline at {project_root(runtime)}"
            raise KeyError(msg)
        kind, run_ids = hydration_facts(events, session_id)
        runtime = self._runtime_for(session_id, workspace, session_kind=kind)
        await continue_session(runtime, run_ids=list(run_ids))
        self._keep(session_id, runtime, workspace)
        return runtime

    def _runtime_for(self, session_id: str, workspace: Workspace | None, *, session_kind: str = "chat") -> Runtime:
        """One session's runtime: its own conversation and state home, the process's own agent."""
        default = self._default.runtime
        home = workspace.state if workspace is not None else default.state
        # A copy, so a gate this session's run reaches is decided over this session, never the default one.
        registry = default.registry.copy() if self._composition.decide_gate is not None else default.registry
        runtime = Runtime(
            registry=registry,
            library=default.library,
            agent=default.agent,
            custom_capabilities=default.custom_capabilities,
            session_id=session_id,
            session_log=workspace.session_log if workspace is not None else default.session_log,
            store=default.store,
            state=home,
            workspace=workspace,
            draft_file=home.sessions / f"{session_id}.draft.json",
            waiting_run_file=home.sessions / f"{session_id}{WAITING_RUN_SUFFIX}",
            session_kind=session_kind,
        )
        if self._composition.decide_gate is not None:
            registry.decide_gate = self._composition.decide_gate(runtime)
        return runtime

    def _keep(self, session_id: str, runtime: Runtime, workspace: Workspace | None) -> None:
        """Keep `runtime` open, and remember which state home to rebuild it from."""
        self._slots[session_id] = _Slot(runtime=runtime, touched=self._clock())
        self._homes[session_id] = workspace
