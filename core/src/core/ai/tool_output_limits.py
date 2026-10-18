"""`ToolOutputLimits` that keeps a tool return's `tools` when it reduces the return."""

from __future__ import annotations

import dataclasses

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.messages import ToolReturn
from pydantic_ai.tools import AgentDepsT
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits as _UpstreamToolOutputLimits

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.tools import ToolDefinition


@dataclass
class ToolOutputLimits(_UpstreamToolOutputLimits[AgentDepsT]):
    """The upstream capability, except a reduced return keeps the tools the original return revealed.

    A deferred capability reveals its tools through `ToolReturn.tools`, so a return that loses them leaves those tools
    unreachable for the rest of the run. Keep `load_capability` out of `tool_filter`: a shortened load no longer counts
    as loaded even with its tools kept, so the capability's tools stay locked.
    """

    async def after_tool_execute(
        self, ctx: RunContext[AgentDepsT], *, call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, object], result: object
    ) -> object:
        """The upstream reduction of `result`, with the original return's `tools` put back when the reduction dropped them.

        Args:
            ctx: The run's context.
            call: The tool call that produced `result`.
            tool_def: The definition of the called tool.
            args: The validated arguments of the call.
            result: What the tool returned.

        Returns:
            `result` as the upstream bands reduce it, carrying the original return's `tools` when the reduction dropped them.
        """
        reduced = await super().after_tool_execute(ctx, call=call, tool_def=tool_def, args=args, result=result)
        if isinstance(result, ToolReturn) and result.tools and isinstance(reduced, ToolReturn) and not reduced.tools:
            return dataclasses.replace(reduced, tools=result.tools)
        return reduced
