"""The console acts: server tools that do what the owner console does, each behind the person's approval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.ai.workflows.contracts import Tier
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import ApprovalRequiredToolset, FilteredToolset, FunctionToolset

from harness.features.followme.domain import CONSOLE_ACTS, SCHEDULE_AUTHORING_ACTS
from harness.session import ANSWERABLE, Runtime

if TYPE_CHECKING:
    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset

    from harness.features.followme.internal_contracts import CardActs, RunActs, SettingsActs

RunAct = Literal["pause", "resume", "stop", "automode"]
"""What `run_control` does to a run. `automode` turns the run's automode on or off, as the call's `on` says."""


def _register_settings_acts(toolset: FunctionToolset[Runtime], settings: SettingsActs) -> None:
    """Mount the three settings acts, each a direct call into `settings`."""

    async def set_model(model_id: str, tier: Tier = "orchestrator") -> str:
        """Switch the model one tier answers with from the next request on (the orchestrator unless a tier is named).

        Needs the person's approval.
        """
        return await settings.set_model(model_id, tier)

    async def select_profile(name: str | None = None) -> str:
        """Put a profile in effect (its plugins, its instructions), or clear it with no name. Needs the person's approval."""
        return await settings.select_profile(name)

    async def toggle_plugin(name: str, *, on: bool) -> str:
        """Open or close one toggleable plugin for every session, from the next turn on. Needs the person's approval."""
        return await settings.toggle_plugin(name, on=on)

    toolset.add_function(set_model, takes_ctx=False)
    toolset.add_function(select_profile, takes_ctx=False)
    toolset.add_function(toggle_plugin, takes_ctx=False)


def _register_run_acts(toolset: FunctionToolset[Runtime], runs: RunActs) -> None:
    """Mount the two run-control acts, each a direct call into `runs`."""

    async def run_control(session_id: str, act: RunAct, *, on: bool = True) -> str:
        """Pause, resume, or stop the run in `session_id`, or set its automode (`on`). Needs the person's approval."""
        if act == "pause":
            return await runs.pause(session_id)
        if act == "resume":
            return await runs.resume(session_id)
        if act == "stop":
            return await runs.stop(session_id)
        return await runs.set_automode(session_id, on=on)

    async def decide_gate(session_id: str, *, approve: bool, feedback: str = "") -> str:
        """Decide the gate waiting in `session_id`. Feedback rides a rejection to the rerun. Needs the person's approval."""
        return await runs.decide(session_id, approve=approve, feedback=feedback)

    toolset.add_function(run_control, takes_ctx=False)
    toolset.add_function(decide_gate, takes_ctx=False)


def _register_card_acts(toolset: FunctionToolset[Runtime], cards: CardActs) -> None:
    """Mount the three workflow-card acts, each a direct call into `cards`."""

    async def archive_workflow(workflow_id: str) -> str:
        """Take a workflow out of the library. Its runs and versions stay. Needs the person's approval."""
        return await cards.archive(workflow_id)

    async def restore_workflow(workflow_id: str) -> str:
        """Put an archived workflow back in the library. Needs the person's approval."""
        return await cards.restore(workflow_id)

    async def redescribe_workflow(workflow_id: str, *, title: str, summary: str = "", applicability: str = "") -> str:
        """Rewrite a workflow's card: title, one-line summary, applicability. Needs the person's approval."""
        return await cards.redescribe(workflow_id, title=title, summary=summary, applicability=applicability)

    toolset.add_function(archive_workflow, takes_ctx=False)
    toolset.add_function(restore_workflow, takes_ctx=False)
    toolset.add_function(redescribe_workflow, takes_ctx=False)


def act_toolset(settings: SettingsActs, runs: RunActs, cards: CardActs) -> FunctionToolset[Runtime]:
    """The eight acts the plugin adds, each one call into its port.

    The schedule acts are the workflow process's own and are not in this toolset.

    Args:
        settings: The port the three settings acts call.
        runs: The port the two run-control acts call.
        cards: The port the three workflow-card acts call.

    Returns:
        The toolset holding the eight acts.
    """
    toolset: FunctionToolset[Runtime] = FunctionToolset()
    _register_settings_acts(toolset, settings)
    _register_run_acts(toolset, runs)
    _register_card_acts(toolset, cards)
    return toolset


def needs_approval(ctx: RunContext[Runtime], tool_def: ToolDefinition, _args: dict[str, object]) -> bool:
    """Whether this call waits for the person: a schedule-authoring act in any session, or a console act in a master one.

    The answer never depends on the follow-me plugin's setting. The schedule acts are gated whether the plugin is on or off.

    Args:
        ctx: The run context, whose session type decides whether a console act waits.
        tool_def: The tool being called.
        _args: The call's arguments. They are not read.

    Returns:
        Whether the call waits for the person's approval.
    """
    if tool_def.name in SCHEDULE_AUTHORING_ACTS:
        return True
    return tool_def.name in CONSOLE_ACTS and ctx.deps.session_kind == "master"


def reachable(ctx: RunContext[Runtime], tool_def: ToolDefinition) -> bool:
    """Whether this turn is offered `tool_def`.

    A tool that waits for the person is offered only on a turn `ANSWERABLE` marks as one someone
    attends. Every other tool is offered on every turn.

    Args:
        ctx: The run context, passed on to `needs_approval`.
        tool_def: The tool to offer or hide.

    Returns:
        Whether the tool is offered on this turn.
    """
    return not needs_approval(ctx, tool_def, {}) or ANSWERABLE.get()


@dataclass
class ConsoleApprovals(AbstractCapability[Runtime]):
    """The approval gate over the agent's whole toolset, mounted in every session.

    The workflow process's own schedule tools and the plugin's own acts meet this one gate, and
    `needs_approval` decides which calls wait. On a turn nobody can answer, a tool that waits is
    hidden. On a turn someone can answer, it is offered and asks the person before it runs.
    """

    def get_wrapper_toolset(self, toolset: AbstractToolset[Runtime]) -> AbstractToolset[Runtime]:
        """The toolset without the tools `reachable` hides, and with `needs_approval` in front of the rest.

        Args:
            toolset: The agent's whole toolset.

        Returns:
            The gated toolset.
        """
        return ApprovalRequiredToolset(FilteredToolset(toolset, reachable), approval_required_func=needs_approval)
