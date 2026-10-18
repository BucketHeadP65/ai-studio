"""The follow-me plugin: what a master session gets on top of the studio agent, and the words that go with it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import CombinedToolset, FilteredToolset

from harness.features.followme.domain import TRUST_ACTS
from harness.session import Runtime

if TYPE_CHECKING:
    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset

FOLLOW_ME_VOICE = (
    "# Follow-me\n"
    "You can move the person around AI Factory Studio and act on their console. navigate(url) goes to a route. "
    "point(target) highlights a registered target. prefill(target, text) fills an input without sending it. "
    "set_tab(target, tab) switches a tab group. Targets are listed in each message's page context, act only on those, "
    "and read each verb's outcome before the next move.\n"
    "Console acts (model, profile, plugins, run controls, gate decisions, schedules, archiving, restoring, redescribing) "
    "wait for the person's approval in their companion panel: say what you are about to do, then call the act once. "
    "A denial is an answer, say so and stop. Activation, promotion, demotion, approval, and saving are the owner's own "
    "acts, take the person to the right page and stop there."
)
"""The instructions the follow-me plugin gives the model: the verbs, the console acts and the owner's own acts."""


def master_only(ctx: RunContext[Runtime]) -> bool:
    """Whether this run belongs to a master session, the one session type the plugin serves.

    Args:
        ctx: The run context, whose session type is read.

    Returns:
        Whether the session is a master session.
    """
    return ctx.deps.session_kind == "master"


def not_a_trust_act(_ctx: RunContext[Runtime], tool_def: ToolDefinition) -> bool:
    """Whether this tool may be offered beside the console acts: everything except the owner's own gestures.

    Args:
        _ctx: The run context. It is not read.
        tool_def: The tool to offer or filter off.

    Returns:
        Whether the tool's name is outside `TRUST_ACTS`.
    """
    return tool_def.name not in TRUST_ACTS


@dataclass
class FollowMe(AbstractCapability[Runtime]):
    """The verbs and the console acts as one capability, with the instructions that go with them and the trust acts filtered off."""

    verbs: AbstractToolset[Runtime]
    """The client-executed verbs the browser performs."""
    acts: AbstractToolset[Runtime]
    """The console acts, each one call into a port."""

    def get_toolset(self) -> AbstractToolset[Runtime]:
        """Both toolsets as one surface.

        Returns:
            The verbs and the acts, combined.
        """
        return CombinedToolset([self.verbs, self.acts])

    def get_wrapper_toolset(self, toolset: AbstractToolset[Runtime]) -> AbstractToolset[Runtime]:
        """The whole surface with the trust acts filtered off it.

        No name in `TRUST_ACTS` has a tool while the plugin is mounted.

        Args:
            toolset: The agent's whole toolset.

        Returns:
            The toolset without the trust acts.
        """
        return FilteredToolset(toolset, not_a_trust_act)

    def get_instructions(self) -> str:
        """The follow-me voice.

        It is one literal string, the same on every call.

        Returns:
            `FOLLOW_ME_VOICE`.
        """
        return FOLLOW_ME_VOICE
