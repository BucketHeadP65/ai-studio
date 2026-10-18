"""`PriceUpdater` that only remembers whether it was started and stopped."""

from __future__ import annotations


class FakePriceUpdater:
    """Records the two calls the app makes, and can script a failing stop."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.failure: Exception | None = None

    def start(self) -> None:
        """Remember the start."""
        self.started = True

    def stop(self) -> None:
        """Remember the stop, then raise `failure` when one is scripted."""
        self.stopped = True
        if self.failure is not None:
            raise self.failure
