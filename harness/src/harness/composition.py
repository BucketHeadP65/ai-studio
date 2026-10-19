"""The composition root: the only module that knows every slice.

`harness_session` is the one composition both entries share. The HTTP lifespan (`harness.main`) and
the ACP entry (`harness.acp`) each resolve it through the container, so both serve the same agent. It
takes its collaborators as arguments, and `harness.features.authoring.wiring` says where each one
comes from. `build_environment` wires the retrieval surface and the custom items into the one
registry of steps a session executes.
"""

from __future__ import annotations

import sys

from collections.abc import Awaitable
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import NoneType
from typing import TYPE_CHECKING, get_args, get_origin

from core.ai.spend_store import FileSpendStore
from core.ai.workflows.contracts import (
    CURRENT_STEP,
    AgentStep,
    Briefing,
    GateDecision,
    LibraryError,
    PresentationRefusedError,
    SaveGuard,
    StepRegistry,
    Tier,
    Workflow,
    WorkflowArguments,
    WorkflowContract,
    WorkflowPresentation,
    prepare,
    validate,
)
from core.ai.workflows.infrastructure.library_collection import WORKFLOW_COLLECTION, LibraryCollection, collection_toolset
from core.ai.workflows.infrastructure.temporal import CONTINUING_STEP_CONVERSATION, json_output
from core.ai.workflows.infrastructure.temporal import connect as temporal_connect
from core.ai.workflows.infrastructure.temporal import worker as temporal_worker
from core.cqrs.contracts import TopicSink, utcnow
from core.observability import baggage, get_logger
from engram import RetrievalKnowledgeBase, document_embedder, embedded_memory_store
from engram.retrieval.contracts import reranking
from engram.skills.contracts import SKILLS as ENGRAM_SKILLS
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from pydantic_ai import Agent, AgentSpec, ModelRetry, capture_run_messages
from pydantic_ai.capabilities import CombinedCapability, Toolset
from pydantic_ai.messages import ModelRequest
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import CodeMode
from pydantic_ai_harness.skills import Skills
from pydantic_ai_harness.spend import Budget, SpendLimits
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

from harness.adapters.contracts import (
    BOOT_PATIENCE,
    GROUNDING_TOOLS,
    WEB_SEARCH_TOOL,
    KnowledgeBaseReading,
    MacNotifier,
    ModelTiers,
    NoopNotifier,
    ServerOutcome,
    StudioReadToolsOpener,
    TierModel,
    as_text,
    code_runner,
    embedding_contract,
    ensure_server,
    environment_stamps,
    grounding_toolset,
    in_sandbox,
    knowledge_base_capability,
    knowledge_base_embedder,
    knowledge_base_names,
    notify_owner_step,
    price_response,
    query_embedder,
    sandbox_pool,
    step_guards,
    step_shell,
    step_workspace,
    store_connection,
    store_ingest,
    store_writer_dsn,
    studio_http_client,
    studio_read_tools,
    tier_in_effect,
    web_search_toolset,
)
from harness.domain.contracts import (
    BRIEFING_ONLY_BUDGET,
    DEFINED_STEP_BUDGET,
    JUDGE_TOOLS,
    NOT_FOR_STEPS,
    SHELL_TOOL,
    UNKNOWN_SESSION,
    SessionEvents,
    SessionTranscript,
    StepModelWrapper,
    card_readiness,
    refusal_text,
)
from harness.features.authoring.contracts import (
    LaunchWorkflow,
    WorkflowProcess,
    build_agent,
    digest_now_tool,
    run_external_agent_step,
    skill_hint_capability,
)
from harness.features.authoring.infrastructure.studio import StudioKnowledgeBaseRefresher
from harness.features.browsing import contracts as browsing
from harness.features.browsing.contracts import CardRefusedError, WorkflowCardPatch
from harness.features.followme.contracts import FollowMe, act_toolset, verb_toolset
from harness.features.projects.contracts import memory_mounts, sanitize_segment
from harness.features.running import contracts as running
from harness.features.running.contracts import (
    RunGraphRefusedError,
    RunRecords,
    RunStateRefusedError,
    SpendLimit,
    SpendLog,
    StepConversation,
    StepSteerDelivery,
    WorkflowNotRunnableError,
    conversation_key,
    decide_session_gate,
    orchestrator_decider,
    pause_session_run,
    resume_session_run,
    rows_for,
    run_relayed,
    set_session_automode,
    spend_limits,
    stop_session_run,
)
from harness.features.running.infrastructure.run_log import (
    carried_state,
    load_waiting,
    reviewed_values,
    revive_values,
    sweep_orphan_reviewed,
)
from harness.features.selfauthoring.contracts import FileCustomCapabilities, Selection, TierBody, promoted_capability_types
from harness.features.selfauthoring.controller import SelfAuthoringController, SelfAuthoringDeployment
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.promotion import capability_grants_for
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from harness.features.sessions.contracts import project_config, project_id_of
from harness.knowledge_base import KnowledgeBase
from harness.plugins import Switched
from harness.session import (
    ACTIVE_RUN,
    WAITING_RUN_SUFFIX,
    ActiveRun,
    MissingProjectRootError,
    NoSessionLog,
    PooledSessions,
    Runtime,
    SessionPool,
    current_run_id,
    run_identity,
    run_log_path,
)
from harness.settings import CHECKOUT, SKILLS, ExternalAgentRegistry, StateSettings, aifactory_state, custom_root

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator, Iterable, Mapping, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    import httpx

    from core.ai.history.contracts import RequestParametersSink
    from core.ai.workflows.contracts import AgentResolver, TurnFacts
    from core.ai.workflows.infrastructure.temporal import StepConversationContinuation
    from core.settings import InfrastructureSettings
    from engram.authoring.contracts import AuthoringTools
    from engram.config import RerankSettings
    from pydantic import JsonValue
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.toolsets import AbstractToolset
    from pydantic_monty import AsyncMonty

    from harness.adapters.contracts import DatabaseBinding, Notifier
    from harness.domain.contracts import KnowledgeBaseSurface
    from harness.features.running.contracts import DurableTurns, SpendReporter
    from harness.features.scheduling.contracts import Scheduler
    from harness.features.sessions.contracts import Sessions
    from harness.settings import (
        ExternalAgentEntry,
        ExternalAgentRegistry,
        HarnessSettings,
        KnowledgeBaseGuidanceSettings,
        LibrarySearchSettings,
        RunSettings,
        SelfAuthoringSettings,
        SkillHintSettings,
        StoreSettings,
        WebSearchSettings,
    )

_logger = get_logger(__name__)


CHAT_KNOWLEDGE_BASE_PLUGINS = frozenset(
    {"search", "fetch", "request_source", "source_queue_policy", "write_policy", "source_queue_rows", "source_queue_decide", "decide_page"}
)
"""The chat agent's knowledge base surface: the two read tools, `request_source`, and the owner's plugins over the
source-queue policy, the source queue and its decisions. `write_note`, `list_undigested`, `read_source`,
`list_recent_digests`, `digest_skeleton`, `trace_source`, `remove_source_artifacts` and `skip_digest` stay
step-only. A workflow step reaches them by name through `build_environment`'s own grant, never through
chat, so a source's removal always goes through the gated `wf.source-remove` workflow."""

TURN_SEQUENCE_INSTRUCTIONS = """You are one worker holding one item through an ordered sequence of tasks.

Your item:

{item}

Each task arrives as its own user message. Everything you read or produced earlier in this
conversation is still yours, use it instead of reading anything a second time. Give every answer
through the task's own answer tool, never as a plain text message."""
"""The system prompt a briefing turn opens its conversation with, rendered once on the item's
first turn and reused verbatim by every later turn so the request prefix stays byte-stable."""

STEP_OUTPUT_RETRY_BUDGET = 4
"""How many times a workflow step's model may be sent back to answer in the step's output shape before the step or the map item fails."""


def _repair_asked_for(prompt: str) -> str:
    """`prompt`, followed by the original request and each gate's refused value and feedback, when this call is a repair."""
    identity = CURRENT_STEP.get()
    if identity is None or not identity.retry_feedback:
        return prompt
    reader = identity.reviewed_artifacts
    if reader is None:
        msg = f"the repair owed by {identity.node_id!r} carries no reader for the values it was refused over"
        raise RuntimeError(msg)
    sections = [prompt, f"### The request this run was given\n{identity.parameters_json}"]
    sections.extend(
        f"### Sent back at gate {feedback.gate_id} (attempt {feedback.attempt}, decided by {feedback.source})\n"
        f"What was reviewed:\n{as_text(reader.read(feedback.reviewed))}\n\n"
        f"What was wrong: {feedback.feedback}\n"
        f"What to change: {feedback.requested_repair}"
        for feedback in identity.retry_feedback
    )
    return "\n\n".join(sections)


