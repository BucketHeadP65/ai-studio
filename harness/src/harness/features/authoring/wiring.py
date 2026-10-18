"""What the chat entries take, and the type each of those is registered under."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass

from core.ai.history.contracts import RequestParametersSink
from core.cqrs.contracts import TopicSink
from core.settings import InfrastructureSettings
from engram.config import RerankSettings
from wireup import injectable

from harness.adapters.contracts import DatabaseBinding, ModelTiers, Notifier
from harness.composition import harness_session
from harness.domain.contracts import ConversationHistory, SessionEvents, SessionTranscript
from harness.features.authoring.domain import SourceRun
from harness.features.authoring.infrastructure.studio import AsyncClient
from harness.features.authoring.internal_contracts import Chat, EndedRuns
from harness.features.authoring.service import ChatService
from harness.features.running.contracts import DurableTurns, RunArchiveQueries, RunRecords, SpendReporter, orchestrator_decider
from harness.features.scheduling.contracts import Scheduler
from harness.features.sessions.contracts import Sessions
from harness.session import PoolComposition, PooledSessions, ProjectIndex, Runtime, SessionPool, SessionRefresh
from harness.settings import (
    ExternalAgentRegistry,
    KnowledgeBaseGuidanceSettings,
    LibrarySearchSettings,
    RunSettings,
    SelfAuthoringSettings,
    SkillHintSettings,
    StateSettings,
    StoreSettings,
    WebSearchSettings,
)


@dataclass(frozen=True)
class SaveApproval:
    """Whether a save through the library port surfaces as a permission request before it lands.

    Only a host whose protocol can ask the person may require it. On every other host a save is written outright.
    """

    required: bool = False


@injectable
def save_approval() -> SaveApproval:
    """No approval, until a host with a protocol that can ask says otherwise.

    Returns:
        A save approval that requires nothing.
    """
    return SaveApproval()


@injectable
@dataclass(frozen=True)
class SessionCollaborators:
    """Everything this process's own session composes over, gathered as one thing the container builds.

    Each field is one collaborator the session is handed.
    """

    studio_client: AsyncClient
    """This deployment's client on the studio knowledge base API."""
    store: StoreSettings
    """How the library ranks candidates."""
    library_search: LibrarySearchSettings
    """Whether one read here may cross knowledge bases."""
    guidance: KnowledgeBaseGuidanceSettings
    """How to retrieve well from any knowledge base here."""
    web_search: WebSearchSettings
    """Whether `web_search` reaches Tavily or searches keyless."""
    self_authoring: SelfAuthoringSettings
    """Where the owner-reviewed custom items live."""
    run: RunSettings
    """Which engine runs a workflow."""
    state: StateSettings
    """Where this deployment's durable process state is written."""
    records: RunRecords
    """Where a run's own records land."""
    reporter: SpendReporter
    """What a spend question is answered from."""
    session_log: SessionEvents
    """This deployment's own timeline, which the session narrates into."""
    transcript: SessionTranscript
    """This deployment's own transcript, which its runs are written to and resumed from."""
    turns: DurableTurns
    """Where a durable run's multi-turn lines are carried between activities."""
    parameters: RequestParametersSink
    """Where every request's tool definitions and model settings are recorded, beside the messages the transcript already keeps."""
    sessions: PooledSessions
    """The pool this process holds, resolved when an act reaches another session."""
    topics: TopicSink
    """Where a change this session causes is announced."""
    notifier: Notifier
    """How the `notify_owner` step reaches this machine's owner."""
    tiers: ModelTiers
    """How a model tier name becomes a model."""
    binding: DatabaseBinding
    """Which databases this process is about to open."""
    external_agents: ExternalAgentRegistry
    """Which ACP agents this session may spawn, read per call."""
    approval: SaveApproval
    """Whether a save through the library port surfaces as a permission request first."""
    scheduler: Scheduler
    """The scheduler the workflow process's schedule tools go through."""
    infrastructure: InfrastructureSettings
    """Where external systems are reached."""
    rerank: RerankSettings
    """How search hits are re-ranked."""
    skill_hint: SkillHintSettings
    """Whether the chat agent gets the skill hint."""


