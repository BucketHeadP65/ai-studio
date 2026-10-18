"""The environment's contract: the ports composition hands the slices, and the vocabulary of the step surface.

Protocols and constants only: what a step may be granted, by name, and the narrow shapes the
composition needs from outside. Nothing here constructs anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pydantic_ai_harness.step_persistence import StepStore
from pydantic_ai_harness.tool_output_limits import READ_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.ai.workflows.contracts import Tier
    from core.settings import InfrastructureSettings
    from mcp import types as mcp_types
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot

    from harness.domain.events import Recorded, SessionEvent

DEFINED_STEP_BUDGET = 24
"""How many model requests a declared step with tools may spend. It is both the hard limit and the threshold the step is warned about."""

BRIEFING_ONLY_BUDGET = 4
"""How many model requests a declared step without tools may spend. It is both the hard limit and the threshold the step is warned about."""


class StepModelWrapper(Protocol):
    """A hook that wraps each model tier's model before step agents are built over it.

    It is asked once per tier when the environment is built, never per request. It receives the
    model the environment resolved for the tier and returns the model that tier's step agents run
    on. It reaches step agents only. No other model in the process passes through it. A deployment
    that wraps nothing installs none.
    """

    def __call__(self, model: Model, *, role: Tier) -> Model:
        """The model a step agent of `role` runs on, built over the one this environment resolved.

        Args:
            model: The model this environment resolved for the role.
            role: The model tier the step agents run in.

        Returns:
            The model the step agents of `role` run on.
        """
        ...


SQL_TOOL = "run_sql"
"""Engram's one-statement read tool, the SQL plugin's surface, held by the proof harness."""

SCHEMA_TOOL = "describe_schema"
"""Engram's schema tool, beside `run_sql` on the same SQL plugin. The proof harness drives these two,
and the studio agent holds the read tools `search` and `fetch` instead."""

NOISE_TOOLS: tuple[str, ...] = ("submit_feedback",)
"""Engram tools no programmatic caller wants, filtered out of what a model sees."""

ENGRAM_SANDBOX = "run_code"
"""Engram's own server-side sandbox, kept off the step surface.

Steps hold the harness's `CodeMode`, whose tool has exactly this name, and two tools cannot share one name."""

NOT_FOR_STEPS: tuple[str, ...] = (*NOISE_TOOLS, ENGRAM_SANDBOX)
"""Engram tools a workflow step never sees: the feedback form, and the sandbox `CodeMode` replaces."""

PROGRAM_OUTPUT_LIMIT = 24_000
"""How much of a program's printed digest reaches the model before it is spilled to disk."""

NOT_IN_SANDBOX: tuple[str, ...] = (READ_TOOL_NAME,)
"""Tools an agent under code mode calls directly, never from inside a program.

The one tool is the overflow read-back, `read_tool_result`, which recovers a payload spilled from the model's view."""

SHELL_TOOL = "run_command"
"""The harness `Shell` capability's tool, and the one capability here that can change anything.

A declared step is granted it by name, exactly like a read. There is no dedicated write verb."""

SANDBOX = "run_code"
"""`CodeMode`'s tool, the one whose return the model reads.

Its string equals `ENGRAM_SANDBOX`, which names the engram tool a step never holds. Use this one
for the tool a step does hold."""

ORCHESTRATOR_SKILLS: tuple[str, ...] = (
    "agent-threads",
    "agent-ux",
    "context-engineering",
    "delegating-work",
    "designing-note-writing",
    "designing-workflows",
    "documenting",
    "engram-init",
    "orchestrating-a-run",
    "saving-a-workflow",
    "placing-knowledge",
    "searching-the-knowledge-base",
    "writing-agents",
    "writing-skills",
)
"""Skills the chat agent holds when no profile names its own list."""

JUDGE_TOOLS: frozenset[str] = frozenset(
    {
        "search",
        "fetch",
        "graph_expand",
        "find_paths",
        "describe_schema",
        "read_notes",
        "list_pages",
        "read_page",
        "read_source",
        "trace_source",
        "list_undigested",
        "list_recent_digests",
        "compounding_work",
        "source_queue_policy",
        "source_queue_rows",
        "digest_skeleton",
        "knowledge_base_profile",
        "recall_beliefs",
    }
)
"""Tools a judge's inline step may hold: every read, no write, no act. A judge changes a run through its verdict alone."""


@dataclass(frozen=True)
class EmbeddingContract:
    """The deployment and width the knowledge base was embedded under, with the connection settings that reach the deployment."""

    deployment: str
    infrastructure: InfrastructureSettings
    dimensions: int