@contextmanager
def _keeping_a_stopped_turn(line: StepConversation) -> Generator[None]:
    """Write whatever a failed or cut-off turn exchanged onto `line`, so the next call sees it."""
    with capture_run_messages() as messages:
        try:
            yield
        except BaseException:
            line.messages = list(messages)
            raise


def _document_skill_names(document: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """The `skills` names a worker's document carries, a sibling of `capabilities` kept out of `AgentSpec` itself."""
    raw = document.get("skills")
    if not isinstance(raw, list):
        return ()
    return tuple(name for name in raw if isinstance(name, str))


def build_worker_agent(  # noqa: PLR0913 (each keyword is a separate term the host sets)
    step: AgentStep,
    output_type: type,
    *,
    model: Model,
    engram: KnowledgeBaseSurface,
    reads: tuple[str, ...],
    budget: int,
    promoted: tuple[type[AbstractCapability[object]], ...],
    resolve_agent: AgentResolver,
    web_search: WebSearchSettings,
    plugins: AuthoringTools | None = None,
    spend: AbstractCapability[object] | None = None,
) -> Agent[None, object]:
    """Build one worker's agent exactly as a graph node runs it: the document, merged under this host's terms.

    The host's terms override the document: the model is always the host's, and the output type
    comes from the step's own `output` slot. A worker holds the reads it named and the shell when it
    named it, never `CodeMode`. Its shell refuses the `harness` verbs that change what every later
    session holds, because a worker has no person to ask.

    Args:
        step: The worker step, whose `agent` is the document inline or the name of a custom agent.
        output_type: The type the step's `output` slot declares.
        model: The model the worker answers on.
        engram: The retrieval surface this function narrows to the reads the step names.
        reads: The read tools this run offers.
        budget: The request budget the worker's guards enforce.
        promoted: Every promoted custom capability's class, which the document may name.
        resolve_agent: How a custom agent's name becomes its document.
        web_search: Whether `web_search` reaches Tavily. A deployment holding no key searches keyless.
        plugins: The `AuthoringTools` `studio_read_tools` opened, which the grounding tools and `skip_digest` are built from.
            `None` makes the grounding tools unreachable.
        spend: The spend limits the agent mounts. `None` mounts none.

    Returns:
        The worker's agent, built and not yet run.

    Raises:
        RuntimeError: `step` names a tool that is not one of this run's reads, `SHELL_TOOL`,
            `WEB_SEARCH_TOOL` or one of `GROUNDING_TOOLS`.
    """
    grounding_reachable = GROUNDING_TOOLS if plugins is not None else ()
    reachable = (*reads, SHELL_TOOL, WEB_SEARCH_TOOL, *grounding_reachable)
    if unreachable := tuple(name for name in step.tools if name not in reachable):
        msg = (
            f"the worker {step.name!r} was granted {', '.join(unreachable)}, which a worker cannot reach here: "
            f"a worker holds this run's reads plus {SHELL_TOOL} and web search ({', '.join(reachable) or 'none'}). "
            f"Make it a briefing, or drop the grant."
        )
        raise RuntimeError(msg)
    granted = frozenset(step.tools)
    reads_granted = tuple(name for name in reads if name in granted)
    web_search_granted = WEB_SEARCH_TOOL in granted
    grounding_granted = tuple(name for name in grounding_reachable if name in granted)
    # `direct_reads`: a worker holds its reads (web search and grounding, the same shape) as a
    # plain toolset, with no CodeMode between the tool and the model, so their returns are spilled
    # here or ride its history.
    direct_reads = (*reads_granted, *((WEB_SEARCH_TOOL,) if web_search_granted else ()), *grounding_granted)
    document = step.agent if not isinstance(step.agent, str) else resolve_agent(step.agent)
    skill_names = _document_skill_names(document)
    document_without_skills = {key: value for key, value in document.items() if key != "skills"}
    return Agent.from_spec(
        AgentSpec.model_validate(dict(document_without_skills)),
        model=model,
        output_type=output_type,
        toolsets=[
            *([engram.filtered(lambda _ctx, tool: tool.name in reads_granted)] if reads_granted else []),
            *([web_search_toolset(web_search.tavily_api_key)] if web_search_granted else []),
            *(
                [grounding_toolset(plugins).filtered(lambda _ctx, tool: tool.name in grounding_granted)]
                if plugins is not None and grounding_granted
                else []
            ),
        ],
        capabilities=[
            *([step_shell()] if SHELL_TOOL in granted else []),
            *([step_workspace(writable=SHELL_TOOL in granted)] if SHELL_TOOL in granted or skill_names else []),
            *step_guards(budget, direct_reads=direct_reads),
            WarnOnCacheBusts(),
            *([Skills((SKILLS, ENGRAM_SKILLS), include=skill_names)] if skill_names else []),
            *([spend] if spend is not None else []),
        ],
        custom_capability_types=(*promoted, Skills),
    )


def _budget_line(budget: int, tool_calls: int | None) -> str:
    """The sentence a defined step's own prompt ends with, naming what it may spend before it starts."""
    if tool_calls is None:
        return f"You have {budget} model requests for this task. When they are nearly gone, answer with what you have."
    return (
        f"You have {budget} model requests and {tool_calls} tool calls for this task. When they are nearly gone, answer with what you have."
    )


def step_baggage(active: ActiveRun, step: str) -> AbstractContextManager[None]:
    """The identities every span a step opens for `step` carries: its run, its workflow, and the step.

    A step running for no run carries nothing. Baggage reaches every span opened underneath, the model
    spans pydantic-ai opens included, so a cost is attributed by reading the span that holds it.

    Args:
        active: The run the step is running for.
        step: The step's name.

    Returns:
        A context manager that sets the baggage while it is entered, or sets nothing for no run.
    """
    identity = run_identity(active)
    if identity is None:
        return nullcontext()
    return baggage(run_id=identity.run_id, workflow=identity.workflow_id, step=step)


def _record_spend(active: ActiveRun, step: str, messages: Sequence[ModelMessage], ledger: SpendLog) -> None:
    """Append what `step` just produced to `ledger`, logging a row it cannot write rather than failing the step."""
    identity = run_identity(active)
    if identity is None:
        return
    try:
        ledger.record(identity.run_id, identity.workflow_id, rows_for(step, messages))
    except OSError as error:
        _logger.warning("spend row dropped for {step}: {error}", step=step, error=error)


class MissingTurnLineError(RuntimeError):
    """A turn past the first has no conversation to resume: the process that ran its earlier turns is not this one.

    A turn resumes from `active.conversations`, which only the process that ran the earlier turns holds.
    """

    def __init__(self, node_id: str, item_key: str, turn: TurnFacts, work: str) -> None:
        """Name the turn whose conversation is missing.

        Args:
            node_id: The node the turn runs in.
            item_key: The item the turn holds.
            turn: The turn, whose index the message counts from one.
            work: The step's name.
        """
        super().__init__(
            f"node {node_id!r} item {item_key!r} turn {turn.index + 1} of {work!r}: the conversation "
            "that ran its earlier turns is not in this process."
        )


@dataclass(frozen=True)
class TurnOnALine:
    """One turn of a durable step conversation, with everything performing it needs besides its checkpoint."""

    turns: DurableTurns
    """Where the step conversation's checkpoint is read from and settled to."""
    output_type: type
    opening: str
    """What a first turn, whose checkpoint is empty, opens the step conversation's system prompt with."""
    build: Callable[[str], Agent[None, object]]
    """Builds the turn's agent from the system prompt the step conversation was opened with, known only once the checkpoint is read."""
    prompt: str
    usage_limits: UsageLimits
    baggage: AbstractContextManager[None]
    """The span identities the model call runs under."""
    billed: Callable[[list[ModelMessage]], None]
    """Records the spend of the messages the turn produced."""


async def _performed_on_a_line(state: StepConversationContinuation, call: TurnOnALine) -> object:
    """One turn of a step conversation the record carries, answering a settled attempt's stored output without calling a model."""
    resumed = await call.turns.begin(state)
    if resumed.settled is not None:
        return TypeAdapter(call.output_type).validate_python(resumed.settled.output)
    worker = call.build(_opened_with(resumed.messages, call.opening))
    with call.baggage:
        result = await worker.run(
            call.prompt,
            message_history=resumed.messages or None,
            conversation_id=str(state.attempt.conversation_id),
            usage_limits=call.usage_limits,
            capabilities=[call.turns.persistence(state)],
        )
    call.billed(result.new_messages())
    await call.turns.settle(resumed, json_output(result.output, call.output_type))
    return result.output


def _opened_with(checkpoint: Sequence[ModelMessage], opening: str) -> str:
    """The system prompt `checkpoint` was opened with, or `opening` when the checkpoint carries none."""
    for message in checkpoint:
        if isinstance(message, ModelRequest) and message.instructions:
            return message.instructions
    return opening


def _resume_line(current: ActiveRun, key: tuple[str, str], turn: TurnFacts, work: str) -> StepConversation | None:
    """The settled conversation `key` resumes: `None` for a first turn, refused when a later turn finds none."""
    line = current.conversations.conversations.get(key)
    if line is not None and not line.messages:
        line = None
    if line is None and turn.index > 0:
        raise MissingTurnLineError(*key, turn, work)
    return line


def _shape_name(shape: object) -> str:
    """A readable name for a turn's own output type: a class's own name, or a generic spelled out with its arguments."""
    origin = get_origin(shape)
    if origin is None:
        return getattr(shape, "__name__", str(shape))
    inner = ", ".join(_shape_name(arg) for arg in get_args(shape)) or "..."
    return f"{_shape_name(origin)}[{inner}]"


def _turn_output_validator(output_type: type) -> Callable[[object], object]:
    """This turn's own check on the model's answer, sending another turn's shape back as a `ModelRetry`."""
    shape = _shape_name(output_type)

    def validate(data: object) -> object:
        try:
            return TypeAdapter(output_type).validate_python(data)
        except ValidationError as error:
            msg = f"This task answers with {shape}. Your answer has the shape of another task. Finish this task, then answer with {shape} through its own answer tool."
            raise ModelRetry(msg) from error

    return validate


async def build_environment(  # noqa: PLR0913, PLR0915 (each keyword is a separate term the host sets, and the statements are the arms of run_defined_step)
    model: Model,
    *,
    engram: KnowledgeBaseSurface,
    sandbox: AsyncMonty | None = None,
    active: ActiveRun | None = None,
    notifier: Notifier | None = None,
    plugins: AuthoringTools | None = None,
    external_agents: ExternalAgentRegistry,
    session_log: SessionEvents,
    custom: Path,
    records: RunRecords,
    web_search: WebSearchSettings,
    tiers: ModelTiers,
    spend: AbstractCapability[object] | None = None,
    turns: DurableTurns | None = None,
    wrap_model: StepModelWrapper | None = None,
) -> StepRegistry:
    """Wire the retrieval surface into the one thing this process can execute: a registry of steps.

    Besides `notify_owner` and `run_external_agent`, the registry holds only the custom steps the owner
    activated with `harness activate step <name>`. Promoted custom capabilities are read once, when the
    environment is built, so a `promote` or `demote` takes effect on the next build, and a worker
    document may declare one by name. The caller owns the connection behind `engram`, whose tools are
    read off it here.

    Args:
        model: What a worker step performs on, and what a briefing performs on when nothing names the
            orchestrator or the helper tier for it.
        engram: The retrieval surface a step's reads are granted from.
        sandbox: An entered Monty pool, which is what lets a workflow carry a program rather than only
            a briefing. `None` refuses a workflow that carries a program, because code that arrived
            inside a workflow never runs outside a sandbox.
        active: The run state a step reads and writes as it runs, such as its stashed rejection
            feedback and its own conversation across calls. A caller that builds a `Runtime` over
            this registry passes the same instance. `None` builds a fresh one.
        notifier: How the `notify_owner` step reaches this machine's owner. `None` registers a
            `NoopNotifier`, and this function never picks a platform notifier itself.
        plugins: The raw `AuthoringTools` `studio_read_tools` opened, which `grounding_toolset` builds
            `knowledge_base_profile`, `recall_beliefs`, `remember_belief` and `skip_digest` from. `None` leaves
            those four off what a step may be granted.
        external_agents: Which ACP agents the `run_external_agent` step may spawn. A workflow run
            has nobody to ask, so an entry under the `gate` policy refuses there and says so.
        session_log: Where the `run_external_agent` step narrates what it started and finished.
        custom: The owner-reviewed custom-item root, whose `steps/`, `agents/` and `capabilities/`
            this environment loads from.
        records: Where a step's own spend rows land.
        web_search: Whether `web_search` reaches Tavily. A deployment holding no key searches
            keyless, and says so by handing over a section with no key in it.
        tiers: How the orchestrator and the helper tier resolve their model, read fresh per request.
            A build that wants both answering on `model` itself says so through a tier resolution
            that builds `model`, never by leaving this out.
        spend: The spend limits every step's agent mounts. Pass the chat agent's own instance, so
            one day ceiling covers both. `None` mounts none.
        turns: Where a durable step conversation is carried between turns. Read only inside a
            Temporal activity that published one, so an in-process run keeps resuming its item out of
            `StepConversations` whatever this holds. `None` leaves the durable arm unavailable, and a
            durable turn then falls back to this process's own memory of the step conversation.
        wrap_model: What every role's model is put through before a step agent is built over it.
            `None` builds each role on the model itself, which is what this deployment does: a
            wrapper is mounted only by a caller that is measuring what a run spends.

    Returns:
        The registry of steps a session executes, with the owner's active custom steps loaded.
    """
    active = active if active is not None else ActiveRun()
    # Derived rather than typed out, so a retrieval tool added to engram becomes grantable without
    # a change here. `describe_schema` is grantable like any read.
    reads = tuple(tool.name for tool in await engram.list_tools() if tool.name not in NOT_FOR_STEPS)
    grounding = GROUNDING_TOOLS if plugins is not None else ()
    grantable = (*reads, SHELL_TOOL, WEB_SEARCH_TOOL, *grounding)
    """What a declared step may ask for by name: the server's reads, the shell it acts with, web
    search, and the grounding tools and `skip_digest` when this environment has plugins."""

    promoted = promoted_capability_types(FileCustomCapabilities.read_only(custom / "capabilities"))
    """Every promoted custom capability's class. Every `Agent.from_spec` call here reads it as
    `custom_capability_types=`, and `capability_grants` below opens exactly this set to a worker."""

    def _step_toolsets(granted: frozenset[str]) -> list[AbstractToolset[None]]:
        """The reads, web search and grounding a briefing's grant reaches, narrowed to exactly `granted`."""
        reads_granted = tuple(name for name in reads if name in granted)
        grounding_granted = tuple(name for name in grounding if name in granted)
        return [
            *([engram.filtered(lambda _ctx, tool: tool.name in reads_granted)] if reads_granted else []),
            *([web_search_toolset(web_search.tavily_api_key)] if WEB_SEARCH_TOOL in granted else []),
            *(
                [grounding_toolset(plugins).filtered(lambda _ctx, tool: tool.name in grounding_granted)]
                if plugins is not None and grounding_granted
                else []
            ),
        ]

    tier_models: dict[Tier, Model] = {
        "worker": model,
        "orchestrator": TierModel("orchestrator", tiers),
        "helper": TierModel("helper", tiers),
    }
    """Which model answers a briefing. The worker tier keeps running on `model` itself, whatever it
    already is. The other two tiers are read fresh from the store, per request, the moment a node
    or a step names one."""
    if wrap_model is not None:
        tier_models = {role: wrap_model(built, role=role) for role, built in tier_models.items()}

    async def run_defined_step(  # noqa: PLR0915 (three arms in sequence: a one-shot briefing, a turn this process remembers, a turn the record carries)
        task: Briefing, output_type: type, /, **inputs: object
    ) -> object:
        """Perform a briefing step, one-shot or as one turn of a multi-turn map resuming its item's conversation."""
        current = ACTIVE_RUN.get() or active
        identity = CURRENT_STEP.get()
        turn = identity.turn if identity is not None else None
        tier: Tier = (identity.tier if identity is not None else None) or task.tier or "worker"
        limits = task.budget
        default_budget = DEFINED_STEP_BUDGET if (task.tools or turn is not None) else BRIEFING_ONLY_BUDGET
        budget = limits.requests if limits is not None else default_budget
        tool_calls = limits.tool_calls if limits is not None else None
        usage_limits = UsageLimits(request_limit=budget, tool_calls_limit=tool_calls)
        granted = frozenset(turn.tools) if turn is not None else frozenset(task.tools)
        toolsets = _step_toolsets(granted)
        if turn is None:
            key = conversation_key(identity, task.name)
            line = current.conversations.open(key, step=task, output_type=output_type, instructions=task.instructions, monitor=None)
            worker: Agent[None, object] = Agent(
                tier_models[tier],
                output_type=output_type,
                deps_type=NoneType,
                toolsets=toolsets,
                capabilities=[
                    *([CodeMode(tools=in_sandbox)] if task.tools else []),
                    *([step_shell()] if SHELL_TOOL in granted else []),
                    *([step_workspace(writable=SHELL_TOOL in granted)] if SHELL_TOOL in granted or task.skills else []),
                    *step_guards(budget),
                    WarnOnCacheBusts(),
                    *([Skills[None]((SKILLS, ENGRAM_SKILLS), include=task.skills)] if task.skills else []),
                    *([spend] if spend is not None else []),
                ],
                instructions=task.instructions,
                retries={"output": STEP_OUTPUT_RETRY_BUDGET},
            )
            briefing = "\n\n".join(f"### {name}\n{as_text(value)}" for name, value in inputs.items())
            prompt = briefing or "You have no inputs. Work from your instructions alone."
            prompt = _repair_asked_for(prompt)
            prompt += "\n\n" + _budget_line(budget, tool_calls)
            try:
                with step_baggage(current, task.name), _keeping_a_stopped_turn(line):
                    result = await run_relayed(worker, line, prompt, usage_limits=usage_limits, capabilities=[StepSteerDelivery(line)])
            finally:
                line.executing = False
            current.conversations.settle(key, result.all_messages())
            _record_spend(current, task.name, result.new_messages(), records.spend)
            return result.output

        def _turn_worker(*, surface: list[type], instructions: str, monitor: WarnOnCacheBusts) -> Agent[None, object]:
            """The agent one turn of a sequence answers on, mounted from the sequence's unions so every turn sends the same prefix."""
            built: Agent[None, object] = Agent(
                tier_models[tier],
                output_type=surface if len(surface) > 1 else surface[0],
                deps_type=NoneType,
                toolsets=toolsets,
                capabilities=[
                    *([CodeMode(tools=in_sandbox)] if granted else []),
                    *([step_shell()] if SHELL_TOOL in granted else []),
                    *([step_workspace(writable=SHELL_TOOL in granted)] if SHELL_TOOL in granted or turn.skills else []),
                    *step_guards(budget),
                    monitor,
                    *([Skills[None]((SKILLS, ENGRAM_SKILLS), include=turn.skills)] if turn.skills else []),
                    *([spend] if spend is not None else []),
                ],
                instructions=instructions,
                retries={"output": STEP_OUTPUT_RETRY_BUDGET},
            )
            built.output_validator(_turn_output_validator(output_type))
            return built

        def _turn_prompt() -> str:
            """This turn's own words: its instructions, its inputs beside the item, its shape and its budget."""
            briefing = "\n\n".join(f"### {name}\n{as_text(value)}" for name, value in inputs.items() if name != turn.each)
            written = task.instructions if not briefing else f"{task.instructions}\n\n{briefing}"
            written += f"\n\nAnswer this task with {_shape_name(output_type)}."
            written = _repair_asked_for(written)
            return written + "\n\n" + _budget_line(budget, tool_calls)

        continuing = CONTINUING_STEP_CONVERSATION.get()
        if turns is not None and continuing is not None:
            item = inputs.get(turn.each)
            return await _performed_on_a_line(
                continuing,
                TurnOnALine(
                    turns=turns,
                    output_type=output_type,
                    opening=TURN_SEQUENCE_INSTRUCTIONS.format(item=as_text(item)) if item is not None else task.instructions,
                    build=lambda written: _turn_worker(
                        surface=list(dict.fromkeys(turn.outputs)), instructions=written, monitor=WarnOnCacheBusts()
                    ),
                    prompt=_turn_prompt(),
                    usage_limits=usage_limits,
                    baggage=step_baggage(current, task.name),
                    billed=lambda produced: _record_spend(current, task.name, produced, records.spend),
                ),
            )
        key = conversation_key(identity, task.name)
        line = _resume_line(current, key, turn, task.name)
        instructions = line.instructions if line is not None else TURN_SEQUENCE_INSTRUCTIONS.format(item=as_text(inputs[turn.each]))
        monitor = line.monitor if line is not None and line.monitor is not None else WarnOnCacheBusts()
        opened = current.conversations.open(key, step=task, output_type=output_type, instructions=instructions, monitor=monitor)
        worker = _turn_worker(surface=list(dict.fromkeys(turn.outputs)), instructions=instructions, monitor=monitor)
        try:
            with step_baggage(current, task.name), _keeping_a_stopped_turn(opened):
                result = await run_relayed(
                    worker,
                    opened,
                    _turn_prompt(),
                    message_history=line.messages if line is not None else None,
                    usage_limits=usage_limits,
                    capabilities=[StepSteerDelivery(opened)],
                )
        finally:
            opened.executing = False
        current.conversations.settle(key, result.all_messages())
        _record_spend(current, task.name, result.new_messages(), records.spend)
        return result.output

    def _resolve_stored_agent(name: str) -> Mapping[str, JsonValue]:
        """`AgentResolver`: the active document of the custom agent `name`, read fresh on every call."""
        return FileCustomAgents.read_only(custom / "agents").resolve_active(name)

    async def run_agent_step(step: AgentStep, output_type: type, /, **inputs: object) -> object:
        """Run a worker step, its agent document merged under this host's terms, keeping its conversation across calls."""
        current = ACTIVE_RUN.get() or active
        budget = DEFINED_STEP_BUDGET if step.tools else BRIEFING_ONLY_BUDGET
        worker = build_worker_agent(
            step,
            output_type,
            model=model,
            engram=engram,
            reads=reads,
            budget=budget,
            promoted=promoted,
            resolve_agent=_resolve_stored_agent,
            web_search=web_search,
            plugins=plugins,
            spend=spend,
        )
        key = conversation_key(CURRENT_STEP.get(), step.name)
        line = current.conversations.open(key, step=step, output_type=output_type, instructions="", monitor=None)
        briefing = "\n\n".join(f"### {name}\n{as_text(value)}" for name, value in inputs.items())
        prompt = briefing or "You have no inputs. Work from your instructions alone."
        prompt = _repair_asked_for(prompt)
        try:
            with step_baggage(current, step.name), _keeping_a_stopped_turn(line):
                result = await run_relayed(
                    worker,
                    line,
                    prompt,
                    message_history=line.messages or None,
                    usage_limits=UsageLimits(request_limit=budget),
                    capabilities=[StepSteerDelivery(line)],
                )
        finally:
            line.executing = False
        current.conversations.settle(key, result.all_messages())
        _record_spend(current, step.name, result.new_messages(), records.spend)
        return result.output

    # An empty registry and no write primitive: a step that needs to write asks for `run_command`.
    # `capability_grants` grants exactly the promoted custom capabilities and nothing wider.
    registry = StepRegistry(
        perform=run_defined_step,
        grants=grantable,
        run_code=code_runner(sandbox, reads=reads, call=engram.direct_call_tool) if sandbox is not None else None,
        run_agent=run_agent_step,
        resolve_agent=_resolve_stored_agent,
        capability_grants=capability_grants_for(promoted),
        judge_tools=frozenset(JUDGE_TOOLS) & frozenset(grantable),
    )
    registry.register(notify_owner_step(notifier or NoopNotifier()), name="notify_owner", replay_safe=False)
    registry.register(run_external_agent_step(external_agents, session_log=session_log), name="run_external_agent", replay_safe=False)
    session_registry = registry.copy()
    FileCustomSteps.read_only(custom / "steps").load_active(session_registry)
    return session_registry


def _announce_database_binding(binding: DatabaseBinding) -> None:
    """Log which databases this process is about to open, warning when no reader URL is exported."""
    _logger.info("studio binding: {binding}", binding=binding.description)
    if not binding.named:
        _logger.warning("studio binding: {binding} has no APP_*READER_URL export. Is this the database you meant?", binding=binding.description)


_DEPLOYMENT_BUDGETS: tuple[Budget[object], ...] = (Budget(usd=Decimal(2), window="run"), Budget(usd=Decimal(25), window="day", warn_at=0.8))
"""This deployment's ceilings: `$2` per run, `$25` per day, warned at 80%."""


def _default_spend_limits(state: StateSettings) -> SpendLimits[object]:
    """This deployment's spend limits, priced by `price_response`, counted in `state.spend_counters` so a restart keeps them."""
    # `object` deps, so one instance fits a workflow step's `Agent[None, object]` and the chat agent's `Agent[Runtime, ...]`.
    return SpendLimits[object](budgets=_DEPLOYMENT_BUDGETS, price=price_response, store=FileSpendStore(state.spend_counters, clock=utcnow))


def deployment_brakes() -> tuple[SpendLimit, ...]:
    """Every money ceiling a run started here is held to, as a preview states them before a launch.

    Read off the very budgets `_default_spend_limits` mounts, so the figures a person is shown and
    the figures that actually refuse can never be two different sets.

    Returns:
        The ceilings, one per budget window.
    """
    return spend_limits(SpendLimits[object](budgets=_DEPLOYMENT_BUDGETS, price=price_response))


def _no_external_agents() -> Mapping[str, ExternalAgentEntry]:
    """No ACP agent at all: what a session that only saves and evaluates reads the registry as."""
    return {}


@asynccontextmanager
async def library_composition(
    settings: HarnessSettings,
    records: RunRecords,
    tiers: ModelTiers,
    binding: DatabaseBinding,
    *,
    open_read_tools: StudioReadToolsOpener = studio_read_tools,
) -> AsyncGenerator[tuple[LibraryCollection, StepRegistry]]:
    """The library and the registry alone: what saving a directory or evaluating one step needs.

    Built from the same lower-level builders `harness_session` composes (`studio_read_tools`,
    `store_connection`, `store_ingest`, `build_environment`), but it never opens the SQL plugin
    (`knowledge_base_capability`) or the chat agent. So a CLI verb can save onto the library or run a
    step's dataset with no `APP_RUN__RUN_ENGINE` set. Its registry refuses every gate, and a workflow
    with an orchestrator gate still validates and saves here.

    Args:
        settings: Every section this deployment sets. A console verb passes its own whole settings.
        records: Where a run's own records land.
        tiers: How a tier name becomes a model here.
        binding: Which databases this process is about to open.
        open_read_tools: How the studio's knowledge bases are opened, `studio_read_tools` by default.
            A caller that wants to see what this composition mounts them with passes its own.

    Yields:
        The library collection and the step registry, in that order, for as long as the block runs.

    Raises:
        RuntimeError: The studio knowledge base API did not answer, so which knowledge bases this
            deployment holds is unknown and no library can be opened over them.
    """
    _announce_database_binding(binding)
    reading = KnowledgeBaseReading(
        guidance=settings.knowledge_base_guidance.guidance, cross_search=settings.library_search.cross_knowledge_base_search
    )
    async with AsyncExitStack() as stack:
        # The studio owns the registry of knowledge bases, so the library is opened over what its API
        # answers. Entered on this composition's own stack, which closes when its caller's block ends.
        studio_client = await stack.enter_async_context(studio_http_client(settings.studio_api))
        reader, _disclosure, plugins = await stack.enter_async_context(
            open_read_tools(client=studio_client, reading=reading, current_run=current_run_id)
        )
        connection = await store_connection()
        stack.push_async_callback(connection.close)
        ingest = await stack.enter_async_context(store_ingest())
        embed = query_embedder() if settings.store.hybrid_search else None
        store = LibraryCollection(connection, ingest, embed=embed)
        notifier = MacNotifier() if sys.platform == "darwin" else NoopNotifier()
        registry = await build_environment(
            TierModel("worker", tiers),
            engram=reader,
            sandbox=await stack.enter_async_context(sandbox_pool()),
            notifier=notifier,
            plugins=plugins,
            external_agents=_no_external_agents,
            session_log=NoSessionLog(),
            custom=custom_root(settings.self_authoring),
            records=records,
            spend=_default_spend_limits(settings.state),
            web_search=settings.web_search,
            tiers=tiers,
        )
        registry.decide_gate = _refuse_gate_outside_a_session
        yield store, registry


def _launcher(records: RunRecords, topics: TopicSink, run: RunSettings) -> LaunchWorkflow:
    """`running.start` with this deployment's record homes, channel and engine already bound."""

    async def launch(session: Runtime, *, spec: Workflow, workflow_id: str, params: WorkflowArguments, runner: str) -> None:
        """Start `spec` as `workflow_id` in `session`, in the background, logged like any other run."""
        await running.start(
            session, records=records, topics=topics, spec=spec, workflow_id=workflow_id, params=params, runner=runner, run=run
        )

    return launch


async def _refuse_gate_outside_a_session(gate_id: str, charge: str, value: object, /) -> GateDecision:
    """The registry session's answer to a gate: a refusal, never a verdict, since only a harness session runs a workflow."""
    del charge, value
    msg = f"gate {gate_id!r} cannot be decided here: the registry session saves and evaluates steps, only a harness session runs a workflow"
    raise RuntimeError(msg)


def _described_or_refused(registry: StepRegistry) -> SaveGuard:
    """This deployment's bar for a workflow worth keeping, the same `card_readiness` and `refusal_text` the save routes use."""

    def guard(workflow: Workflow, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None) -> str | None:
        del applicability
        readiness = card_readiness(workflow, prepare(workflow, registry), title=title, summary=summary, presentation=presentation)
        return None if readiness.described else refusal_text(readiness)

    return guard


async def harness_session(  # noqa: PLR0913 (each keyword is one collaborator this composition is handed)
    stack: AsyncExitStack,
    *,
    studio_client: httpx.AsyncClient,
    store: StoreSettings,
    library_search: LibrarySearchSettings,
    guidance: KnowledgeBaseGuidanceSettings,
    web_search: WebSearchSettings,
    self_authoring: SelfAuthoringSettings,
    run: RunSettings,
    state: StateSettings,
    records: RunRecords,
    reporter: SpendReporter,
    session_log: SessionEvents,
    transcript: SessionTranscript,
    turns: DurableTurns,
    parameters: RequestParametersSink,
    sessions: PooledSessions,
    topics: TopicSink,
    notifier: Notifier,
    tiers: ModelTiers,
    binding: DatabaseBinding,
    external_agents: ExternalAgentRegistry,
    scheduler: Scheduler,
    open_read_tools: StudioReadToolsOpener = studio_read_tools,
    save_requires_approval: bool = False,
    wrap_model: StepModelWrapper | None = None,
    infrastructure: InfrastructureSettings | None = None,
    rerank: RerankSettings | None = None,
    skill_hint: SkillHintSettings | None = None,
) -> Runtime:
    """The one composition both entries share: knowledge base, store, registry, port, agent, session.

    The HTTP lifespan (`harness.main`) and the ACP entry point (`harness.acp`) both resolve this
    through the container, so both serve the same agent. The one composition difference an entry
    may have is `save_requires_approval`.

    Args:
        stack: What every connection this session opens is entered on, so they close with it.
        studio_client: This deployment's client on the studio knowledge base API. Two surfaces
            read the studio through it: the registry of knowledge bases the library and the SQL
            plugin are opened over, and `digest_now`'s ingest calls.
        store: How the library ranks candidates, which decides whether a query is embedded.
        library_search: Whether one read here may cross knowledge bases.
        guidance: How to retrieve well from any knowledge base here, beneath each one's applied policy.
        web_search: Whether `web_search` reaches Tavily or searches keyless.
        self_authoring: Where the owner-reviewed custom items live.
        run: Which engine runs a workflow.
        state: Where this deployment's durable process state is written.
        records: Where a run's own records land: its spend, the archive and the evidence log.
        reporter: What a spend question is answered from.
        session_log: This deployment's own timeline, which the session narrates into.
        transcript: This deployment's own transcript, which its runs are written to and resumed from.
        turns: Where a durable run's multi-turn step conversations are carried between activities.
            Read only by a step running inside a Temporal activity that published one. An in-process
            run resumes its item out of this process's own memory.
        parameters: Where every request's tool definitions and model settings are recorded, beside
            the messages the transcript already keeps.
        sessions: The pool this process holds, resolved when an act reaches another session.
        topics: Where a change this session causes is announced.
        notifier: How the `notify_owner` step reaches this machine's owner.
        tiers: How a tier name becomes a model, for the tiers the environment and the agent read.
        binding: Which databases this process is about to open.
        external_agents: Which ACP agents this session may spawn, read per call.
        scheduler: The scheduler the workflow process's schedule tools go through.
        open_read_tools: How the studio's knowledge bases are opened, `studio_read_tools` by default.
            A caller that wants to see what this session mounts them with passes its own.
        save_requires_approval: Whether a save through the library port surfaces as a permission
            request first. Only a host whose protocol can ask sets it.
        wrap_model: What every role's model is put through before this session's step agents are
            built over it. `None` is this deployment: nothing is wrapped and no measurement opens.
        infrastructure: Where external systems are reached, read for the OpenRouter key decision models use. `None`
            turns re-ranking and the skill hint off.
        rerank: How search hits are re-ranked. `None` turns re-ranking off.
        skill_hint: Whether the chat agent gets the skill hint. `None` turns it off.

    Returns:
        The session's runtime, with its gate decider bound.

    Raises:
        RuntimeError: The studio knowledge base API did not answer, so which knowledge bases this
            deployment holds is unknown and neither the library nor the SQL plugin can open.
    """
    _announce_database_binding(binding)
    custom = custom_root(self_authoring)
    reading = KnowledgeBaseReading(guidance=guidance.guidance, cross_search=library_search.cross_knowledge_base_search)
    # Entered on the stack: the engines underneath have a lifetime the composition root owns. Both
    # entries that build this session (the HTTP lifespan, the ACP entry point) are a process just
    # starting, so this one read waits out the studio's own boot rather than racing it.
    # The spend limits, one instance for the whole process: mounted on a workflow step's agent
    # below and on the chat agent's own worker tree (`build_agent`'s `spend=`), so the day
    # ceiling covers everything this deployment runs rather than tracking two ceilings that each
    # allow half a runaway. The re-ranker's requests run through it too.
    spend = _default_spend_limits(state)
    reranker = reranking(rerank, infrastructure.openrouter, capabilities=(spend,)) if rerank is not None and infrastructure is not None else None
    reader, disclosure, plugins = await stack.enter_async_context(
        open_read_tools(client=studio_client, reading=reading, patience=BOOT_PATIENCE, current_run=current_run_id, rerank=reranker)
    )
    if not isinstance(reader, RetrievalKnowledgeBase):
        # The chat agent mounts engram's read capability itself (its tools AND its voices), where a
        # step environment only reads the grantable surface off it. So this session needs the capability.
        msg = f"the library opened as {type(reader).__name__}, which is not engram's read capability: the chat agent has no tools to mount"
        raise TypeError(msg)
    connection = await store_connection()
    stack.push_async_callback(connection.close)
    ingest = await stack.enter_async_context(store_ingest())
    # `query_embedder` builds the query side from engram's own factory and settings, so the two
    # sides cannot come to mean different things by "the same model".
    embed = query_embedder() if store.hybrid_search else None
    library = LibraryCollection(connection, ingest, embed=embed)
    # The agent-memory store: vendor authority (compare-and-set, receipts) plus engram's own
    # projection, entered once for the session's lifetime like `store_ingest` above, because it owns
    # the same writer role's pool. `embedding_contract` is the one source both this and `store_ingest`
    # read, so a note's vectors and a workflow page's vectors are never in disagreeing spaces.
    memory_contract = embedding_contract()
    agent_memory_store = await stack.enter_async_context(
        embedded_memory_store(
            store_writer_dsn(),
            embedder=document_embedder(
                deployment=memory_contract.deployment, infrastructure=memory_contract.infrastructure, dimensions=memory_contract.dimensions
            ),
            embed_query=knowledge_base_embedder(),
        )
    )
    # Built before the registry, and threaded into it, so the two share one instance: a step
    # closed over `active` at environment-build time and a `Runtime` that owns `active` afterwards
    # are reading and writing the very same run state.
    active = ActiveRun()
    # The sandbox is entered here rather than inside `build_environment` because a registry that closed
    # its own sandbox when it went out of scope would take running workflows with it.
    registry = await build_environment(
        TierModel("worker", tiers),
        engram=reader,
        sandbox=await stack.enter_async_context(sandbox_pool()),
        active=active,
        notifier=notifier,
        plugins=plugins,
        external_agents=external_agents,
        session_log=session_log,
        custom=custom,
        records=records,
        spend=spend,
        web_search=web_search,
        tiers=tiers,
        turns=turns,
        wrap_model=wrap_model,
    )
    # `expose=()` means none of the host's own tools reach the model: the port's three verbs are
    # the whole surface, so `search` never sits beside `search_workflows` for the agent to thrash
    # between.
    port = WorkflowContract[Runtime](
        source=collection_toolset(library),
        vocabulary=WORKFLOW_COLLECTION,
        expose=(),
        registry=registry,
        saved_by="agent:harness-author",
        # The stamps a save records and a fetch compares.
        environment=environment_stamps(custom),
        save_requires_approval=save_requires_approval,
        # With the evidence log on the port, search hits and fetches carry the evidence each
        # workflow has earned. Every runner writes the same file.
        evidence_log=records.evidence,
        # `save_workflow` stays hidden until the model loads the `saving-a-workflow` skill, the
        # same guidance it needs to draft a workflow worth saving in the first place.
        save_capability_id="saving-a-workflow",
        # The same bar the Save button answers to, so the agent's own write cannot land a
        # workflow the person's write would have been refused.
        save_guard=_described_or_refused(registry),
    )
    workflows = WorkflowProcess(
        port=port,
        records=records,
        reporter=reporter,
        scheduler=scheduler,
        sessions=sessions,
        topics=topics,
        run=run,
        knowledge_bases=knowledge_base_names(studio_client),
    )
    # Every functional plugin arrives behind its own switch (`plugins.py`): the owner's flip reads
    # per request, so `harness plugins` reaches the next turn with no rebuild. The SQL plugin
    # starts off, so the read tools are the only retrieval surface until the owner turns it on.
    knowledge_base_reads = Switched(
        KnowledgeBase[Runtime](knowledge_base=reader, only=CHAT_KNOWLEDGE_BASE_PLUGINS), plugin="knowledge-base", state=state
    )
    sql = Switched(await stack.enter_async_context(knowledge_base_capability(studio_client, reading=reading)), plugin="sql", state=state)
    leveled = Switched(disclosure, plugin="leveled-reads", state=state)
    digest = Switched(
        Toolset[Runtime](
            toolset=FunctionToolset[Runtime](
                [digest_now_tool(StudioKnowledgeBaseRefresher(studio_client), launch=_launcher(records, topics, run))]
            )
        ),
        plugin="digest-now",
        state=state,
    )
    project = project_config(CHECKOUT)
    # One project namespace for this checkout's own memory, and a plugin-per-mount memory config
    # read fresh per call, as `disabled_plugins()` is, so a change to `.aifactory/config.json`
    # reaches the next turn with no restart.
    project_namespace = f"project-{sanitize_segment(project_id_of(CHECKOUT))}"
    # One `Switched` around all three mounts, not one each: `Switched.id` is `f"plugin-{plugin}"`, so
    # three separate wrappers on the same plugin name would mint the same capability id three times,
    # which `Agent.__init__` refuses as a duplicate. `CombinedCapability` groups the three as one
    # capability tree, and each mount's own `for_run` (the global one's per-turn gate included) still
    # resolves on its own underneath it.
    memory = (
        Switched(
            CombinedCapability[Runtime](
                capabilities=list(
                    memory_mounts(
                        agent_memory_store,
                        project_namespace=project_namespace,
                        global_memory_enabled=lambda: project_config(CHECKOUT).memory.global_memory,
                    )
                )
            ),
            plugin="memory",
            state=state,
        ),
    )
    # The companion's hands: the verbs are declared here, and the acts answer through the route handlers, so a companion
    # act and a station click are the same act. `build_agent` mounts them behind the `follow-me` plugin, in the master
    # session only.
    console_settings = SelfAuthoringDeployment(state=state, self_authoring=self_authoring, run=run, tiers=tiers)
    console = RouteConsole(settings=console_settings, records=records, session_log=session_log, sessions=sessions, topics=topics)
    follow_me = FollowMe(verbs=verb_toolset(), acts=act_toolset(console, console, console))
    hint = (
        skill_hint_capability(skill_hint, infrastructure.openrouter, state=state, project=project, spend=spend)
        if skill_hint is not None and infrastructure is not None
        else None
    )
    agent = build_agent(
        TierModel("orchestrator", tiers),
        workflows,
        knowledge_base_reads,
        state=state,
        custom=custom,
        web_search=web_search,
        store=transcript,
        parameters=parameters,
        external_agents=external_agents,
        project=project,
        disclosure=(leveled,),
        sql=sql,
        worker_reads=KnowledgeBase[Runtime](knowledge_base=reader),
        tiers=tiers,
        memory=memory,
        digest=digest,
        follow_me=follow_me,
        spend=spend,
        skill_hint=hint,
    )
    runtime = Runtime(
        registry=registry, library=library, agent=agent, active=active, session_log=session_log, store=transcript, state=state
    )
    # This process's own session gets its decider wired here, because the registry `build_environment`
    # returns exists before any `Runtime` does, and the decider needs one to run its turn against.
    # Every pooled session gets the same wiring at hydration time, through the `decide_gate` the pool
    # was built with.
    registry.decide_gate = orchestrator_decider(runtime, sessions)
    return runtime


async def ensure_run_engine(stack: AsyncExitStack, state: StateSettings, settings: RunSettings) -> ServerOutcome | None:
    """Make the configured engine reachable before anything tries to run on it.

    Args:
        stack: Where a local Temporal server this process starts is entered, so it goes down with the app.
        state: Whose home keeps that server's history, which outlives the app.
        settings: Which engine runs workflows, and where Temporal listens.

    Returns:
        How reaching or starting the Temporal server went, or `None` on the in-process engine, which
        needs no server.
    """
    if settings.run_engine != "temporal":
        running.install_engine_reachable(reachable=True)
        return None
    outcome = await stack.enter_async_context(ensure_server(settings, state.home))
    running.install_engine_reachable(reachable=outcome.reachable)
    return outcome


async def serve_durable_runs(stack: AsyncExitStack, registry: StepRegistry, outcome: ServerOutcome | None, settings: RunSettings) -> bool:
    """On the durable engine, make this process also the worker that executes the steps.

    An unreachable engine serves nothing rather than half of something: `running.start`
    refuses a launch, and a scheduled fire is stamped with why.

    Args:
        stack: Where the worker is entered, so it stops with the app.
        registry: The registry of steps the worker executes.
        outcome: What `ensure_run_engine` reported, `None` on the in-process engine.
        settings: Which engine runs workflows, and Temporal's address, namespace and task queue.

    Returns:
        Whether this process now serves durable runs: false on the in-process engine and on an
        unreachable outcome, true once the worker is entered.
    """
    if settings.run_engine != "temporal":
        return False
    if outcome is not None and not outcome.reachable:
        _logger.warning("nothing serves temporal at {address}, so this process serves no durable runs", address=outcome.address)
        return False
    client = await temporal_connect(settings.temporal_address, namespace=settings.temporal_namespace)
    await stack.enter_async_context(temporal_worker(client, registry, task_queue=settings.temporal_task_queue))
    _logger.info("serving durable runs on {queue}", queue=settings.temporal_task_queue)
    return True


def restore_waiting_run(session: Runtime, records: RunRecords, topics: TopicSink) -> None:
    """Relaunch the run a process death left in the run log, so its gate goes back to the person.

    A run in a chat session is not relaunched. It waits paused for the person to resume it. A scheduled
    run relaunches whatever its session.
    The logged steps replay without running again, and the restored run keeps the dead run's repairs,
    gate decisions, reaches and settled steps. A workflow that no longer validates here has its run
    log dropped with a warning. Call this only at boot: it first sweeps the reviewed values of every
    run no run log still names.

    Args:
        session: The session whose run log is read and whose registry the workflow is validated against.
        records: Where the restored run's records land.
        topics: Where the restored run announces that it moved.
    """
    path = run_log_path(session)
    if swept := sweep_orphan_reviewed(path):
        _logger.info("swept {n} reviewed-value subtree(s) left by a run nothing here still holds", n=swept)
    _relaunch_logged_run(session, path, records, topics)


def _started_at(run_id: str) -> str:
    """When the run `run_id` started, as a launch writes it, or `""` when the id carries no start time."""
    try:
        return datetime.strptime(f"{run_id.split('-', maxsplit=1)[0]}+0000", "%Y%m%dT%H%M%S%f%z").isoformat()
    except ValueError:
        return ""


def _relaunch_logged_run(session: Runtime, path: Path, records: RunRecords, topics: TopicSink) -> None:
    """Relaunch the run the log at `path` holds in `session`, pause it, or drop the log when its workflow no longer validates."""
    task = session.active.task
    if task is not None and not task.done():
        _logger.warning("leaving {path} in place: its session already has a run in flight", path=str(path))
        return
    waiting = load_waiting(path)
    if waiting is None:
        return
    report = validate(waiting.spec, session.registry)
    if not report.ok:
        _logger.warning("dropping the run log: {workflow} no longer validates here", workflow=waiting.workflow_id)
        path.unlink(missing_ok=True)
        return
    if waiting.session_kind != "runner" and not waiting.runner.startswith(running.SCHEDULE_RUNNER_PREFIX):
        session.active.paused_by_restart = True
        session.active.workflow_id = waiting.workflow_id
        session.active.run_id = waiting.run_id or None
        session.active.run_title = waiting.run_title
        session.active.result = "A restart stopped this run. Resume it to carry on."
        session.active.started_at = waiting.started_at or _started_at(waiting.run_id)
        session.active.gate_mode = waiting.gate_mode
        session.active.gate_timeout = waiting.gate_timeout
        session.active.progress.reset()
        for node_id in revive_values(waiting.spec, session.registry, waiting.values):
            session.active.progress.replayed(node_id)
        _logger.info("pausing {workflow} for the person to resume it", workflow=waiting.workflow_id)
        return
    memo = revive_values(waiting.spec, session.registry, waiting.values)
    dead_values = reviewed_values(path, waiting.run_id) if waiting.run_id else None
    carried = carried_state(
        waiting, kept=frozenset(node.id for node in waiting.spec.nodes), reviewed=dead_values.captured() if dead_values is not None else ()
    )
    _logger.info("restoring {workflow}: {n} logged steps replay", workflow=waiting.workflow_id, n=len(memo))
    running.launch(
        session,
        records=records,
        topics=topics,
        spec=waiting.spec,
        workflow_id=waiting.workflow_id,
        params=waiting.params,
        memo=memo,
        carried=carried,
        run_title=waiting.run_title,
        gate_mode=waiting.gate_mode,
        gate_timeout=waiting.gate_timeout,
        runner=waiting.runner,
        session_kind=waiting.session_kind,
    )
    if dead_values is not None:
        # The restored run has re-filed everything it still owes a repair over, so the run that
        # died has no reader left and its subtree need not wait for the next boot's sweep.
        dead_values.clear()


def run_log_folders(checkout: Path, checkout_state: StateSettings, roots: Iterable[Path]) -> tuple[Path, ...]:
    """Where each registered project's pooled sessions keep their run logs, found without creating anything.

    The checkout's own root maps to `checkout_state`'s sessions folder, and every other root to its
    `.aifactory/state/sessions`. A root that is no longer a directory is left out.

    Args:
        checkout: The harness checkout.
        checkout_state: The checkout's own state home.
        roots: The registered projects' roots, the checkout's among them.

    Returns:
        One sessions folder per root that still exists, in the order given.
    """
    folders: list[Path] = []
    for root in roots:
        if root.resolve() == checkout.resolve():
            folders.append(checkout_state.sessions)
        elif root.is_dir():
            folders.append(StateSettings(home=aifactory_state(root)).sessions)
    return tuple(folders)


async def restore_waiting_runs(pool: SessionPool, folders: Iterable[Path], records: RunRecords, topics: TopicSink) -> None:
    """Relaunch every pooled session's run that a process death left in a run log in `folders`.

    A run in a chat session is paused for the person to resume instead. A scheduled run relaunches
    whatever its session. Call this only at boot, after the process's own session is restored. A run
    log whose session no longer opens is deleted. A run log that is not its session's own is left in place, unless the
    session is the process's own idle one, which takes the log over. A session with a run in flight
    keeps it. A log that raises is left in place and never stops the others. Each folder's orphaned
    reviewed values are swept once, before any run in it is relaunched, so a relaunched run's
    re-filed values are never swept.

    Args:
        pool: The sessions this process holds, which opens each run log's session.
        folders: The folders holding pooled sessions' run logs.
        records: Where each relaunched run's records land.
        topics: Where each relaunched run announces that it moved.
    """
    for folder in folders:
        logs = sorted(folder.glob(f"*{WAITING_RUN_SUFFIX}"))
        relaunching: list[tuple[Runtime, Path, bool]] = []
        for path in logs:
            try:
                found = await _opened_run_log(pool, path)
            except Exception:
                _logger.exception("leaving {path} in place: opening its session failed", path=str(path))
                continue
            if found is not None:
                relaunching.append(found)
        if logs and (swept := sweep_orphan_reviewed(logs[0])):
            _logger.info("swept {n} reviewed-value subtree(s) in {folder}", n=swept, folder=str(folder))
        for runtime, path, adopt in relaunching:
            log_path = path
            try:
                if adopt:
                    adopted = _adopted(runtime, path)
                    if adopted is None:
                        continue
                    log_path = adopted
                _relaunch_logged_run(runtime, log_path, records, topics)
            except Exception:
                _logger.exception("relaunching the run log now at {path} failed, and it stays there", path=str(log_path))


async def _opened_run_log(pool: SessionPool, path: Path) -> tuple[Runtime, Path, bool] | None:
    """The session a pooled run log belongs to, the log's path and whether the session must adopt it, or `None` to skip it."""
    session_id = path.name.removesuffix(WAITING_RUN_SUFFIX)
    try:
        runtime = await pool.get(session_id)
    except (KeyError, MissingProjectRootError):
        _logger.warning("dropping the run log of session {session_id}: the session no longer opens", session_id=session_id)
        _drop(path)
        return None
    if run_log_path(runtime) == path:
        return runtime, path, False
    task = runtime.active.task
    if runtime is pool.default and (task is None or task.done()) and not runtime.active.paused_by_restart:
        return runtime, path, True
    _logger.warning("leaving {path} in place: it is not the run log of session {session_id}", path=str(path), session_id=session_id)
    return None


def _drop(path: Path) -> None:
    """Delete the run log at `path`, if it is still there."""
    path.unlink(missing_ok=True)


def _adopted(runtime: Runtime, path: Path) -> Path | None:
    """Move the pooled run log at `path` and its reviewed values to `runtime`'s own run log, or answer `None` when it holds a run."""
    task = runtime.active.task
    if (task is not None and not task.done()) or runtime.active.paused_by_restart:
        _logger.warning("leaving {path} in place: the process's own session already holds a run", path=str(path))
        return None
    waiting = load_waiting(path)
    if waiting is None:
        msg = "the run log did not load"
        raise ValueError(msg)
    destination = run_log_path(runtime)
    destination.parent.mkdir(parents=True, exist_ok=True)
    path.replace(destination)
    reviewed = path.parent / "reviewed" / waiting.run_id
    if waiting.run_id and reviewed.is_dir():
        moved = destination.parent / "reviewed" / waiting.run_id
        moved.parent.mkdir(parents=True, exist_ok=True)
        reviewed.replace(moved)
    return destination


def harness_surface(settings: SelfAuthoringDeployment, session_log: SessionEvents, sessions: Sessions) -> SelfAuthoringController:
    """The self-authoring surface a console act runs through, resolved the way a request resolves it.

    Args:
        settings: The deployment settings the surface acts on.
        session_log: Where the surface narrates what it changed.
        sessions: The sessions an act may reach.

    Returns:
        The self-authoring controller, built as a request gets it.
    """
    return SelfAuthoringController(settings, session_log, sessions)


class RouteConsole:
    """The console acts, each answered in process by the very route handler its AI Factory Studio button calls.

    Implements the follow-me ports (`SettingsActs`, `RunActs`, `CardActs`) over the route helpers, so an
    act the companion performs and the same click in a station cannot drift: the same guard, the same log entry,
    the same words on refusal. Answers are sentences, never exceptions: a refusal is an answer the model relays.
    """

    def __init__(
        self,
        *,
        settings: SelfAuthoringDeployment,
        records: RunRecords,
        session_log: SessionEvents,
        sessions: PooledSessions,
        topics: TopicSink,
    ) -> None:
        """Build a console whose acts run through the route handlers.

        Args:
            settings: The deployment settings every act works on.
            records: Where a run an act touches writes its records.
            session_log: Where an act narrates what it did.
            sessions: How an act reaches the session its id names. The pool is resolved when the act
                runs, never here.
            topics: Where a run an act starts or resumes announces that it moved.
        """
        self._settings = settings
        self._records = records
        self._session_log = session_log
        self._sessions = sessions
        self._topics = topics

    async def _surface(self) -> SelfAuthoringController:
        """The self-authoring surface this console's own acts run through."""
        return harness_surface(self._settings, self._session_log, await self._sessions.pool())

    async def _default(self) -> Runtime:
        """The process's own session, which an unscoped act works on."""
        return (await self._sessions.pool()).default

    async def set_model(self, model_id: str, tier: Tier = "orchestrator") -> str:
        """Switch the model a tier answers with, returning the sentence to relay.

        The tier keeps its current thinking setting.

        Args:
            model_id: The model the tier switches to.
            tier: The model tier to switch.

        Returns:
            The confirmation, or the route's refusal.
        """
        try:
            body = TierBody(model=model_id, thinking=tier_in_effect(self._settings.tiers, tier).setting.thinking)
            await (await self._surface()).put_tier(tier, body)
        except HTTPException as exc:
            return f"Refused: {exc.detail}"
        return f"{tier} set to {model_id}."

    async def select_profile(self, name: str | None) -> str:
        """Put a profile in effect, or clear it with no name, returning the sentence to relay.

        Args:
            name: The profile to put in effect, or `None` to clear the selection.

        Returns:
            The confirmation, or the route's refusal.
        """
        try:
            selected = await (await self._surface()).put_selection(Selection(name=name))
        except HTTPException as exc:
            return f"Refused: {exc.detail}"
        return "Profile cleared, the hand-set plugins govern again." if selected.name is None else f"Profile {selected.name} selected."

    async def toggle_plugin(self, name: str, *, on: bool) -> str:
        """Turn one plugin on or off, returning the sentence to relay.

        Args:
            name: The plugin's name in the catalog.
            on: Whether to turn it on.

        Returns:
            The plugin's new state, or the route's refusal.
        """
        try:
            flipped = (await self._surface()).flip_plugin(name, enabled=on)
        except HTTPException as exc:
            return f"Refused: {exc.detail}"
        return f"Plugin {flipped.name} is {'on' if flipped.enabled else 'off'}."

    async def pause(self, session_id: str) -> str:
        """Pause the run in this session, returning the sentence to relay.

        Args:
            session_id: The session whose run to pause.

        Returns:
            The confirmation, or the route's refusal.
        """
        return await self._on_session(
            session_id, lambda session: pause_session_run(session, self._settings.run), "Paused {workflow}."
        )

    async def resume(self, session_id: str) -> str:
        """Resume the run in this session, returning the sentence to relay.

        Args:
            session_id: The session whose run to resume.

        Returns:
            The confirmation, or the route's refusal.
        """
        return await self._on_session(
            session_id,
            self._resume,
            "Resumed {workflow}.",
        )

    async def stop(self, session_id: str) -> str:
        """Stop the run in this session, returning the sentence to relay.

        Args:
            session_id: The session whose run to stop.

        Returns:
            The confirmation, or the route's refusal.
        """
        return await self._on_session(
            session_id,
            lambda session: stop_session_run(session, self._settings.run, self._records),
            "Stopped {workflow} for good.",
        )

    async def set_automode(self, session_id: str, *, on: bool) -> str:
        """Turn this session's automode on or off, returning the sentence to relay.

        Args:
            session_id: The session whose automode to set.
            on: Whether to turn automode on.

        Returns:
            The confirmation, or the route's refusal.
        """
        return await self._on_session(
            session_id,
            lambda session: set_session_automode(session, self._settings.run, on=on),
            "Automode on." if on else "Automode off.",
        )

    async def decide(self, session_id: str, *, approve: bool, feedback: str) -> str:
        """Decide the gate waiting in this session, returning the sentence to relay.

        Args:
            session_id: The session whose waiting gate to decide.
            approve: Whether to approve the gate.
            feedback: What the decision says to the run.

        Returns:
            The confirmation, or the route's refusal.
        """
        return await self._on_session(
            session_id,
            lambda session: decide_session_gate(session, self._settings.run, approve=approve, feedback=feedback),
            "Approved the gate." if approve else "Rejected the gate.",
        )

    async def archive(self, workflow_id: str) -> str:
        """Take the workflow out of the library, returning the sentence to relay.

        Args:
            workflow_id: The workflow to archive.

        Returns:
            The confirmation, or the route's refusal.
        """
        try:
            await browsing.archive_workflow(await self._default(), workflow_id)
        except LibraryError as exc:
            return f"Refused: {exc}."
        return f"Archived {workflow_id}."

    async def restore(self, workflow_id: str) -> str:
        """Put the archived workflow back in the library, returning the sentence to relay.

        Args:
            workflow_id: The archived workflow to restore.

        Returns:
            The confirmation, or the route's refusal.
        """
        try:
            await browsing.restore_workflow(await self._default(), workflow_id)
        except LibraryError as exc:
            return f"Refused: {exc}."
        return f"Restored {workflow_id}."

    async def redescribe(self, workflow_id: str, *, title: str, summary: str, applicability: str) -> str:
        """Rewrite the workflow's card, returning the sentence to relay.

        This act rewrites only the fields it is given and blanks nothing.

        Args:
            workflow_id: The workflow whose card to rewrite.
            title: The card's new title.
            summary: The card's new summary. A blank one keeps the current summary.
            applicability: When the workflow applies. A blank one keeps the current text.

        Returns:
            The confirmation, or the route's refusal.
        """
        patch = WorkflowCardPatch(title=title, summary=summary or None, applicability=applicability or None)
        try:
            await browsing.redescribe_workflow(await self._default(), workflow_id, patch)
        except (CardRefusedError, PresentationRefusedError, LibraryError) as exc:
            return f"Refused: {exc}."
        return f"Redescribed {workflow_id}."

    async def _resume(self, session: Runtime) -> None:
        """Resume the run in `session` from its own draft or logged workflow."""
        await resume_session_run(session, spec_yaml="", records=self._records, topics=self._topics)

    async def _on_session(
        self, session_id: str, act: Callable[[Runtime], Awaitable[None] | None], done: str
    ) -> str:
        """Run `act` against the named session, answering `done` on success and the refusal's own words on a refusal."""
        try:
            session = await (await self._sessions.pool()).get(session_id)
        except KeyError:
            return f"Refused: {UNKNOWN_SESSION}."
        except MissingProjectRootError as exc:
            return f"Refused: {exc}."
        try:
            answer = act(session)
            if isinstance(answer, Awaitable):
                await answer
        except (RunStateRefusedError, RunGraphRefusedError, WorkflowNotRunnableError) as exc:
            return f"Refused: {exc}."
        return done.format(workflow=session.active.workflow_id)
