"""The delegates the agent hands tasks to: one generic delegate, three delegation tiers, and the two plugins that offer them.

A delegate is a coding agent with the shell, the checkout's files and the knowledge bases' reads.
An orchestrator briefs it with one self-contained task, and it never sees the orchestrator's conversation.
The delegation tiers `quick`, `standard` and `deep` are the same delegate, each on a model tier the owner sets.
A delegate holds no delegation plugin of its own, so delegation is one level deep.
"""

from __future__ import annotations

import glob
import os
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from acp import RequestError, schema
from core.ai.filesystem import UnnumberedFileSystem
from core.ai.shell import PathGuardedShell
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import DynamicWorkflow, SubAgent, SubAgents
from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS, Shell
from pydantic_ai_harness.subagents import ModelOption

from harness.adapters.contracts import AllowOnce, ExternalAgent, ModelTiers, TierModel, chat_workspace, step_guards
from harness.domain.contracts import ExternalAgentFinished, ExternalAgentStarted, ExternalAgentUpdated
from harness.plugins import Switched
from harness.session import Runtime
from harness.settings import CHECKOUT, CONSOLE_CLOSED_ENV, WORKSPACE, ExternalAgentEntry, ExternalAgentRegistry, StateSettings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from core.ai.workflows.contracts import Tier as ModelTier
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.models import Model
    from pydantic_ai.settings import ModelSettings

    from harness.adapters.contracts import AgentUpdate, ExternalResult, PermissionPolicy, UpdateSink
    from harness.domain.contracts import SessionEvents
    from harness.features.authoring.internal_contracts import PermissionAsker

DELEGATE_TOOL = "delegate_task"
"""The delegation tool's name, the vendor default. `build_agent` keeps it out of code mode."""

SCRIPT_TOOL = "run_agent_script"
"""The script plugin's tool. It must not be the vendor default `run_workflow`, which the workflow process already owns."""

DelegationTier = Literal["quick", "standard", "deep"]
"""The delegation tiers, named for the job the orchestrator routes on."""

DEFAULT_TIER: DelegationTier = "standard"
"""The delegation tier a delegation runs on when the orchestrator passes no `model` key."""

WORKER_BUDGET = 40
"""Model requests one delegate run may spend, apart from the parent's own turn budget."""

WORKER_TIMEOUT_SECONDS = 600.0
"""Wall clock one delegation may take before the parent gets a steering message instead."""

MAX_DELEGATIONS_PER_TURN = 8
"""Delegations to the delegate per parent run."""

MAX_SCRIPT_AGENT_CALLS = 12
"""Delegate runs one script may start, an exact ceiling that holds under concurrent fan-out."""

FILES_DENIED_PATTERNS: tuple[str, ...] = (
    "**/*.threads.json",
    "application_settings.json",
    "**/application_settings.json",
    "**/.env",
    "**/.env.*",
    "**/external-agents.json",
)
"""What every files plugin and the shell beside it refuse, the authoring agent's and a delegate's: a project's notes sidecar, and every credential file, at the root or any depth.

The shared settings file is a credential file, because its `infrastructure` section holds every provider key. The
external agents registry is one too, because each entry's `env` holds that agent's provider keys."""

WORKER_DIRECT_READS: tuple[str, ...] = ("read_file", "run_command", "search_files", "find_files", "list_directory", "search", "fetch")
"""The delegate's tools whose returns can be large. A return over their output limit is spilled."""

WORKER_DESCRIPTION = (
    "A coding worker with the shell, the checkout's files, and the knowledge bases' reads. "
    "Give it one self-contained task with the acceptance check, and it reports what it did and found."
)