class ToolCaller(Protocol):
    """How this host runs one granted tool on behalf of a program. `MCPToolset.direct_call_tool` is one.

    It adds no retries, no approval and no output limits.
    """

    async def __call__(self, name: str, args: dict[str, object], /) -> object:
        """Run the named tool with these arguments and return whatever it answered.

        Args:
            name: The tool's name.
            args: The tool's arguments, by name.

        Returns:
            Whatever the tool answered.
        """
        ...


class SessionEvents(Protocol):
    """One session log, as a capability, a flag tool and a timeline reader hold it.

    A workspace's own file and this deployment's own both answer it, and a caller never knows which
    one it holds.
    """

    def record(self, event: SessionEvent) -> Recorded | None:
        """Append `event` to the session log.

        Args:
            event: The event to append.

        Returns:
            The log entry with its position and time, or `None` when the write failed.
        """
        ...

    def events(self) -> tuple[Recorded, ...]:
        """Every readable event on this timeline, in order.

        Returns:
            The readable log entries, oldest first.
        """
        ...

    def boot_sweep(self) -> None:
        """Close on this timeline what an older process left open.

        You must call it once, before the first request and before the session is resumed. First, a
        `forgotten-at` marker an older process left becomes a `conversation_forgotten` entry stamped
        with the marker's own time. Then every turn that started and never ended gets a
        `turn_abandoned` entry. An implementation with nothing to sweep records nothing.
        """
        ...


class SessionTranscript(StepStore, Protocol):
    """Every session's transcript: the runs and steps a turn writes, and what recall reads back.

    One store holds every session's runs, and step persistence and conversation search both use it.
    A run is placed in it by the conversation id the run carries.
    """

    async def list_snapshots(self, *, run_id: str) -> list[ContinuableSnapshot]:
        """Every one of the run's settled steps, oldest first.

        A message a later step replaced is still held by the step that first carried it, so this
        read offers it where the newest step alone would not.

        Args:
            run_id: The run to read.

        Returns:
            The run's settled steps, oldest first.
        """
        ...


class ConversationHistory(Protocol):
    """The record's read of what the model last saw in one conversation.

    A caller that only starts turns declares this port and never the transcript's writes. The same
    store answers both.
    """

    async def history_for_next_run(self, *, conversation_id: str) -> list[ModelMessage]:
        """The newest complete step of the conversation's newest top-level run, or nothing.

        A run another run started is skipped. Only complete steps count: a run that stopped for its
        caller settles no step, so this answers nothing for it and the calls it waits on are not here.

        Args:
            conversation_id: The conversation to read.

        Returns:
            The messages of that step, or an empty list.
        """
        ...


class ConversationMessages(Protocol):
    """Every message of one conversation once, in the form it was first said.

    `ConversationHistory` answers what the model last saw. This port answers everything that ever happened.
    """

    async def conversation_messages(self, *, conversation_id: str) -> list[ModelMessage]:
        """Every message of the conversation once, oldest first.

        Args:
            conversation_id: The conversation to read.

        Returns:
            The messages, oldest first.
        """
        ...


class KnowledgeBaseSurface(Protocol):
    """The part of the knowledge base toolset this environment actually uses.

    Three members: the tool list grants derive from, the filter a worker's grant becomes, and the
    direct call a sandboxed program dispatches through.
    """

    async def list_tools(self) -> list[mcp_types.Tool]:
        """The tools the connected server currently offers.

        Returns:
            The server's tools.
        """
        ...

    def filtered(self, filter_func: Callable[[RunContext[Any], ToolDefinition], bool | Awaitable[bool]]) -> AbstractToolset[Any]:
        """Any toolset over the filtered surface admitting only the tools `filter_func` accepts.

        It may be any toolset type, such as one that also guards or records its calls.

        Args:
            filter_func: Whether one tool is admitted, asked per run context and tool definition.

        Returns:
            The filtered toolset.
        """
        ...

    async def direct_call_tool(
        self, name: str, args: dict[str, Any], *, metadata: dict[str, Any] | None = None, use_task: bool = False
    ) -> Any:
        """Run one named tool directly, bypassing an agent's tool-call machinery.

        Args:
            name: The tool's name.
            args: The tool's arguments, by name.
            metadata: The request-level `_meta` payload sent with the call, or `None` for none.
            use_task: Whether the server runs the call as a task. The completed result is returned either way.

        Returns:
            Whatever the tool answered.
        """
        ...
