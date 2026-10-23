"""The change notification channel over Postgres: one process's end of it and the connections it runs on.

Every announcement goes through Postgres, even when the producer and the reader share a process, so a job runner's
topics reach an API process on the same database.
"""

from __future__ import annotations

import asyncio

from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy import text

from core.cqrs.events import ChangeNotification
from core.db import Listener, Notifier
from core.observability import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping

    from sqlalchemy import Result, TextClause

_logger = get_logger(__name__)

TOPICS_CHANNEL = "coordination_topics"
"""The one channel every change topic travels on. The payload is the topic string itself."""

SUBSCRIBER_QUEUE_LIMIT = 256
"""How many unread change notifications one in-process reader may hold. Past it the oldest is dropped, because the
newest hint to refetch is the one that matters."""


class Executes(Protocol):
    """What `publish_in` needs from a transaction: a session or a connection that executes a statement."""

    async def execute(self, statement: TextClause, parameters: Mapping[str, object] | None = None, /) -> Result[*tuple[object, ...]]:
        """Run `statement` with `parameters` inside the caller's transaction.

        Args:
            statement: The statement to run.
            parameters: Its bound values.

        Returns:
            The statement's result.
        """
        ...


class TopicNotifier(Protocol):
    """Sends one payload on one channel, over a connection of its own."""

    async def notify(self, channel: str, payload: str) -> None:
        """Send `payload` on `channel`.

        Args:
            channel: The channel to send on.
            payload: The text to send.
        """
        ...

    async def close(self) -> None:
        """Release the connection."""
        ...


class TopicListener(Protocol):
    """Receives payloads from one channel, over a connection of its own."""

    async def open(self) -> None:
        """Start listening."""
        ...

    async def get(self) -> str:
        """The next payload, waiting for one.

        Returns:
            The payload.

        Raises:
            ConnectionError: Once the connection is lost.
        """
        ...

    async def close(self) -> None:
        """Stop listening."""
        ...


def _log_unexpected_exit(task: asyncio.Task[None]) -> None:
    """Log a broadcaster task that ended without `stop` cancelling it."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _logger.error("broadcaster task exited unexpectedly", error=exc)
    else:
        _logger.error("broadcaster task exited without being cancelled")


class Broadcaster:
    """One process's whole end of the topic channel.

    Two ways in. `publish` is for a producer holding no transaction: it queues the topic and returns, and the publisher
    task coalesces repeats of the same topic within `coalesce_seconds` into one `pg_notify`. `publish_in` is for a
    producer inside a transaction: the notification rides the caller's session, so it fires only if that transaction
    commits.

    One way out. The listening connection feeds every in-process subscriber, so an announcement from any process on
    the same database reaches every reader here.
    """

    def __init__(  # noqa: PLR0913 (every argument after the DSN is keyword-only and defaulted)
        self,
        dsn: str,
        *,
        coalesce_seconds: float = 0.1,
        backoff_cap_seconds: float = 30.0,
        notifier: TopicNotifier | None = None,
        listener: TopicListener | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Hold both connections, not yet open.

        Args:
            dsn: The database both connections open on.
            coalesce_seconds: How long the publisher task gathers before sending, and how long the fan-out task first
                waits before reopening a listening connection it lost.
            backoff_cap_seconds: The longest wait between reopen attempts. The wait doubles after each failed attempt up
                to it, and a reopen that succeeds starts it over.
            notifier: The sending connection. Defaults to a `Notifier` on `dsn`.
            listener: The listening connection. Defaults to a `Listener` on `dsn`.
            sleep: How the wait between reopen attempts is taken.
        """
        self._notifier: TopicNotifier = notifier if notifier is not None else Notifier(dsn)
        self._listener: TopicListener = listener if listener is not None else Listener(dsn, TOPICS_CHANNEL)
        self._coalesce_seconds = coalesce_seconds
        self._backoff_cap_seconds = backoff_cap_seconds
        self._sleep = sleep
        self._pending: list[str] = []
        self._queued = asyncio.Event()
        self._subscribers: set[asyncio.Queue[ChangeNotification]] = set()
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def pending(self) -> list[str]:
        """The topics queued and not yet sent, in the order they were published."""
        return list(self._pending)

    @property
    def subscriber_count(self) -> int:
        """How many in-process readers are attached right now."""
        return len(self._subscribers)

    def publish(self, topic: str) -> None:
        """Announce `topic` when the publisher next sends. It returns at once and never raises.

        Args:
            topic: The topic that changed.
        """
        self._pending.append(topic)
        self._queued.set()

    async def publish_in(self, session: Executes, topic: str) -> None:
        """Announce `topic` on the caller's session or connection, delivered when that transaction commits.

        Args:
            session: The caller's open session or connection.
            topic: The topic that changed.
        """
        await session.execute(text("SELECT pg_notify(:channel, :topic)"), {"channel": TOPICS_CHANNEL, "topic": topic})

    async def flush(self) -> int:
        """Send every queued topic once, in order.

        A send that fails is logged and dropped. The reader's own refetch covers a lost topic.

        Returns:
            How many distinct topics went out.
        """
        topics: list[str] = []
        for topic in self._pending:
            if topic not in topics:
                topics.append(topic)
        self._pending.clear()
        self._queued.clear()
        sent = 0
        for topic in topics:
            try:
                await self._notifier.notify(TOPICS_CHANNEL, topic)
            except Exception:
                _logger.exception("could not announce {topic}", topic=topic)
            else:
                sent += 1
        return sent

    def deliver(self, topic: str) -> None:
        """Hand `topic` to every in-process subscriber, dropping a full reader's oldest change notification.

        Args:
            topic: The topic heard on the channel.
        """
        notification = ChangeNotification(topic=topic, occurred_at=datetime.now(UTC))
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(notification)

    def subscribe(self) -> AsyncGenerator[ChangeNotification]:
        """Every topic this process hears from now on. Closing the iterator drops the queue.

        The reader is registered the moment this is called, not on its first read, so a topic published right after
        never races a reader that has not started pulling yet.

        Returns:
            The change notifications, in the order they were heard.
        """
        queue: asyncio.Queue[ChangeNotification] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers.add(queue)
        return self._drain(queue)

    async def _drain(self, queue: asyncio.Queue[ChangeNotification]) -> AsyncGenerator[ChangeNotification]:
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    async def start(self) -> None:
        """Open the listening connection and run the publisher and fan-out tasks."""
        await self._listener.open()
        self._tasks = [asyncio.create_task(self._publish_forever()), asyncio.create_task(self._listen_forever())]
        for task in self._tasks:
            task.add_done_callback(_log_unexpected_exit)

    async def stop(self) -> None:
        """Stop both tasks and close both connections. Calling it twice is harmless."""
        for task in self._tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self._listener.close()
        await self._notifier.close()

    async def _publish_forever(self) -> None:
        while True:
            await self._queued.wait()
            await asyncio.sleep(self._coalesce_seconds)
            await self.flush()

    async def _listen_forever(self) -> None:
        delay = self._coalesce_seconds
        failures = 0
        while True:
            try:
                await self._listener.open()
                if failures:
                    _logger.info("listener connection restored after {failures} failed attempts", failures=failures)
                    delay, failures = self._coalesce_seconds, 0
                self.deliver(await self._listener.get())
            except OSError as exc:
                if not failures:
                    _logger.warning("listener connection lost, reconnecting with backoff", error=exc)
                failures += 1
                await self._sleep(delay)
                delay = min(delay * 2, self._backoff_cap_seconds)
