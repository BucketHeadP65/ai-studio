"""What the terminal slice registers: the pty adapter, under the port the terminal controller declares."""

from __future__ import annotations

from wireup import injectable

from harness.features.terminal.infrastructure.pty_session import PtySession
from harness.features.terminal.internal_contracts import PtySpawner


@injectable
def pty_spawner() -> PtySpawner:
    """How the terminal opens a pty: the `PtySession` class itself, whose `spawn` opens a real one.

    Returns:
        The `PtySession` class, under its port.
    """
    return PtySession


REGISTRATIONS: list[object] = [pty_spawner]
"""Everything this slice puts in the container, each under the type its consumers declare."""