def worker_instructions(*, workspace: Path, checkout: Path, files: bool = True) -> str:
    """What a delegate is told: how to work, where its commands start, and where its file tools reach.

    Args:
        workspace: The directory the delegate's commands start in, its parent's workspace.
        checkout: The repository checkout the delegate works on.
        files: Whether the delegate holds file tools. When false the text does not mention them.

    Returns:
        The delegate's instructions.
    """
    where = f"- Your commands start in {workspace}, not in the repository checkout at {checkout}. `cd {checkout}` before you work on the repository."
    if files:
        where += f" Your file tools read and write only inside {checkout}, with paths relative to {workspace}."
    return (
        "You are a worker: one self-contained task, briefed by an orchestrator that sees a conversation you do not.\n"
        f"{where}\n"
        "- Do the task as written. Do not widen it. Where the brief is silent, state the assumption you took and proceed.\n"
        "- Verify what you changed with the check the brief names, or the nearest one the machine offers, and say which you ran.\n"
        "- Report file paths, commands, and results verbatim. A refusal, a failing check, or a missing file is a result: "
        "report it as what it is, never as done.\n"
        "- Finish with the answer the brief asked for, first. The orchestrator relays your report, so everything it needs "
        "must be in it."
    )


def _checkout_patterns(workspace: Path, checkout: Path) -> list[str] | None:
    """The patterns that confine a delegate to `checkout`, its own path included, or `None` when it is outside `workspace`."""
    try:
        relative = checkout.resolve().relative_to(workspace.resolve())
    except ValueError:
        return None
    if not relative.parts:
        return ["**"]
    escaped = glob.escape(relative.as_posix())
    return [escaped, f"{escaped}/**"]


@dataclass(frozen=True)
class Tier:
    """One delegation tier: its key, named for the job, and the model tier it runs on."""

    key: DelegationTier
    tier: ModelTier
    description: str


TIERS: tuple[Tier, ...] = (
    Tier(key="quick", tier="helper", description="mechanical edits, lookups, summaries, running a check, single-file work"),
    Tier(key=DEFAULT_TIER, tier="worker", description="ordinary implementation, review with a clear brief, debugging with a repro"),
    Tier(
        key="deep",
        tier="orchestrator",
        description="design judgment, multi-file changes, hard debugging, the final review before reporting done",
    ),
)
"""The delegation tiers, in listing order. The owner sets the model each model tier runs on, from AI Factory Studio."""


def worker_agent(  # noqa: PLR0913 (one delegate's build facts, which a params object would only rename)
    name: str,
    *,
    model: Model | None,
    settings: ModelSettings | None,
    description: str,
    reads: AbstractCapability[Runtime] | None,
    spend: AbstractCapability[Runtime],
    state: StateSettings,
    workspace: Path = WORKSPACE,
    checkout: Path = CHECKOUT,
    own_environ: Mapping[str, str] | None = None,
) -> Agent[Runtime, str]:
    """One delegate: the shell, the files, the reads it was handed, the step guards, and the parent's spend brake.

    The shell, the files and the reads sit behind the same plugins as the parent's, so closing a
    plugin closes it for every delegate too. The shell refuses a command naming a path the files plugin denies.
    A command in it finds the console closed.

    Args:
        name: The agent's name. In the script catalog it is the function a script calls, one per delegation tier.
        model: The model it runs on. `None` for the delegation roster, where the delegation tier decides
            at delegation time.
        settings: Its model settings, or `None` for the model's own.
        description: What the orchestrator reads to choose it.
        reads: The knowledge bases' read surface built for delegates, or `None` where the deployment mounts none.
        spend: The parent's own spend brake, so the day ceiling covers every delegate too.
        state: The home each plugin switch is read from.
        workspace: Where the delegate's commands start, its parent's workspace.
        checkout: The repository checkout. The delegate's file tools reach only inside it, and it gets no file tools when
            the checkout is not inside `workspace`.
        own_environ: When set, the delegate mounts its own workspace on `workspace`, with the command environment taken
            from this mapping. `None` leaves it to the run, as `SubAgents` hands a child its parent's workspace.

    Returns:
        The delegate.
    """
    allowed = _checkout_patterns(workspace, checkout)
    capabilities: list[AbstractCapability[Runtime]] = [
        *([chat_workspace(workspace, environ=own_environ)] if own_environ is not None else []),
        Switched(
            PathGuardedShell[Runtime](
                shell=Shell[Runtime](
                    env={CONSOLE_CLOSED_ENV: "1"},
                    denied_env_patterns=LLM_API_KEY_ENV_PATTERNS,
                    default_timeout=20.0,
                    max_output_chars=24_000,
                ),
                denied_path_patterns=FILES_DENIED_PATTERNS,
            ),
            plugin="shell",
            state=state,
        ),
        *(
            [
                Switched(
                    UnnumberedFileSystem[Runtime](allowed_patterns=allowed, denied_patterns=list(FILES_DENIED_PATTERNS)),
                    plugin="files",
                    state=state,
                )
            ]
            if allowed is not None
            else []
        ),
        *([Switched(reads, plugin="knowledge-base", state=state)] if reads is not None else []),
        *step_guards(WORKER_BUDGET, direct_reads=WORKER_DIRECT_READS),
        spend,
    ]
    return Agent(
        model,
        deps_type=Runtime,
        name=name,
        description=description,
        instructions=worker_instructions(workspace=workspace, checkout=checkout, files=allowed is not None),
        model_settings=settings,
        capabilities=capabilities,
    )


