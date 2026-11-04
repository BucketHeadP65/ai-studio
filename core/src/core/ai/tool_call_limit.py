"""`ToolCallLimitWarning`, the warning a model gets as its run's tool calls near their limit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, SystemPromptPart
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.models import ModelRequestContext

_Severity = Literal["URGENT", "CRITICAL"]


@dataclass
class ToolCallLimitWarning(AbstractCapability[AgentDepsT]):
    """The warning a model gets before a request once the run's tool calls pass a share of the run's tool-call limit.

    The limit is the run's own `UsageLimits.tool_calls_limit`, and a run without one is never warned. The count is the
    run's `RunUsage.tool_calls`, whatever the run's usage records as a tool call. The warning is URGENT from
    `warning_threshold` of the limit, and CRITICAL once the remaining calls are at most the larger of
    `critical_remaining_tool_calls` and `critical_remaining_share` of the limit. It is a request-scoped system prompt:
    the model sees it in that one request, and the history keeps it. A share outside its range or a negative critical
    remainder raises `ValueError`.
    """

    counts_program_calls: bool = False
    """Whether each call a `run_code` program makes counts toward the limit, as under pydantic-ai-harness's `CodeMode`.

    The warning then says so, and names how many more calls a program may make. That is one fewer than remain, as the
    `run_code` call itself takes one.
    """

    warning_threshold: float = 0.7
    """Share of the limit at which warnings begin, above 0 and at most 1."""

    critical_remaining_tool_calls: int = 3
    """The fewest remaining tool calls at which the warning is CRITICAL, whatever the limit."""

    critical_remaining_share: float = 0.1
    """Share of the limit at or under which the remaining tool calls make the warning CRITICAL, from 0 to 1."""

    def __post_init__(self) -> None:
        if not 0 < self.warning_threshold <= 1:
            msg = "warning_threshold must be between 0 (exclusive) and 1 (inclusive)."
            raise ValueError(msg)
        if self.critical_remaining_tool_calls < 0:
            msg = "critical_remaining_tool_calls must be non-negative."
            raise ValueError(msg)
        if not 0 <= self.critical_remaining_share <= 1:
            msg = "critical_remaining_share must be between 0 and 1 (inclusive)."
            raise ValueError(msg)

    def _warning(self, used: int, limit: int) -> str | None:
        """The warning for a run that has made `used` of `limit` tool calls, or `None` below the threshold."""
        share = used / limit
        if share < self.warning_threshold:
            return None
        remaining = max(0, limit - used)
        critical = max(self.critical_remaining_tool_calls, self.critical_remaining_share * limit)
        severity: _Severity = "CRITICAL" if remaining <= critical else "URGENT"
        guidance = (
            "Finish the task with what you have and make only the tool calls you need."
            if severity == "URGENT"
            else "Answer now with what you have. A tool call past the limit ends the task before you answer."
        )
        details = f"- Tool calls: {used}/{limit} used ({share:.0%}), {remaining} remaining."
        if self.counts_program_calls:
            room = max(0, remaining - 1)
            details += (
                " Each tool call a `run_code` program makes counts, and so does the `run_code` call itself, "
                f"so a program may make at most {room} more {'call' if room == 1 else 'calls'}."
            )
        return "\n".join((f"{severity}: The run's tool call limit is approaching.", details, guidance))

    async def before_model_request(self, ctx: RunContext[AgentDepsT], request_context: ModelRequestContext) -> ModelRequestContext:
        """Enqueue the warning into this request once the run's tool calls pass the threshold.

        A request that continues a suspended response gets none.

        Args:
            ctx: The run's context, whose usage holds the tool calls made so far and whose usage limits hold the limit.
            request_context: The request about to be sent.

        Returns:
            `request_context` unchanged.
        """
        messages = request_context.messages
        if not messages or not isinstance(messages[-1], ModelRequest):
            return request_context
        limit = ctx.usage_limits.tool_calls_limit if ctx.usage_limits is not None else None
        if limit is None or limit < 1:
            return request_context
        if (text := self._warning(ctx.usage.tool_calls, limit)) is not None:
            ctx.enqueue(SystemPromptPart(text, scope="request"))
        return request_context
