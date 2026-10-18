"""Tie an always-mounted tool's visibility to a deferred capability it does not itself belong to.

The capability that contributes the tool stays mounted, so closing its plugin still removes the
tool. The tool itself stays hidden until the model loads the other capability with
`load_capability`. `NamedToolDeferral` rewrites the matching tool definitions after they leave
their owning capability and before `CodeMode` decides which of them run inside a program.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import PreparedToolset

from harness.session import Runtime

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.tools import RunContext, ToolDefinition
    from pydantic_ai.toolsets import AbstractToolset


@dataclass
class NamedToolDeferral(AbstractCapability[Runtime]):
    """Stamp `defer_loading` and `capability_id` onto named tools that other capabilities contribute.

    Once the model loads the named capability, the tool becomes callable exactly as if it belonged
    to it. A name this run's tools do not carry, because its plugin is off, is ignored.
    """

    capability_ids: Mapping[str, str]
    """Each tool name, mapped to the id of a capability mounted elsewhere in the agent, usually one
    with `defer_loading=True` of its own."""

    def get_wrapper_toolset(self, toolset: AbstractToolset[Runtime]) -> AbstractToolset[Runtime]:
        """Wrap `toolset` so the named tools carry `defer_loading` and their `capability_id`.

        Args:
            toolset: The agent's combined toolset.

        Returns:
            The same tools, with every named one deferred and tied to its capability.
        """
        capability_ids = self.capability_ids

        async def _tag(_ctx: RunContext[Runtime], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
            return [
                replace(td, defer_loading=True, capability_id=capability_id) if (capability_id := capability_ids.get(td.name)) else td
                for td in tool_defs
            ]

        return PreparedToolset(toolset, _tag)
