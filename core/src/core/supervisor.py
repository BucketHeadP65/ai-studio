"""One child process kept alive for as long as its parent runs.

`SupervisedChild` starts the child through the callable it is given, never a command of its own.
A child that exits by itself is started again after a capped exponential backoff.
`stop` terminates it and never starts another.
"""

from __future__ import annotations

import asyncio
import contextlib

from typing import TYPE_CHECKING

from core.observability import get_logger

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from collections.abc import Awaitable, Callable

_logger = get_logger(__name__)

type Spawn = Callable[[], Awaitable[Process]]
"""Starts the child and answers the running process."""


class SupervisedChild:
    """A child process, restarted with backoff whenever it exits before `stop`."""

    def __init__(
        self,
        spawn: Spawn,
        *,
        name: str,
        backoff_seconds: float = 1.0,
        backoff_cap_seconds: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Bind what to start and how patiently to retry it.

        Args:
            spawn: Starts the child and answers the process, called once per attempt.
            name: What the child is called in the restart log line.
            backoff_seconds: How long to wait before the first restart.
            backoff_cap_seconds: The longest wait between restarts. The delay doubles up to it and
                stays there, so a child that cannot start never busy-loops.
            sleep: How the wait is taken.
        """
        self._spawn = spawn
        self._name = name
        self._backoff_seconds = backoff_seconds
        self._backoff_cap_seconds = backoff_cap_seconds
        self._sleep = sleep
        self._process: Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        """True while the supervising task exists and has not finished."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the child and keep it alive on a background task. Call once, from a running loop."""
        self._stopping = False
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Terminate the child and stop supervising. Calling it before `start` is a no-op.

        A supervising task that already ended on a failure is reported here and never re-raised,
        so shutdown finishes whatever went wrong while the parent was serving.
        """
        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
                await self._process.wait()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                _logger.exception("supervising {child} had already ended on a failure", child=self._name)
            self._task = None
        self._process = None

    async def _supervise(self) -> None:
        """Start the child, wait for it, and start it again until `stop` is called, on the same backoff after any failure."""
        delay = self._backoff_seconds
        while not self._stopping:
            try:
                self._process = await self._spawn()
                code = await self._process.wait()
            except Exception:
                _logger.exception("supervising {child} failed, retrying in {delay:.1f}s", child=self._name, delay=delay)
            else:
                if self._stopping:
                    return
                _logger.warning("{child} exited with code {code}, restarting in {delay:.1f}s", child=self._name, code=code, delay=delay)
            await self._sleep(delay)
            delay = min(delay * 2, self._backoff_cap_seconds)
