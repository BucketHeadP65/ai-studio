"""The Vercel AI data-stream protocol, in and out. Nothing else in the slice knows it."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from core.ai.history.contracts import StepUsage, to_chunk
from pydantic_ai import AgentRunResult
from pydantic_ai.messages import ModelMessage, sanitize_messages
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.ui import NativeEvent
from pydantic_ai.ui.vercel_ai import VercelAIAdapter, VercelAIEventStream
from pydantic_ai.ui.vercel_ai.response_types import BaseChunk, DataChunk
from starlette.responses import StreamingResponse

from harness.features.authoring.api_models import ChatRequest
from harness.features.authoring.internal_contracts import Turn
from harness.session import Runtime

SDK_VERSION = 6
"""The protocol version the studio's own client reads. Its SDK is v7, which reads the v6 wire."""


def messages_of(body: ChatRequest) -> list[ModelMessage]:
    """The new messages the request carries, as pydantic-ai messages.

    The parts a client is not trusted to send are stripped: its own system prompts, file URLs on a
    scheme other than http or https, and references to files uploaded to the model provider.

    Args:
        body: The chat request.

    Returns:
        The messages, with those parts stripped.
    """
    return sanitize_messages(VercelAIAdapter.load_messages(body.messages))


@dataclass
class StudioEventStream(VercelAIEventStream[Runtime, str | DeferredToolRequests]):
    """The protocol's own chunks, with the step readings this app's client also reads.

    A reading appears in `usage` as each model response finishes. It goes out ahead of the next
    event's chunks, or before the closing frames, so a response's cost is on the wire before
    whatever the turn does next.
    """

    usage: list[StepUsage] = field(default_factory=list)

    async def handle_event(self, event: NativeEvent) -> AsyncIterator[BaseChunk]:
        """Any reading a finished response left, then whatever this event says.

        Args:
            event: The run's next event.

        Yields:
            The waiting readings' chunks, then the protocol's chunks for `event`.
        """
        for chunk in self._readings():
            yield chunk
        async for chunk in super().handle_event(event):
            yield chunk

    async def after_stream(self) -> AsyncIterator[BaseChunk]:
        """The last response's reading, then the protocol's own closing frames.

        Yields:
            The waiting readings' chunks, then the closing frames.
        """
        for chunk in self._readings():
            yield chunk
        async for chunk in super().after_stream():
            yield chunk

    def _readings(self) -> Sequence[DataChunk]:
        """Every reading waiting, taken off the list so none goes out twice."""
        taken = [to_chunk(reading) for reading in self.usage]
        self.usage.clear()
        return taken


def streaming_response(body: ChatRequest, accept: str | None, turn: Turn) -> StreamingResponse:
    """`turn` encoded as the Vercel AI data stream, in the form `accept` asks for and under the protocol's headers.

    A turn that settled something sends one `data-settle` chunk carrying it, before the closing frames.

    Args:
        body: The chat request the turn answers.
        accept: The request's `Accept` header, or `None` when it sent none.
        turn: The turn to stream.

    Returns:
        The streaming response.
    """
    stream = StudioEventStream(body, accept=accept, sdk_version=SDK_VERSION, usage=turn.usage)

    async def settled(result: AgentRunResult[str | DeferredToolRequests]) -> AsyncIterator[BaseChunk]:
        """What the turn settled, as the one chunk the panel reads, or nothing when it settled nothing."""
        del result
        payload = await turn.settle()
        if payload is not None:
            yield DataChunk(type="data-settle", data=payload)

    return stream.streaming_response(stream.transform_stream(turn.events, on_complete=settled))
