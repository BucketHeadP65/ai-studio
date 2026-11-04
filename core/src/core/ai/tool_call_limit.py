"""`WarnNearToolCallLimit`, which warns the model as a run's tool calls near their limit."""

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
class WarnNearToolCallLimit(AbstractCapability[AgentDepsT]):
    """Warns the model before a request once the run's tool calls pass a share of their limit.

    The warning is a request-scoped system prompt. The model sees it in that one request, and the history keeps it, so
    the cached prefix stays valid. The count is the run's `RunUsage.tool_calls`, which holds each call a `run_code`
    program makes as well as the `run_code` call itself. Pass the limit the run's `UsageLimits.tool_calls_limit` holds.
    A limit below 1, a threshold outside (0, 1] or a negative critical remainder raises `ValueError`.
    """

    max_tool_calls: int
    """The run's tool-call limit."""

    warning_threshold: float = 0.7
    """Share of `max_tool_calls` at which warnings begin, above 0 and at most 1."""

    critical_remaining_tool_calls: int = 3
    """Remaining tool calls at which the warning becomes CRITICAL."""

    def __post_init__(self) -> None:
        if self.max_tool_calls <= 0:
            msg = "max_tool_calls must be positive."
            raise ValueError(msg)
        if not 0 < self.warning_threshold <= 1:
            msg = "warning_threshold must be between 0 (exclusive) and 1 (inclusive)."
            raise ValueError(msg)
        if self.critical_remaining_tool_calls < 0:
            msg = "critical_remaining_tool_calls must be non-negative."
            raise ValueError(msg)

    def _warning(self, used: int) -> str | None:
        """The warning for a run that has made `used` tool calls, or `None` below the threshold."""
        share = used / self.max_tool_calls
        if share < self.warning_threshold:
            return None
        remaining = max(0, self.max_tool_calls - used)
        severity: _Severity = "CRITICAL" if remaining <= self.critical_remaining_tool_calls else "URGENT"
        guidance = (
            "Finish the task with what you have and make only the tool calls you need. Keep any further program within the calls that remain."
            if severity == "URGENT"
            else "Answer now with what you have. A tool call past the limit ends the task before you answer."
        )
        counted = "Each tool call a `run_code` program makes counts, and so does the `run_code` call itself."
        details = f"- Tool calls: {used}/{self.max_tool_calls} used ({share:.0%}), {remaining} remaining. {counted}"
        return "\n".join((f"{severity}: The run's tool call limit is approaching.", details, guidance))

    async def before_model_request(self, ctx: RunContext[AgentDepsT], request_context: ModelRequestContext) -> ModelRequestContext:
        """Enqueue the warning into this request once the run's tool calls pass the threshold.

        A request that continues a suspended response gets none, because the request after it gets its own.

        Args:
            ctx: The run's context, whose usage holds the tool calls made so far.
            request_context: The request about to be sent.

        Returns:
            `request_context` unchanged.
        """
        messages = request_context.messages
        if not messages or not isinstance(messages[-1], ModelRequest):
            return request_context
        if (text := self._warning(ctx.usage.tool_calls)) is not None:
            ctx.enqueue(SystemPromptPart(text, scope="request"))
        return request_context
