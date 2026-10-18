"""The port the terminal controller opens a pty through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.terminal.infrastructure.pty_session import PtySession


class PtySpawner(Protocol):
    """How the terminal controller opens one pty-backed child process in a starting directory."""

    def spawn(self, cwd: Path) -> PtySession:
        """Open a pty and start a child process behind it in `cwd`.

        The caller has already checked `cwd` against every gate. The caller owns the session it gets back and must
        `close()` it on every exit path.

        Args:
            cwd: The directory the child starts in.

        Returns:
            The open session, with its child already running.
        """
        ...
