"""Reaching the owner on this machine: a macOS notification through osascript, or nothing."""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class CommandRunner(Protocol):
    """Run one command and report its exit code."""

    async def __call__(self, argv: Sequence[str]) -> int:
        """Run the command `argv` and wait for it to end.

        Args:
            argv: The program and its arguments.

        Returns:
            The command's exit code.
        """
        ...


async def _run_osascript(argv: Sequence[str]) -> int:
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await process.wait()


class Notifier(Protocol):
    """One way to tell the owner something short."""

    async def notify(self, title: str, message: str) -> bool:
        """Tell the owner `message` under `title`.

        Args:
            title: The notification's title.
            message: The notification's text.

        Returns:
            True when it was delivered.
        """
        ...


class MacNotifier:
    """`Notifier` over `osascript -e 'display notification ...'`."""

    def __init__(self, run: CommandRunner = _run_osascript) -> None:
        """Hold the command runner.

        Args:
            run: Runs `osascript` and answers its exit code. The default starts a real subprocess.
        """
        self._run = run

    async def notify(self, title: str, message: str) -> bool:
        """Post a macOS notification through `osascript`.

        Args:
            title: The notification's title.
            message: The notification's text.

        Returns:
            False when `osascript` exits non-zero or cannot start.
        """
        script = f'display notification "{_escape(message)}" with title "{_escape(title)}"'
        try:
            return await self._run(("osascript", "-e", script)) == 0
        except OSError:
            return False


class NoopNotifier:
    """`Notifier` for a host with no notification channel."""

    async def notify(self, title: str, message: str) -> bool:
        """Deliver nothing.

        Args:
            title: Ignored.
            message: Ignored.

        Returns:
            Always False.
        """
        del title, message
        return False


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
