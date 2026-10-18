"""The per-line stream relay and the steer capability every step-agent mounts.

`run_relayed` runs one agentic step and appends its Vercel v6 chunks to the step's
`StepConversation.stream` as they arrive, in the same encoding the chat stream uses. Each run is
one envelope from `start` to `finish`, whatever tool round trips happen inside it.
`StepSteerDelivery` moves what a person sent to that line while it ran onto the run's
pending-message queue, so the words reach the model inside the turn already under way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.ui.vercel_ai import VercelAIEventStream
from pydantic_ai.ui.vercel_ai.response_types import DataChunk, DoneChunk
from pydantic_graph import End

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from pydantic_ai import Agent, AgentRunResult
    from pydantic_ai.capabilities import AgentCapability
    from pydantic_ai.capabilities.abstract import AgentNode, NodeResult
    from pydantic_ai.messages import AgentStreamEvent, ModelMessage
    from pydantic_ai.models import ModelRequestContext
    from pydantic_ai.tools import RunContext
    from pydantic_ai.usage import UsageLimits

    from harness.features.running.conversations import StepConversation

STEP_STEER_PREAMBLE = "The person watching this step sent this while you were working. Fold it into what you do next."
"""The system line queued beside each steered message, telling the model it arrived mid-turn."""

_STEER_ENCODER = VercelAIEventStream(sdk_version=6)
"""The one encoder the steer and replay-floor frames share, safe because `encode_event` keeps no state between calls."""


async def run_relayed(  # noqa: PLR0913 (the agent and its line, then four arguments passed straight to worker.run_stream_events)
    worker: Agent[None, object],
    conversation: StepConversation,
    prompt: str,
    *,
    usage_limits: UsageLimits,
    message_history: list[ModelMessage] | None = None,
    capabilities: Sequence[AgentCapability[None]] | None = None,
) -> AgentRunResult[object]:
    """Run `worker` on `prompt`, appending each chunk of its one Vercel v6 envelope to `conversation.stream` as it arrives.

    The envelope opens and closes once per call, not once per graph node. The encoder's final done
    frame is left off, so `conversation.stream` stays open across every turn of the line until the
    HTTP route closes it. A run that raises still ends its envelope: open parts close, pending tool
    calls are marked failed or interrupted, and an `error` chunk comes before `finish`. The exception
    is then raised again unchanged, exactly as a plain `worker.run(...)` would raise it.

    Args:
        worker: The step's agent.
        conversation: The step's line, whose `stream` receives the encoded chunks.
        prompt: What this turn asks the model.
        usage_limits: The limits the run is held to.
        message_history: The line's earlier messages, or `None` for a first turn.
        capabilities: Capabilities mounted for this run only, such as `StepSteerDelivery`.

    Returns:
        The run's result, as `worker.run` would return it.

    Raises:
        Exception: whatever the run raised, once its `error` chunk is on the stream.
    """
    encoder = VercelAIEventStream(sdk_version=6)
    result: AgentRunResult[object] | None = None
    failure: Exception | None = None

    async def native_events() -> AsyncIterator[AgentStreamEvent | AgentRunResultEvent[object]]:
        nonlocal result, failure
        try:
            async with worker.run_stream_events(
                prompt, message_history=message_history, usage_limits=usage_limits, capabilities=capabilities
            ) as events:
                async for event in events:
                    if isinstance(event, AgentRunResultEvent):
                        result = event.result
                    yield event
        except Exception as exc:
            # Kept for `run_relayed` to raise again once the loop below has drained `transform_stream`'s
            # error path. Raising here too is what runs that path. Without it a failed run would end
            # with a clean `finish`.
            failure = exc
            raise

    async for chunk in encoder.transform_stream(native_events()):
        if isinstance(chunk, DoneChunk):
            continue
        conversation.stream.append(encoder.encode_event(chunk))

    if failure is not None:
        raise failure
    if result is None:
        msg = "the run ended with neither a result nor a raised error"
        raise RuntimeError(msg)
    return result


def steer_chunk(message: str, at: str) -> str:
    """The `data-steer` frame, exactly as the wire carries it.

    Args:
        message: What the person sent to the running step.
        at: When it arrived, as an ISO 8601 timestamp.

    Returns:
        The encoded chunk, ending in a blank line.
    """
    return _STEER_ENCODER.encode_event(DataChunk(type="data-steer", data={"message": message, "at": at}))


def replay_floor_chunk(floor: int, requested: int) -> str:
    """The `data-replay-floor` frame every step stream opens with, in the wire's own `data-*` encoding.

    Args:
        floor: The index of the first chunk the replay delivers.
        requested: The `since` index the client asked to replay from.

    Returns:
        The encoded chunk, ending in a blank line.
    """
    return _STEER_ENCODER.encode_event(DataChunk(type="data-replay-floor", data={"floor": floor, "requested": requested}))


class StepSteerDelivery(AbstractCapability[None]):
    """Drains one line's steer inbox onto the run's pending-message queue.

    It drains before each model request and again when the run would otherwise end, so a steer that
    lands during the final response still redirects the run.
    """

    def __init__(self, conversation: StepConversation) -> None:
        """Deliver the steers sent to one step's line.

        Args:
            conversation: The line whose steer inbox this drains.
        """
        self._conversation = conversation

    async def before_model_request(self, ctx: RunContext[None], request_context: ModelRequestContext) -> ModelRequestContext:
        """Queue whatever the line's inbox is holding since the last model request.

        Args:
            ctx: The run context whose pending-message queue receives the steers.
            request_context: The request about to be made.

        Returns:
            `request_context`, unchanged.
        """
        _drain(ctx, self._conversation)
        return request_context

    async def after_node_run(self, ctx: RunContext[None], *, node: AgentNode[None], result: NodeResult[None]) -> NodeResult[None]:
        """Queue what arrived while the final response streamed, so the run continues instead of ending past it.

        Args:
            ctx: The run context whose pending-message queue receives the steers.
            node: The node that just ran. It is not read.
            result: What the node returned. Only an `End` drains the inbox.

        Returns:
            `result`, unchanged.
        """
        del node
        if isinstance(result, End):
            _drain(ctx, self._conversation)
        return result


def _drain(ctx: RunContext[None], conversation: StepConversation) -> None:
    """Move every waiting steer on `conversation`, oldest first, each as its own queued message beside the preamble."""
    while conversation.steer_inbox:
        ctx.enqueue(SystemPromptPart(content=STEP_STEER_PREAMBLE), conversation.steer_inbox.pop(0))