@injectable
async def session(collaborators: SessionCollaborators) -> AsyncIterator[Runtime]:
    """This process's own session: its library, its registry of steps and the agent over them.

    Args:
        collaborators: Everything the session composes over.

    Yields:
        The session, whose connections close when the container closes it.
    """
    async with AsyncExitStack() as stack:
        yield await harness_session(
            stack,
            studio_client=collaborators.studio_client,
            store=collaborators.store,
            library_search=collaborators.library_search,
            guidance=collaborators.guidance,
            web_search=collaborators.web_search,
            self_authoring=collaborators.self_authoring,
            run=collaborators.run,
            state=collaborators.state,
            records=collaborators.records,
            reporter=collaborators.reporter,
            session_log=collaborators.session_log,
            transcript=collaborators.transcript,
            turns=collaborators.turns,
            parameters=collaborators.parameters,
            sessions=collaborators.sessions,
            topics=collaborators.topics,
            notifier=collaborators.notifier,
            tiers=collaborators.tiers,
            binding=collaborators.binding,
            external_agents=collaborators.external_agents,
            save_requires_approval=collaborators.approval.required,
            scheduler=collaborators.scheduler,
            infrastructure=collaborators.infrastructure,
            rerank=collaborators.rerank,
            skill_hint=collaborators.skill_hint,
        )


@injectable
def pool_composition(refresh: SessionRefresh, sessions: PooledSessions, topics: TopicSink, tiers: ModelTiers) -> PoolComposition:
    """What this process lends every session the pool composes.

    Args:
        refresh: How a settled turn brings its session's projected rows up to date.
        sessions: The pool being built from this composition, which each pooled session's orchestrator reads.
        topics: Where a change a session causes is announced.
        tiers: How a model tier name becomes a model.

    Returns:
        The composition, whose gate decider gives each pooled session its own orchestrator.
    """
    return PoolComposition(
        refresh=refresh, decide_gate=lambda runtime: orchestrator_decider(runtime, sessions), topics=topics, tiers=tiers
    )


@injectable
def session_pool(default: Runtime, projects: ProjectIndex, composition: PoolComposition) -> SessionPool:
    """Every session this process holds open, with the process's own as the default.

    Args:
        default: This process's own session.
        projects: What the pool knows about projects and sessions it did not open itself.
        composition: What this process lends every session the pool composes.

    Returns:
        The pool.
    """
    return SessionPool(default, projects=projects, composition=composition)


@injectable
def sessions_port(pool: SessionPool) -> Sessions:
    """The same pool, under the port a controller declares instead of the concrete class.

    Args:
        pool: The session pool.

    Returns:
        `pool` itself.
    """
    return pool


@dataclass(frozen=True)
class ArchivedRuns:
    """`EndedRuns` over the archive every run of this deployment lands in, narrowed to what a citation needs."""

    archive: RunArchiveQueries

    def find(self, run_id: str) -> SourceRun | None:
        """The run `run_id` names, read from the run archive, as the identity and the checked outcome an adaptation cites.

        Args:
            run_id: The ended run to find.

        Returns:
            The run, or `None` when the archive holds no run with that id.
        """
        record = self.archive.get(run_id)
        if record is None:
            return None
        return SourceRun(run_id=record.run_id, workflow_id=record.workflow_id, run_title=record.run_title, outcome=record.result)


@injectable
def ended_runs(archive: RunArchiveQueries) -> EndedRuns:
    """The ended runs an adaptation may cite, read off this deployment's own archive.

    Args:
        archive: The run archive every run of this deployment lands in.

    Returns:
        The ended runs, read from `archive`.
    """
    return ArchivedRuns(archive)


@injectable
def chat_service(pool: SessionPool, history: ConversationHistory, refresh: SessionRefresh) -> Chat:
    """One turn of one session, over the sessions this process holds and the record behind them.

    Args:
        pool: The sessions this process holds open.
        history: The conversation record a rebuilt session starts from.
        refresh: How a settled turn brings its session's projected rows up to date.

    Returns:
        The chat service.
    """
    return ChatService(pool, history, refresh)


REGISTRATIONS: list[object] = [
    save_approval,
    SessionCollaborators,
    session,
    pool_composition,
    session_pool,
    sessions_port,
    ended_runs,
    chat_service,
]
"""Everything this slice puts in the container, each under the type its consumers declare."""
