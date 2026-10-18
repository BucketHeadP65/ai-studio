"""Mid-turn steering: what the person sends while a turn runs reaches the model inside that turn.

The steer route collects messages on the session's `steer_inbox`.
This capability moves them onto the run's own pending-message queue with `ctx.enqueue`.
pydantic-ai then delivers them into the next model request, or continues a run that was about to end
so the reply addresses what arrived.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import SystemPromptPart
from pydantic_graph import End

from harness.session import Runtime

if TYPE_CHECKING:
    from pydantic_ai.capabilities.abstract import AgentNode, NodeResult
    from pydantic_ai.models import ModelRequestContext
    from pydantic_ai.tools import RunContext

STEER_PREAMBLE = "The person sent this while you were working. Fold it into what you do next. It is not a new turn."
"""The system line sent beside each steered message, so the model knows it arrived mid-turn.
The person's own words are sent unchanged, so the transcript shows exactly what they typed."""


class SteerDelivery(AbstractCapability[Runtime]):
    """Drains the session's steer inbox onto the run's pending-message queue.

    It drains before each model request and when the run would otherwise end.
    A message queued at the end still continues the run.
    """

    async def before_model_request(self, ctx: RunContext[Runtime], request_context: ModelRequestContext) -> ModelRequestContext:
        """Queue whatever the person sent since the last model request.

        Args:
            ctx: The run context, whose session holds the steer inbox.
            request_context: The model request about to be sent.

        Returns:
            `request_context`, unchanged.
        """
        _drain(ctx)
        return request_context

    async def after_node_run(self, ctx: RunContext[Runtime], *, node: AgentNode[Runtime], result: NodeResult[Runtime]) -> NodeResult[Runtime]:
        """Queue what arrived while the final response streamed, so the run continues instead of ending past it.

        Args:
            ctx: The run context, whose session holds the steer inbox.
            node: The node that just ran. It is not read.
            result: What the node returned. Only an `End` drains the inbox.

        Returns:
            `result`, unchanged.
        """
        del node
        if isinstance(result, End):
            _drain(ctx)
        return result


def _drain(ctx: RunContext[Runtime]) -> None:
    """Move every waiting steer, oldest first, each as its own queued message."""
    while ctx.deps.steer_inbox:
        ctx.enqueue(SystemPromptPart(content=STEER_PREAMBLE), ctx.deps.steer_inbox.pop(0))
