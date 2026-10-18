"""A `Wake` nobody notifies, so a background loop waits its own cadence and reaches no database."""

from __future__ import annotations

import asyncio


class FakeWake:
    """Sleeps exactly as long as it was asked to, and records every wait."""

    def __init__(self) -> None:
        self.waited: list[float] = []

    async def wait(self, seconds: float) -> None:
        """Sleep `seconds`, which is what a wake no other process notifies does."""
        self.waited.append(seconds)
        await asyncio.sleep(seconds)