def delegation_plugins(  # noqa: PLR0913 (one delegation build's facts, which a params object would only rename)
    *,
    tiers: ModelTiers,
    reads: AbstractCapability[Runtime] | None,
    spend: AbstractCapability[Runtime],
    state: StateSettings,
    workspace: Path = WORKSPACE,
    checkout: Path = CHECKOUT,
    environ: Mapping[str, str] | None = None,
) -> tuple[SubAgents[Runtime], DynamicWorkflow[Runtime]]:
    """The two delegation capabilities over one delegate and one table of delegation tiers.

    Neither capability shares the parent's usage counter, so a delegate's requests never count
    against the parent's own turn budget. Each delegate is bounded on its own instead.

    Args:
        tiers: How a model tier name becomes a model.
        reads: The knowledge bases' read surface built for delegates, or `None` where the deployment mounts none.
        spend: The parent's own spend brake.
        state: The home each delegate's plugin switches are read from.
        workspace: Where the delegates' commands start, their parent's workspace.
        checkout: The repository checkout the delegates' file tools are confined to.
        environ: The environment the agent scripts' workers take their command environment from. `None` reads this
            process's own.

    Returns:
        The `delegate` capability and the `agent-scripts` capability.
    """
    models = {tier.key: TierModel(tier.tier, tiers) for tier in TIERS}
    menu: dict[str, ModelOption] = {tier.key: ModelOption(models[tier.key], description=tier.description) for tier in TIERS}
    worker = worker_agent(
        "worker",
        model=None,
        settings=None,
        description=WORKER_DESCRIPTION,
        reads=reads,
        spend=spend,
        state=state,
        workspace=workspace,
        checkout=checkout,
    )
    delegate = SubAgents[Runtime](
        agents=[
            SubAgent(
                worker,
                models=[DEFAULT_TIER, *(tier.key for tier in TIERS if tier.key != DEFAULT_TIER)],
                usage_limits=UsageLimits(request_limit=WORKER_BUDGET),
                timeout_seconds=WORKER_TIMEOUT_SECONDS,
                max_calls=MAX_DELEGATIONS_PER_TURN,
                contain_errors=True,
            )
        ],
        models=menu,
        agent_folders=None,
        forward_usage=False,
        # The id must not be `delegating-work`, which the skill of that name already holds in the same run.
        # It matches the plugin's own name, as `agent-scripts` does below.
        id="delegate",
        description=(
            "Delegate one self-contained task to a worker on a model tier (delegate_task). Read "
            "`delegating-work` for which tier fits and how to brief it."
        ),
        # Never deferred, so the orchestrator sees the tool without loading anything first.
        defer_loading=False,
    )
    scripts = DynamicWorkflow[Runtime](
        agents=[
            worker_agent(
                tier.key,
                model=models[tier.key],
                settings=None,
                description=tier.description,
                reads=reads,
                spend=spend,
                state=state,
                workspace=workspace,
                checkout=checkout,
                own_environ=os.environ if environ is None else environ,
            )
            for tier in TIERS
        ],
        tool_name=SCRIPT_TOOL,
        max_agent_calls=MAX_SCRIPT_AGENT_CALLS,
        forward_usage=False,
        sub_agent_usage_limits=UsageLimits(request_limit=WORKER_BUDGET),
        id="agent-scripts",
        description=(
            "Write a script that calls the tier workers quick, standard, and deep as async functions: "
            "fan-out, chains, votes, and retry loops in one call, only the final value comes back."
        ),
        defer_loading=True,
    )
    return delegate, scripts


