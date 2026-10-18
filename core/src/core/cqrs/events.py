"""Change topics: what a change notification is, who announces one, and the frames a browser reads.

A change notification carries a topic and a time, never a payload. The reader refetches what it already knows how to
fetch, so no surface here has to know any other surface's shape.
"""

from __future__ import annotations

import asyncio
import json

from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from core.cqrs.domain import Job
    from core.cqrs.infrastructure.broadcaster import Broadcaster

PING_SECONDS = 15.0
"""How long a stream may say nothing before it writes a comment frame to stay open."""


class ChangeNotification(BaseModel):
    """One thing changed, named by topic and stamped. It never says what changed."""

    model_config = ConfigDict(frozen=True)

    topic: str
    occurred_at: datetime


class TopicSink(Protocol):
    """Anything a producer can tell that a topic changed."""

    def publish(self, topic: str) -> None:
        """Announce `topic`. It must not block and must not raise.

        Args:
            topic: The topic that changed.
        """
        ...


def matches(topic: str, wanted: frozenset[str] | None) -> bool:
    """Whether `topic` is one the reader asked for.

    A wanted name matches itself and everything under it, so `run` matches `run.wf.report` and never matches `runs`.

    Args:
        topic: The topic that changed.
        wanted: The names the reader asked for, or `None` for every topic.

    Returns:
        True when the reader should hear `topic`.
    """
    if wanted is None:
        return True
    return any(topic == name or topic.startswith(f"{name}.") for name in wanted)


class TopicPublisher:
    """A job observer that announces a transition as topics.

    Every transition announces `jobs`. A job whose kind appears in `key_topics` also announces its own key under the
    declared root, so a page watching one thing refetches without knowing that jobs exist.
    """

    def __init__(self, sink: TopicSink, *, key_topics: Mapping[str, str] = {}) -> None:
        """Bind where the topics go and which kinds announce their key.

        Args:
            sink: Where the topics go.
            key_topics: Job kind to the topic root its key is announced under, such as
                `{"kb.refresh": "knowledge-base"}`. A kind not named here announces `jobs` only.
        """
        self._sink = sink
        self._key_topics = key_topics

    def transitioned(self, job: Job) -> None:
        """Announce `jobs`, and `{root}.{key}` when this job's kind declares a root and has a key.

        Args:
            job: The job as it now stands.
        """
        self._sink.publish("jobs")
        root = self._key_topics.get(job.kind)
        if root is not None and job.key:
            self._sink.publish(f"{root}.{job.key}")


async def change_frames(
    broadcaster: Broadcaster, topics: frozenset[str] | None, *, ping_seconds: float = PING_SECONDS
) -> AsyncGenerator[str]:
    """The body of a `text/event-stream` over `broadcaster`, filtered to `topics`.

    Yields one `event: change` frame per matching topic and a `: ping` comment whenever `ping_seconds` pass with
    nothing written, so an idle stream is not closed under it. A topic the filter drops never counts as something
    written, so a busy channel with a narrow filter still pings on time. Framing only, no HTTP.

    Args:
        broadcaster: The process's end of the topic channel.
        topics: The names the reader asked for, or `None` for every topic.
        ping_seconds: How long the stream may stay silent before a ping.

    Yields:
        Each frame, ready to write.
    """
    stream = broadcaster.subscribe()
    pending = asyncio.ensure_future(anext(stream))
    loop = asyncio.get_running_loop()
    last_write = loop.time()
    try:
        while True:
            remaining = ping_seconds - (loop.time() - last_write)
            done, _ = await asyncio.wait({pending}, timeout=max(remaining, 0))
            if not done:
                yield ": ping\n\n"
                last_write = loop.time()
                continue
            notification = pending.result()
            pending = asyncio.ensure_future(anext(stream))
            if matches(notification.topic, topics):
                yield f"event: change\ndata: {json.dumps({'topic': notification.topic, 'at': notification.occurred_at.isoformat()})}\n\n"
                last_write = loop.time()
    finally:
        pending.cancel()
        with suppress(asyncio.CancelledError):
            await pending
        await stream.aclose()
