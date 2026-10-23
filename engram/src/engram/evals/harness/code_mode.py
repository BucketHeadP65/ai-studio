"""Code mode whose run tool-call count holds only the calls the model asks for, never the calls a `run_code` program makes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness import CodeMode

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.toolsets import AbstractToolset, ToolsetTool

# The one tool `CodeMode` shows the model in place of the tools it folds into the sandbox.
_RUN_CODE = "run_code"


@dataclass
class ModelCallsCodeMode(CodeMode[AgentDepsT]):
    """`CodeMode` that keeps the tool calls a `run_code` program makes off the run's tool-call count.

    The run's count, which `UsageLimits.tool_calls_limit` is checked against, then holds only the calls the model asks
    for itself, each `run_code` call among them, counted the way pydantic-ai counts calls without code mode. A program's
    own calls are never checked against `tool_calls_limit` either. They are still bounded by `max_tool_calls` per
    `run_code` call. Calls a program starts while the model is still writing it, under `eager` or `speculate`, still count.
    """

    def get_wrapper_toolset(self, toolset: AbstractToolset[AgentDepsT]) -> AbstractToolset[AgentDepsT] | None:
        """Wrap code mode's toolset so each `run_code` call records its program's tool calls apart from the run.

        Args:
            toolset: The agent's assembled toolset, which code mode folds into the sandbox.

        Returns:
            Code mode's toolset behind the wrapper, or `None` when code mode wraps nothing.
        """
        code_mode_toolset = super().get_wrapper_toolset(toolset)
        return None if code_mode_toolset is None else _ProgramUsageApart(wrapped=code_mode_toolset)


@dataclass
class _ProgramUsageApart(WrapperToolset[AgentDepsT]):
    """Runs each `run_code` call with a usage of its own and no tool-call limit, so its program's calls count apart."""

    async def call_tool(
        self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> object:
        """Pass the call on, giving a `run_code` call a fresh usage and the run's limits without `tool_calls_limit`."""
        if name == _RUN_CODE:
            limits = ctx.usage_limits
            ctx = replace(ctx, usage=RunUsage(), usage_limits=None if limits is None else replace(limits, tool_calls_limit=None))
        return await self.wrapped.call_tool(name, tool_args, ctx, tool)
