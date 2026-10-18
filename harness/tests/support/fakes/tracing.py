"""`Tracing` that ships nothing, so composing an application never reconfigures this process."""

from __future__ import annotations


class FakeTracing:
    """Records that it was started, and configures nothing."""

    def __init__(self) -> None:
        self.started = False

    def start(self) -> None:
        """Remember the start."""
        self.started = True
