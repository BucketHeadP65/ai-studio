"""The authoring agent, assembled entirely from capabilities.

Every domain surface arrives as a capability carrying its own tools and its own instructions: the
workflow process (`harness.features.authoring.process`), the knowledge bases, the sandbox. This
module registers no tools of its own.
"""

from __future__ import annotations

import os

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from core.ai.dedup import DedupToolResults
from core.ai.empty_answer import EmptyAnswerRetry
from core.ai.filesystem import UnnumberedFileSystem
from core.ai.format_maps.contracts import FormatMaps, MappedTool, command_starts
from core.ai.history.contracts import RequestParametersSink, recording_capabilities
from core.ai.shell import PathGuardedShell
from core.ai.tool_output_limits import ToolOutputLimits
from core.ai.voice import RuntimeVoiced
from core.observability import get_logger
from engram.skills.contracts import SKILLS as ENGRAM_SKILLS
from pydantic_ai import Agent
from pydantic_ai.capabilities import Capability, CombinedCapability, DynamicCapability, IncludeToolReturnSchemas, Toolset
from pydantic_ai.models.anthropic import AnthropicCompaction
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai_harness import CodeMode
from pydantic_ai_harness.compaction import ClearToolResults, DeduplicateFileReads, SummarizingCompaction, TieredCompaction, WarnNearLimits
from pydantic_ai_harness.logfire import ManagedPrompt
from pydantic_ai_harness.pydantic_ai_docs import PydanticAIDocs
from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS, Shell
from pydantic_ai_harness.skills import Skills
from pydantic_ai_harness.spend import Budget, SpendLimits
from pydantic_ai_harness.tool_output_limits import Band, Spill, Truncate
from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts

from harness.adapters.contracts import (
    ACT_OUTPUT_LIMIT,
    ACT_PREVIEW,
    RecordHistorySource,
    chat_workspace,
    content_cache,
    price_response,
    spill_store,
    web_search_toolset,
)
from harness.domain.contracts import NOT_IN_SANDBOX, ORCHESTRATOR_SKILLS, PROGRAM_OUTPUT_LIMIT, SANDBOX, SessionTranscript
from harness.features.authoring.deferred_tools import NamedToolDeferral
from harness.features.authoring.instructions import INSTRUCTIONS
from harness.features.authoring.steering import SteerDelivery
from harness.features.authoring.workers import (
    DELEGATE_TOOL,
    EXTERNAL_AGENT_TOOL,
    FILES_DENIED_PATTERNS,
    SCRIPT_TOOL,
    delegation_plugins,
    external_agent_plugin,
)
from harness.features.followme.contracts import CONSOLE_ACTS, VERBS, ConsoleApprovals, master_only
from harness.features.projects.contracts import NoteOverlay, Preferences
from harness.features.selfauthoring.contracts import (
    AGENT_GUIDANCE,
    CUSTOM_ITEM_DIRECTORIES,
    PENDING_GUIDANCE,
    STEP_GUIDANCE,
    AgentCreation,
    FileCustomCapabilities,
    PendingCapabilityCreation,
    ProfileCreation,
    ProfileVoice,
    SkillsPlugin,
    StepCreation,
    activations_under,
    known_skill_names,
    promoted_capability_types,
    selected_profile_skills,
)
from harness.features.sessions.contracts import CompactionConfig, FrictionFlagging, ProjectConfig, project_config
from harness.plugins import Switched
from harness.sandbox import SandboxDiscipline
from harness.session import Runtime, SessionContextUsage, SessionConversationSearch
from harness.settings import CHECKOUT, SKILLS, WORKSPACE, StateSettings
from harness.vendor.loop_detection import LoopDetection

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.models import Model
    from pydantic_ai.tools import RunContext

    from harness.adapters.contracts import ModelTiers
    from harness.features.authoring.process import WorkflowProcess
    from harness.settings import ExternalAgentRegistry, WebSearchSettings