EXTERNAL_AGENT_TOOL = "run_external_agent"
"""The external-agent plugin's tool. An act, like the two delegation tools, so it never folds into a program."""

EXTERNAL_UPDATE_SECONDS = 1.0
"""How often at most an external agent's progress reaches the session log. Its own log holds the rest."""

EXTERNAL_UPDATE_CHARS = 200
"""How much of one update's text a log entry carries."""


def _render(request: schema.RequestPermissionRequest) -> str:
    """The permission request as a person reads it: what is being done, and to which paths."""
    call = request.tool_call
    lines = [f"{call.title or call.tool_call_id} ({call.kind or 'other'})"]
    lines.extend(location.path for location in call.locations or ())
    return "\n".join(lines)


def _update_text(update: AgentUpdate) -> str | None:
    """One line saying what the external agent is doing, or `None` for an update that says nothing."""
    if isinstance(update, schema.AgentMessageChunk | schema.AgentThoughtChunk) and isinstance(update.content, schema.TextContentBlock):
        return update.content.text.strip() or None
    if isinstance(update, schema.ToolCallStart | schema.ToolCallProgress):
        return update.title
    return None


@dataclass(frozen=True)
class _GatePolicy:
    """The `gate` policy: every permission request goes to whoever answers gates in this session."""

    ask: PermissionAsker
    agent: str

    async def decide(self, request: schema.RequestPermissionRequest) -> bool:
        """Put the request in front of the plugin's own asker, under a gate id naming the call."""
        return await self.ask(f"external:{self.agent}:{request.tool_call.tool_call_id}", _render(request))


@dataclass
class _Narrator:
    """Records one external agent's progress on the session log, at most one entry per `EXTERNAL_UPDATE_SECONDS`."""

    session_log: SessionEvents
    agent: str
    clock: Callable[[], float]
    _last: float | None = field(default=None, init=False)

    def update(self, update: AgentUpdate) -> None:
        """Record this update when the last one is old enough, else let it pass."""
        text = _update_text(update)
        if text is None:
            return
        now = self.clock()
        if self._last is not None and now - self._last < EXTERNAL_UPDATE_SECONDS:
            return
        self._last = now
        self.session_log.record(ExternalAgentUpdated(agent=self.agent, text=text[:EXTERNAL_UPDATE_CHARS]))


def _resolve_cwd(cwd: str | None) -> Path:
    """The directory an external agent works in: `cwd` resolved, or this checkout."""
    return Path(cwd).expanduser().resolve() if cwd is not None else CHECKOUT


async def _drive_external(  # noqa: PLR0913 (one external run's own six facts, which a params object would only rename)
    entry: ExternalAgentEntry, *, brief: str, cwd: Path, agent: str, ask: PermissionAsker | None, on_update: UpdateSink
) -> ExternalResult:
    """Spawn `entry`'s command and run one turn on it. `ask` of `None` allows every request."""
    policy: PermissionPolicy = AllowOnce() if ask is None else _GatePolicy(ask=ask, agent=agent)
    external = ExternalAgent(command=entry.command, args=entry.args, env=entry.env)
    return await external.run(brief, cwd=cwd, permission=policy, on_update=on_update, timeout_seconds=entry.timeout_seconds)


