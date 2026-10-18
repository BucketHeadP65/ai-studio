"""`PtySession` alone: a real pty behind a real child, owned and torn down by one caller.

No mock of the pty layer -- `pty.openpty()` and `/bin/cat` are the real thing, exactly as the
websocket route uses them once every gate has passed. What's pinned here is the resource
contract: bytes actually round-trip through the child, and `close()` steps no zombie behind,
however the pump loop that owned the session would have gotten there.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

import pytest

from harness.features.terminal.infrastructure.pty_session import PtySession

if TYPE_CHECKING:
    from pathlib import Path


async def test_bytes_written_to_the_session_round_trip_through_a_real_child(tmp_path: Path) -> None:
    session = PtySession.spawn(tmp_path, command=("/bin/cat",))
    try:
        session.write(b"hello pty\n")
        collected = b""
        for _ in range(20):
            collected += await session.read()
            if b"hello pty" in collected:
                break
        assert b"hello pty" in collected
    finally:
        await session.close()


@pytest.mark.timeout(10)
async def test_close_reaps_the_child_no_zombie_survives_it(tmp_path: Path) -> None:
    # An idle /bin/cat steps the output pump's os.read() blocked, the exact shape a
    # close()-ordering regression hangs on -- a reintroduced deadlock here must fail loudly
    # (timeout) rather than hang the gate run indefinitely.
    session = PtySession.spawn(tmp_path, command=("/bin/cat",))
    pid = session.process.pid

    os.kill(pid, 0)  # alive before close -- otherwise the assertion below proves nothing

    await session.close()

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        message = f"pid {pid} is still reachable after close()"
        raise AssertionError(message)


async def test_close_is_idempotent(tmp_path: Path) -> None:
    session = PtySession.spawn(tmp_path, command=("/bin/cat",))
    await session.close()
    await session.close()  # a second close from another exit path must not raise


async def test_a_child_that_ignores_sigterm_is_escalated_to_sigkill(tmp_path: Path) -> None:
    trap_sigterm = "trap '' TERM; while true; do sleep 0.05; done"
    session = PtySession.spawn(tmp_path, command=("/bin/sh", "-c", trap_sigterm), grace_seconds=0.3)
    pid = session.process.pid

    await session.close()

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        message = f"pid {pid} survived SIGTERM and was never escalated to SIGKILL"
        raise AssertionError(message)