_logger = get_logger(__name__)

AGENT_NAME = "harness-author"
"""The authoring agent's one name: the transcript records every run under it."""

CHAT_BUDGET = 32
"""Model requests one chat turn may spend. It is both the route's hard limit and the point the agent
is warned at."""

ACTS: tuple[str, ...] = (
    "run_workflow",
    "stop_run",
    "save_workflow",
    # `adapt_workflow` and the four `write_custom_*` tools each carry a whole document as their argument,
    # which crashes the sandbox as a literal inside `run_code`.
    "adapt_workflow",
    "write_custom_capability",
    "write_custom_step",
    "write_custom_agent",
    "write_custom_profile",
    "run_command",
    "write_file",
    "edit_file",
    "create_directory",
    DELEGATE_TOOL,
    SCRIPT_TOOL,
    EXTERNAL_AGENT_TOOL,
    *CONSOLE_ACTS,
    *VERBS,
)
"""Tools whose call is itself the event: they run work, wait on a person, write a file, or shell out.

`CodeMode` never folds these into a program, so each one lands in the transcript as its own named
call. The console acts and the page verbs count as acts too."""

KNOWLEDGE_BASE_FETCH = "fetch"
"""The knowledge bases' leveled read, always called directly and never through a program, so the
model itself reads the level summaries."""

READERS: tuple[str, ...] = ("read_pyai_docs", "search_conversation_history", "read_file")
"""Look-ups whose answer is prose the model reads (docs pages, past-conversation excerpts, file
contents), always called directly and never through a program."""

SQL_TOOLS: tuple[str, ...] = ("run_sql", "describe_schema")
"""The SQL plugin's two tools, under engram's default names. A session setting decides whether they
fold into `run_code`, which they do by default."""

SELF_AUTHORING_CAPABILITY_ID = "self-authoring"
"""The deferred capability every self-authoring tool below is tied to, whatever plugin mounted it."""

SELF_AUTHORING_TOOLS: tuple[str, ...] = (
    "write_custom_capability",
    "list_custom_capabilities",
    "disable_custom_capability",
    "write_custom_step",
    "list_custom_steps",
    "write_custom_agent",
    "list_custom_agents",
    "write_custom_profile",
    "list_custom_profiles",
    "list_skills",
)
"""Every tool the four author-* plugins and the skills-listing plugin contribute between them.

Each plugin still decides whether its own tools exist. This list ties the ones that do to one
deferred capability, so loading it makes every self-authoring tool callable at once."""

CONTEXT_BOUND: tuple[str, ...] = (*ACTS, SANDBOX, *NOT_IN_SANDBOX, KNOWLEDGE_BASE_FETCH, *READERS, *SQL_TOOLS)
"""Tools whose return lands in this agent's context, the only ones the output limits may spill.

Every other tool runs inside `CodeMode`, where a spill would replace a value mid-program with a
notice and break the program. `load_capability` is left out: a shortened load no longer counts as
loaded, so the capability's tools stay locked and its instructions arrive as a preview."""

def _shell_workspace_instructions() -> str:
    """The shell plugin's situating sentence, a function so it renders after the identity instructions."""
    return f"Commands run in {WORKSPACE}. Reach for the shell to find, search and run things. To read or change one file, use the files plugin."


def _files_workspace_instructions() -> str:
    """The files plugin's situating sentence, a function so it renders after the identity instructions."""
    return (
        f"File paths are relative to {WORKSPACE}, nothing outside it is reachable. "
        "Reach for these tools to read or change one file you can already name. To find it first, use the shell."
    )


