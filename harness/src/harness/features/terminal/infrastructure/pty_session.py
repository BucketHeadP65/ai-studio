"""One pty-backed child process, and the command a terminal runs when it is given none."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import shutil
import struct
import subprocess
import termios

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

REAP_GRACE_SECONDS = 3.0
"""How long a `SIGTERM`ed child gets to exit on its own before `SIGKILL` follows."""


def default_command() -> tuple[str, ...]:
    """The command a terminal runs when the caller names none.

    Returns:
        `claude` when it is on `PATH`, else the caller's own `$SHELL`, else `/bin/sh`.
    """
    claude = shutil.which("claude")
    return (claude,) if claude is not None else (os.environ.get("SHELL", "/bin/sh"),)


def _claim_controlling_terminal() -> None:
    """Make the forked child a session leader that owns this pty, after fork and before exec."""
    os.setsid()
    # Without TIOCSCTTY, Ctrl-C and Ctrl-Z reach the wrong process group.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


@dataclass
class PtySession:
    """The live pty: a master fd this process reads and writes, and the child behind the slave end."""

    master_fd: int
    """The pty's master file descriptor, where the child's output is read and its input is written."""
    process: subprocess.Popen[bytes]
    """The child process, running behind the pty's slave end."""
    grace_seconds: float = REAP_GRACE_SECONDS
    """How long `close()` waits after `SIGTERM` before it sends `SIGKILL`."""
    _closed: bool = field(default=False, init=False)

    @classmethod
    def spawn(cls, cwd: Path, command: Sequence[str] | None = None, *, grace_seconds: float = REAP_GRACE_SECONDS) -> PtySession:
        """Open a pty and run `command` behind it, started in `cwd`.

        `command` must never come from a client. Without it the child runs `default_command()`.

        Args:
            cwd: The directory the child starts in.
            command: The program and its arguments. `None` runs `default_command()`.
            grace_seconds: How long `close()` waits after `SIGTERM` before it sends `SIGKILL`.

        Returns:
            The open session, with its child already running.
        """
        argv = tuple(command) if command is not None else default_command()
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(  # noqa: S603 (argv is default_command() or a fixed command, never client input)
            list(argv),
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_claim_controlling_terminal,  # noqa: PLW1509 (the callback only calls setsid and ioctl, so it cannot deadlock on a lock or an import)
            close_fds=True,
        )
        os.close(slave_fd)
        return cls(master_fd=master_fd, process=process, grace_seconds=grace_seconds)

    def resize(self, cols: int, rows: int) -> None:
        """Tell the pty's foreground process the new terminal size.

        This is best effort and never raises. A size outside 0 to 65535 is ignored.

        Args:
            cols: The new width, in columns.
            rows: The new height, in rows.
        """
        with contextlib.suppress(OSError, struct.error):
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def read(self, size: int = 4096) -> bytes:
        """One chunk of the child's output.

        Args:
            size: The most bytes one read returns.

        Returns:
            The bytes read, or `b""` once the child side has closed.
        """
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, os.read, self.master_fd, size)
        except OSError:
            return b""

    def write(self, data: bytes) -> None:
        """Send bytes to the child's input.

        This is best effort and never raises. Bytes a closing pty refuses are dropped.

        Args:
            data: The bytes to send.
        """
        with contextlib.suppress(OSError):
            os.write(self.master_fd, data)

    async def close(self) -> None:
        """Stop the child, reap it, then close the master fd.

        It may be called from any exit path, any number of times. The child gets `SIGTERM`, then `SIGKILL` when it
        has not exited within `grace_seconds`, and it is reaped either way, so no zombie is ever left behind.
        """
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            if not await self._wait_exited(self.grace_seconds):
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self._wait_exited(None)
        # Closing the master before the child is gone can hang os.close forever while a read is blocked on it.
        with contextlib.suppress(OSError):
            os.close(self.master_fd)

    async def _wait_exited(self, grace: float | None) -> bool:
        """Whether the child exited within `grace` seconds (`None` waits forever), waited on in a worker thread."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.process.wait, grace)
        except subprocess.TimeoutExpired:
            return False
        return True