def run_external_agent_step(
    registry: ExternalAgentRegistry,
    *,
    session_log: SessionEvents,
    ask: PermissionAsker | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[str, str, str | None], Awaitable[str]]:
    """Build the `run_external_agent` step body over the registry of external agents, the session log and the asker.

    Args:
        registry: The registry of external agents. It is read on every call, so an edit reaches the next call.
        session_log: Where the run is recorded.
        ask: How a `gate` entry's permission requests are answered. `None` refuses a `gate` entry
            rather than quietly allowing it.
        clock: The clock that spaces the progress entries on the session log.

    Returns:
        The step body, which takes the agent's name, the brief and the directory it works in, or this checkout when it is `None`.
    """

    async def run_external_agent(agent: str, prompt: str, cwd: str | None = None) -> str:
        """Hand one self-contained brief to another coding agent and report what it did."""
        try:
            configured = registry()
        except (OSError, ValueError) as exc:
            return f"the external-agent registry could not be read. {exc}"
        entry = configured.get(agent)
        if entry is None:
            names = ", ".join(sorted(configured)) or "none"
            return f"no external agent named {agent}. The configured names are {names}."
        if entry.permission == "auto":
            gate: PermissionAsker | None = None
        elif ask is None:
            return (
                f"{agent} runs under the gate policy and this plugin cannot ask anyone here. Drive it from an editor "
                f"over ACP, or set its permission to auto in the registry."
            )
        else:
            gate = ask
        root = _resolve_cwd(cwd)
        if not root.is_dir():
            return f"{root} is not a directory here, so {agent} has nowhere to work."
        session_log.record(ExternalAgentStarted(agent=agent, cwd=str(root)))
        narrator = _Narrator(session_log=session_log, agent=agent, clock=clock)
        try:
            result = await _drive_external(entry, brief=prompt, cwd=root, agent=agent, ask=gate, on_update=narrator.update)
        except (OSError, RequestError) as exc:
            session_log.record(ExternalAgentFinished(agent=agent, stop_reason="failed"))
            return f"{agent} could not run. {exc}"
        session_log.record(ExternalAgentFinished(agent=agent, stop_reason=result.stop_reason, touched_files=result.touched_files))
        touched = ", ".join(result.touched_files) or "no files"
        answer = result.text.strip() or "(it reported no text)"
        return f"{agent} finished ({result.stop_reason}) and touched {touched}.\n\n{answer}"

    return run_external_agent


def external_agent_plugin(registry: ExternalAgentRegistry, *, clock: Callable[[], float] = time.monotonic) -> Toolset[Runtime]:
    """The `external-agent` plugin: one tool that drives another harness over ACP.

    The session log and the gate asker are read off the calling session on every call, never bound here.

    Args:
        registry: The registry of external agents, read on every call.
        clock: The clock that spaces the progress entries on the session log.

    Returns:
        The plugin's toolset.
    """

    async def run_external_agent(ctx: RunContext[Runtime], agent: str, prompt: str, cwd: str | None = None) -> str:
        """Hand one self-contained brief to another coding agent and report what it did.

        The external agent sees this brief and nothing else. It works the directory it is given
        directly, so name the paths and the check it should run in the brief.

        Args:
            ctx: The run context. The session it carries owns the session log and the gate asker.
            agent: A configured agent's name.
            prompt: The whole brief, self-contained.
            cwd: The directory it works in. Omitted, this checkout.
        """
        asker = ctx.deps.gate_asker
        step = run_external_agent_step(
            registry, session_log=ctx.deps.session_log, ask=asker.ask if asker is not None else None, clock=clock
        )
        return await step(agent, prompt, cwd)

    return Toolset[Runtime](toolset=FunctionToolset[Runtime]([run_external_agent]))
