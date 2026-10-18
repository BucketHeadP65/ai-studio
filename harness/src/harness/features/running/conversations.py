"""The step conversations of the current run, one per agentic step and map item, keyed by the node and item.

An inline agent step (`AgentStep`) keeps its message history across every time a run invokes it,
and a rejection's feedback reaches it as the next invocation's words. Read and write that history
only through the one `StepConversations` the session holds, so a rerun sees its own history.

A step conversation opens when a step starts executing (`open`) and settles when it answers
(`settle`), so a running step always has a live entry with a tailable `stream`. `close_all` ends
every step conversation's stream when the run ends.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from core.ai.workflows.contracts import AgentStep, Briefing, StepIdentity
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.warn_on_cache_busts import WarnOnCacheBusts


STEP_STREAM_BUFFER_BYTES = 1_000_000
"""How many bytes of encoded frames one step conversation's live buffer keeps. Appending past it evicts the
oldest whole frames first, so a long-running step never grows its buffer without bound."""


@dataclass
class StepStream:
    """One step conversation's live stream: encoded chunks appended in order, tailed by any number of readers.

    A chunk's index is absolute for the stream's whole life, even once eviction starts: chunk `i`
    lives at `chunks[i - dropped]` for as long as it is still held. A reader whose index has been
    evicted jumps forward to the oldest chunk still held, never to a stale offset.
    """

    chunks: list[str] = field(default_factory=list)
    closed: bool = False
    dropped: int = 0
    """How many of this stream's oldest chunks have been evicted to stay under `STEP_STREAM_BUFFER_BYTES`."""
    _held_bytes: int = field(default=0, repr=False, compare=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False, compare=False)
    """Set, then replaced with a fresh event, on every append and on close, so each waiting reader wakes once per change."""

    def append(self, chunk: str) -> None:
        """Add one encoded chunk to the tail, then wake every reader.

        While the buffer is over `STEP_STREAM_BUFFER_BYTES`, the oldest whole chunks are evicted,
        always keeping the newest.

        Args:
            chunk: One encoded frame.
        """
        self.chunks.append(chunk)
        self._held_bytes += len(chunk.encode())
        while self._held_bytes > STEP_STREAM_BUFFER_BYTES and len(self.chunks) > 1:
            evicted = self.chunks.pop(0)
            self._held_bytes -= len(evicted.encode())
            self.dropped += 1
        self._wake.set()
        self._wake = asyncio.Event()

    def close(self) -> None:
        """Stop the stream for good, waking every reader still waiting so its `tail` can end. Idempotent."""
        if self.closed:
            return
        self.closed = True
        self._wake.set()

    def floor(self, since: int) -> int:
        """The absolute index a replay from `since` would start at right now: `since`, or the oldest chunk still held.

        A snapshot only. Eviction can advance before a replay starts, and `replay` derives its own start.

        Args:
            since: The index the reader asks to start from.

        Returns:
            The larger of `since` and the index of the oldest chunk held.
        """
        return max(since, self.dropped)

    async def replay(self, since: int = 0) -> AsyncIterator[tuple[int, str]]:
        """Replay from `floor(since)` as `(index, chunk)` pairs, then live as chunks arrive, ending once the stream closes.

        A reader that falls behind eviction, even while suspended between two pairs, jumps forward
        to the oldest chunk still held. Each pair's index is always the chunk's true index.

        Args:
            since: The index to start from.

        Yields:
            Each chunk with its absolute index.
        """
        index = self.floor(since)
        while True:
            index = max(index, self.dropped)
            if index - self.dropped < len(self.chunks):
                yield index, self.chunks[index - self.dropped]
                index += 1
                continue
            if self.closed:
                return
            await self._wake.wait()

    async def tail(self, since: int = 0) -> AsyncIterator[str]:
        """`replay(since)`'s chunks without their indices, for a reader that only wants the wire bytes.

        Args:
            since: The index to start from.

        Yields:
            Each chunk, in order.
        """
        async for _, chunk in self.replay(since):
            yield chunk


@dataclass
class StepConversation:
    """One step's standing in the current run: its step, its output type, its conversation, its live state."""

    step: Briefing | AgentStep
    output_type: type
    instructions: str = ""
    """The system prompt the conversation was opened with, reused verbatim by every later turn so
    the request prefix stays byte-stable."""
    monitor: WarnOnCacheBusts | None = None
    """The configured cache monitor this conversation mounts on every turn. Its high-water mark
    lives inside one turn's own run, so it observes a prefix collapse within a turn, never across
    turns."""
    messages: list[ModelMessage] = field(default_factory=list)
    stream: StepStream = field(default_factory=StepStream)
    """The live stream. A rerun that reopens this step conversation keeps the same stream, chunks and all."""
    steer_inbox: list[str] = field(default_factory=list)
    """Words a person sent this step conversation while it was executing, waiting for the step to read them."""
    executing: bool = False
    """Whether a step is running in this step conversation right now."""


@dataclass
class StepConversations:
    """Every step conversation in the current run, keyed by the node and item that opened it."""

    conversations: dict[tuple[str, str], StepConversation] = field(default_factory=dict)

    def reset(self) -> None:
        """Close every held step conversation's stream, then drop them all. A launch calls this before a new run starts.

        A reader still tailing a dropped step conversation's stream sees the close and ends.
        """
        self.close_all()
        self.conversations.clear()

    def open(
        self,
        key: tuple[str, str],
        *,
        step: Briefing | AgentStep,
        output_type: type,
        instructions: str,
        monitor: WarnOnCacheBusts | None,
    ) -> StepConversation:
        """The step conversation `key` names, marked executing: the one already open, reused whole, or a freshly opened one.

        An open step conversation keeps its `messages`, `stream` and `steer_inbox`, so a map item's later
        turn and a step's rerun read its history and keep the same stream. `step`, `output_type`,
        `instructions` and `monitor` are replaced with this call's.

        Args:
            key: The node and item the step conversation belongs to.
            step: The step now running in the step conversation.
            output_type: The output type the next turn runs under.
            instructions: The system prompt the next turn runs under.
            monitor: The cache monitor the next turn mounts, or `None`.

        Returns:
            The step conversation, the same object on every call for one key.
        """
        conversation = self.conversations.get(key)
        if conversation is not None:
            conversation.step = step
            conversation.output_type = output_type
            conversation.instructions = instructions
            conversation.monitor = monitor
            conversation.executing = True
            return conversation
        conversation = StepConversation(step=step, output_type=output_type, instructions=instructions, monitor=monitor, executing=True)
        self.conversations[key] = conversation
        return conversation

    def settle(self, key: tuple[str, str], messages: list[ModelMessage]) -> None:
        """Write the run's own messages onto `key`'s step conversation and mark it no longer executing.

        Args:
            key: The node and item of a step conversation that is open.
            messages: The messages the step's turn ended with.
        """
        conversation = self.conversations[key]
        conversation.messages = messages
        conversation.executing = False

    def close_all(self) -> None:
        """Close every held step conversation's stream. Called when the run ends."""
        for conversation in self.conversations.values():
            conversation.stream.close()


def conversation_key(identity: StepIdentity | None, fallback: str) -> tuple[str, str]:
    """The conversation key for the step now performing: its node and item, or the bare name.

    Args:
        identity: The step's identity in the running graph, or `None` when it has none.
        fallback: The name to key by when there is no identity.

    Returns:
        The node id and the item key, the item empty when the step has none.
    """
    if identity is None:
        return (fallback, "")
    return (identity.node_id, identity.item_key or "")
