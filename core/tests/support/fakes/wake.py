"""A worker wake held until cancellation, exposing when it entered and unwound."""

import asyncio

from dataclasses import dataclass, field


@dataclass
class BlockingWake:
    """An external notification wait that never produces work."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    exited: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self, seconds: float) -> None:
        """Wait until released or cancelled, recording both boundaries."""
        del seconds
        self.entered.set()
        try:
            await self.release.wait()
        finally:
            self.exited.set()