def build_agent(
    model: Model,
    workflows: WorkflowProcess,
    knowledge_base_reads: AbstractCapability[Runtime],
    *,
    state: StateSettings,
    custom: Path,
    web_search: WebSearchSettings,
    store: SessionTranscript,
    parameters: RequestParametersSink,
    external_agents: ExternalAgentRegistry,
    project: ProjectConfig | None = None,
    disclosure: Sequence[AbstractCapability[Runtime]] = (),
    sql: AbstractCapability[Runtime] | None = None,
    sql_in_sandbox: Callable[[], bool] | None = None,
    worker_reads: AbstractCapability[Runtime] | None = None,
    tiers: ModelTiers | None = None,
    memory: Sequence[AbstractCapability[Runtime]] = (),
    digest: AbstractCapability[Runtime] | None = None,
    follow_me: AbstractCapability[Runtime] | None = None,
    spend: AbstractCapability[Runtime] | None = None,
    skill_hint: AbstractCapability[Runtime] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Agent[Runtime, str | DeferredToolRequests]:
    """Build the authoring agent for one session, since each session composes its own environment.

    `model` may be any `Model`, a test's `TestModel` included. The agent answers in plain prose, or
    ends its turn deferred with a client-executed verb or an approval outstanding. Every entry must
    handle both, because `ConsoleApprovals` can make any tool ask the person first.

    Args:
        model: Which model answers.
        workflows: The workflow process as one capability: the library's tools, the validator, the
            runner and their instructions.
        knowledge_base_reads: The knowledge bases' read plugin, `search` and the leveled `fetch`.
        state: Where this deployment's durable process state is written, which every plugin switch
            is read from.
        custom: The owner-reviewed custom-item root the four authoring plugins write into.
        web_search: Whether `web_search` reaches Tavily. A section with no key in it searches
            keyless.
        store: The transcript this session's runs are written to and searched in.
        parameters: Where every request's tool definitions and model settings are recorded, beside
            the messages the transcript already keeps.
        external_agents: Which ACP agents this session may spawn, read per call.
        project: The context policy and skill directories this session opens with. `None` reads
            `project_config(CHECKOUT)`, the harness checkout's own configuration.
        disclosure: The leveled-fetch plugin: the `fetch` toolset capability and its progressive
            disclosure. Empty mounts neither.
        sql: The SQL plugin over the same knowledge bases (`run_sql` and `describe_schema`). `None`
            leaves the read tools the whole retrieval surface.
        sql_in_sandbox: Whether the SQL tools fold into `run_code`, read per request. `None` always
            folds them.
        worker_reads: The knowledge bases' read plugin built for the workers, a separate instance
            from the agent's own `knowledge-base` plugin. `None` gives workers no reads.
        tiers: How a model tier name becomes a model, for delegation and the context gauge. `None`
            mounts no delegation plugin and leaves the gauge on its default window.
        memory: The standing-memory mounts (global, project and the session scratchpad), already
            behind the `memory` plugin. Empty mounts none.
        digest: The `digest_now` plugin, built by the caller over its own studio API client. `None`
            mounts nothing.
        follow_me: The follow-me companion's plugin, mounted only in a master session. `None`
            mounts nothing.
        spend: The spend limits. `None` builds this session's own, priced through the model
            catalog. Pass the instance a workflow step's agent mounts, so one day limit covers both.
        skill_hint: The skill hint, mounted beside the skills behind the `skills` switch. `None` mounts none.
        environ: The environment the session's command environment is taken from, through `workspace_env`. `None`
            reads this process's own.

    Returns:
        The agent, answering in plain text or with a deferred turn.
    """
    resolved_project = project if project is not None else project_config(CHECKOUT)
    rides_sandbox = sql_in_sandbox if sql_in_sandbox is not None else _always
    # Read once per session, not per turn: a `harness promote`/`demote capability` while a
    # session is already running takes effect on the next session, not this one.
    promoted = promoted_capability_types(FileCustomCapabilities.read_only(custom / CUSTOM_ITEM_DIRECTORIES["capability"]))
    # The skill names `write_custom_agent` and `write_custom_profile` accept: the registry plus this
    # project's own libraries, read once per session like `promoted`.
    known_skills = known_skill_names(CHECKOUT, resolved_project)
    # The one log every authoring plugin here records through, the same one `harness activate` writes.
    activations = activations_under(state)
    # The spend limits. An unpriced response counts as free but is still tallied, and a breach fails
    # the turn. Every worker run mounts this same instance.
    spend = (
        spend
        if spend is not None
        else SpendLimits[Runtime](
            budgets=[Budget(usd=Decimal(2), window="run"), Budget(usd=Decimal(25), window="day", warn_at=0.8)],
            price=price_response,
        )
    )
    command_environ = os.environ if environ is None else environ
    delegation = (
        delegation_plugins(tiers=tiers, reads=worker_reads, spend=spend, state=state, environ=command_environ) if tiers is not None else None
    )
    capabilities: list[AbstractCapability[Runtime]] = [
            # The workspace every tool of the session runs in: the shell, the files, the skills, the memory
            # and the capability authoring. It stays mounted whichever plugin switches are closed.
            chat_workspace(WORKSPACE, environ=command_environ),
            # What the person sends mid-turn (the steer route) reaches the model inside the
            # running turn, at its next request or as a redirect of a run about to end.
            SteerDelivery(),
            workflows,
            knowledge_base_reads,
            *([sql] if sql is not None else []),
            *disclosure,
            # Only the read verbs: their results are documented pydantic types.
            IncludeToolReturnSchemas(tools=["search", KNOWLEDGE_BASE_FETCH]),
            # Only the leveled read, since a call inside a program bypasses this wrapper. It must stay
            # listed before `CodeMode`, or a program can receive a dedup note where it expected a listing.
            DedupToolResults(tools=[KNOWLEDGE_BASE_FETCH]),
            Switched(RuntimeVoiced(PydanticAIDocs()), plugin="pydantic-ai-docs", state=state),
            # A model-agnostic web-search tool: Tavily when a key is set, DuckDuckGo (keyless)
            # otherwise (`adapters.websearch`). One name, `web_search`, whichever vendor answers.
            Switched(
                Toolset[Runtime](toolset=web_search_toolset(web_search.tavily_api_key)),
                plugin="web-search",
                state=state,
            ),
            # The capability-authoring plugin. Every `write_custom_capability` write lands pending
            # (disabled) until the owner runs `harness activate capability <name>`. `guidance`
            # replaces the vendor's default text, which describes a write that is active at once.
            Switched(
                RuntimeVoiced(
                    PendingCapabilityCreation[Runtime](
                        directory=custom / CUSTOM_ITEM_DIRECTORIES["capability"], guidance=PENDING_GUIDANCE, activations=activations
                    )
                ),
                plugin="author-capabilities",
                state=state,
            ),
            # The step-authoring plugin. Every `write_custom_step` write lands pending too, and joins the
            # registry only when the next environment is built, never in this run.
            Switched(
                RuntimeVoiced(StepCreation[Runtime](directory=custom / CUSTOM_ITEM_DIRECTORIES["step"], guidance=STEP_GUIDANCE, activations=activations)),
                plugin="author-scripts",
                state=state,
            ),
            # The agent-authoring plugin. Every `write_custom_agent` write lands pending too, and an
            # `AgentStep` can name it the moment it is activated. `custom_capability_types=promoted`
            # lets a custom agent declare a promoted custom capability by name, checked at write time
            # like a built-in one.
            Switched(
                RuntimeVoiced(
                    AgentCreation[Runtime](
                        directory=custom / CUSTOM_ITEM_DIRECTORIES["agent"],
                        guidance=AGENT_GUIDANCE,
                        custom_capability_types=promoted,
                        known_skill_names=known_skills,
                        activations=activations,
                    )
                ),
                plugin="author-agents",
                state=state,
            ),
            # The profile-authoring plugin. A profile groups plugins, custom workflows and standing
            # instructions the owner puts in effect as one act. A write lands pending, and only the
            # owner selects a profile.
            Switched(
                RuntimeVoiced(
                    ProfileCreation[Runtime](
                        directory=custom / CUSTOM_ITEM_DIRECTORIES["profile"], known_skill_names=known_skills, activations=activations
                    )
                ),
                plugin="author-profiles",
                state=state,
            ),
            # The registry of skills, which an agent writing a custom agent or profile lists with
            # `list_skills` before naming skills on it.
            Switched(
                SkillsPlugin[Runtime](root=CHECKOUT, config=resolved_project),
                plugin="list-skills",
                state=state,
            ),
            # One deferred capability for every self-authoring tool. Loading `self-authoring` makes
            # every tool the open plugins mounted callable at once, and closing a plugin still
            # removes its tools. The marker carries no tools of its own. `NamedToolDeferral` ties
            # them to it.
            Capability[Runtime](
                id=SELF_AUTHORING_CAPABILITY_ID,
                defer_loading=True,
                description=(
                    "Author a new pydantic-ai capability, step, agent, or profile at "
                    "runtime: write_custom_capability, write_custom_step, write_custom_agent, write_custom_profile, "
                    "and their list_custom_*/disable_custom_* companions. Each write lands "
                    "pending for the owner to review and activate. List the registry with "
                    "list_skills before naming skills on a step or an agent."
                ),
            ),
            NamedToolDeferral(capability_ids=dict.fromkeys(SELF_AUTHORING_TOOLS, SELF_AUTHORING_CAPABILITY_ID)),
            # Always on: the selected profile's instructions join every request, and nothing is added
            # while no profile is selected.
            ProfileVoice[Runtime](state),
            # Recall over the record the recording capabilities below write. Each turn rebinds the
            # source to its own conversation, so recall never reaches another session's.
            Switched(
                RuntimeVoiced(
                    SessionConversationSearch(
                        RecordHistorySource(store, conversation_id=None),
                        max_matches=resolved_project.compaction.recall_max_matches,
                        # The default source lists nothing. Each run points it at its own conversation.
                        scope="all",
                    )
                ),
                plugin="recall",
                state=state,
            ),
            # The standing memory (global, project, scratchpad), which the caller wraps in one
            # `memory` plugin.
            *memory,
            # `digest_now`: land a source, sync a knowledge base, and start its digest workflow
            # without waiting for it.
            *([digest] if digest is not None else []),
            # The companion's verbs and acts, behind the plugin and mounted only in a master session.
            # Every other session's tools are identical to a build without it.
            *([Switched(follow_me, plugin="follow-me", state=state, only=master_only)] if follow_me is not None else []),
            # The approval gate over every console act, always mounted and inert outside a master
            # session. The schedule tools and the companion's acts both ask the person first through it.
            ConsoleApprovals(),
            # The agent's own friction voice: `flag_friction` files a typed report when a tool,
            # a skill, an instruction, a workflow, or the agent itself ground against a turn.
            Switched(RuntimeVoiced(FrictionFlagging()), plugin="friction", state=state),
            spend,
            # Delegation: the worker and its delegation tiers behind one plugin, the scripted workers
            # behind another. Absent when `tiers` is `None`.
            *(
                [Switched(delegation[0], plugin="delegate", state=state), Switched(delegation[1], plugin="agent-scripts", state=state)]
                if delegation is not None
                else []
            ),
            # Another harness, driven over ACP: one brief, one turn, its progress in this
            # session's session log. The registry says which agents this deployment may spawn.
            Switched(external_agent_plugin(external_agents), plugin="external-agent", state=state),
            Switched(
                _SkillsForSelection(_skill_directories(resolved_project), state)
                if skill_hint is None
                else CombinedCapability[Runtime](capabilities=[_SkillsForSelection(_skill_directories(resolved_project), state), skill_hint]),
                plugin="skills",
                state=state,
            ),
            # Commands run in WORKSPACE (this checkout's parent unless `APP_WORKSPACE` moves it), so a
            # sibling repository is reachable. `max_output_chars` stays above the act band below,
            # which does the clamping with a spill the model can read back. A command naming a path
            # the files plugin denies is refused before it runs.
            Switched(
                CombinedCapability[Runtime](
                    capabilities=[
                        PathGuardedShell[Runtime](
                            shell=Shell[Runtime](denied_env_patterns=LLM_API_KEY_ENV_PATTERNS, default_timeout=20.0, max_output_chars=24_000),
                            denied_path_patterns=FILES_DENIED_PATTERNS,
                        ),
                        Capability[Runtime](instructions=_shell_workspace_instructions),
                    ]
                ),
                plugin="shell",
                state=state,
            ),
            # The files plugin over the same WORKSPACE root as the shell. Writes are acts, and the
            # listing and search tools run inside code mode. Line numbers stay off so a spilled file
            # greps clean. The notes sidecar is denied, so the agent reads a project's notes only
            # through `NoteOverlay`, and every credential file is denied too.
            Switched(
                CombinedCapability[Runtime](
                    capabilities=[
                        UnnumberedFileSystem[Runtime](root_dir=WORKSPACE, denied_patterns=list(FILES_DENIED_PATTERNS)),
                        Capability[Runtime](instructions=_files_workspace_instructions),
                    ]
                ),
                plugin="files",
                state=state,
            ),
            # Appends the owner's unarchived notes below a `read_file` return for an annotated
            # markdown file, changing nothing above the fence. `root` must name the files plugin's
            # own `root_dir`, because a `read_file` path is relative to it.
            # `NoteOverlay.get_ordering()` pins its order, not its place in this list.
            NoteOverlay(root=WORKSPACE),
            # The owner's preferences, always on. Both files are read on every request, so an edit
            # reaches the next turn.
            Preferences(
                global_path=Path.home() / ".aifactory" / "preferences.md", project_path=CHECKOUT / ".aifactory" / "preferences.md"
            ),
            # A large docs page answers with its heading map, and the agent calls again with
            # part='<section>', sliced from the run's cached fetch. A cache miss tells the agent to
            # call again without `part`, and no tool is ever re-run silently. The shell maps only a
            # `git diff` or `git show`, which then answers one part per file.
            FormatMaps[Runtime](
                tools={
                    "read_pyai_docs": "markdown",
                    "run_command": MappedTool(format="diff", when=command_starts("git diff", "git show"), over=4_000),
                },
                cache=content_cache(),
            ),
            CodeMode(tools=lambda _ctx, tool: _in_code_mode(tool.name, rides_sandbox)),
            SandboxDiscipline[Runtime](),
            LoopDetection(on_loop="nudge"),
            # The project's context policy: escalating tiers, or the provider's own compaction.
            # `SessionContextUsage` must stay right after it, so its reading shows what the
            # compactor left.
            _compaction_capability(model, resolved_project.compaction),
            SessionContextUsage(tiers=tiers),
            WarnNearLimits(max_iterations=CHAT_BUDGET),
            ToolOutputLimits(
                bands=[Band(over=ACT_OUTPUT_LIMIT, action=Spill(preview_chars=ACT_PREVIEW, then=Truncate(max_chars=ACT_OUTPUT_LIMIT)))],
                # A program's printed digest gets more room than an act's result. An oversized map
                # or page from `FormatMaps` spills here too, losslessly.
                per_tool={
                    SANDBOX: [
                        Band(over=PROGRAM_OUTPUT_LIMIT, action=Spill(preview_chars=3_000, then=Truncate(max_chars=PROGRAM_OUTPUT_LIMIT)))
                    ],
                },
                store=spill_store(),
                tool_filter=CONTEXT_BOUND,
            ),
            # The transcript, and beside each request's messages the tool definitions and model
            # settings it was sent with.
            *recording_capabilities(store, parameters, agent_name=AGENT_NAME, deps_type=Runtime),
            WarnOnCacheBusts(),
            # Retries an answer that is only whitespace with a prompt that names that mistake.
            EmptyAnswerRetry(),
            # `INSTRUCTIONS` serves when no published version resolves. The label stays pinned,
            # because text that changes per run breaks the provider's prompt cache. `render_template`
            # would fail, since pydantic cannot build a schema for `Runtime`.
            ManagedPrompt[Runtime](name="studio_author", default=INSTRUCTIONS, label="production"),
    ]
    # Plain text, never structured output: a `BaseModel` output makes the provider demand a tool
    # call on every request. `DeferredToolRequests` is always listed, because any tool may need the
    # person's approval, and it never reaches the provider. `EmptyAnswerRetry` above spends one of
    # the two output retries.
    agent: Agent[Runtime, str | DeferredToolRequests] = Agent(
        model,
        deps_type=Runtime,
        output_type=[str, DeferredToolRequests],
        capabilities=capabilities,
        retries={"tools": 3, "output": 2},
        name=AGENT_NAME,
    )
    return agent


def _skill_directories(project: ProjectConfig) -> list[Path]:
    """`SKILLS`, engram's own skills and every configured project skill directory that exists, logging each missing one."""
    directories = [SKILLS, ENGRAM_SKILLS]
    for rel in project.skills:
        path = CHECKOUT / rel
        if path.is_dir():
            directories.append(path)
        else:
            _logger.warning("project config names a skills directory that does not exist: {path}", path=str(path))
    return directories


class _SkillsForSelection(DynamicCapability[Runtime]):
    """The chat agent's skill catalog, rebuilt on every run from the profile in effect."""

    def __init__(self, directories: list[Path], state: StateSettings) -> None:
        self.directories = directories
        self.state = state
        super().__init__(self._resolve, id="skills-for-selection")

    def _resolve(self, ctx: RunContext[Runtime]) -> AbstractCapability[Runtime] | None:
        """The active catalog: the selected profile's own list, or `ORCHESTRATOR_SKILLS` when none is selected."""
        del ctx
        names = selected_profile_skills(self.state)
        include = frozenset(names) if names is not None else frozenset(ORCHESTRATOR_SKILLS)
        return Skills[Runtime](self.directories, include=include)


def _compaction_capability(model: Model, config: CompactionConfig) -> AbstractCapability[Runtime]:
    """The compactor `config.strategy` selects: escalating tiers, or the provider's own."""
    if config.strategy == "provider-native":
        return AnthropicCompaction(token_threshold=config.target_tokens)
    return TieredCompaction(
        # `max_tokens=1` only satisfies each tier's constructor. `TieredCompaction` decides when each tier runs.
        tiers=[
            ClearToolResults(max_tokens=1),
            DeduplicateFileReads(file_key=_file_read_key, max_tokens=1),
            SummarizingCompaction(model=config.summarizer_model or model, max_tokens=1),
        ],
        target_tokens=config.target_tokens,
    )


def _file_read_key(call: ToolCallPart) -> str | None:
    """The path a `read_file` call names, which `DeduplicateFileReads` keys on, or `None` for any other tool."""
    if call.tool_name != "read_file":
        return None
    path = call.args_as_dict().get("path")
    return path if isinstance(path, str) else None


def _always() -> bool:
    return True


def _in_code_mode(name: str, sql_rides: Callable[[], bool]) -> bool:
    """Whether the named tool folds into `run_code`: acts and direct reads never do, and the SQL tools follow `sql_rides`."""
    if name in (*ACTS, *NOT_IN_SANDBOX, KNOWLEDGE_BASE_FETCH, *READERS):
        return False
    if name in SQL_TOOLS:
        return sql_rides()
    return True
