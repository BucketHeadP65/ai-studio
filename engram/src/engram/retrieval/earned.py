"""The SQL tools, unmounted until the knowledge base earns them.

`Earned` mounts its wrapped capability only while `earned()` answers true. When it answers false, nothing
is mounted, neither tools nor instructions. An auto-mode knowledge base's `SqlKnowledgeBase` is wrapped in
`Earned`. A fixed-mode knowledge base's is not wrapped.

Compose `Earned` under a host's own plugin switch, never the other way round. Auto decides what is
available, and the owner's plugin switch decides what is permitted.

`SwitchCache` answers from the last completed read of the stored switches. Seed it with a real read at
construction. Every later answer schedules one background refresh, skipped when one is already running
or no event loop is running, so an owner's change reaches the tools within one turn after any later use.
Await `refresh()` to update the cache deterministically.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability, DynamicCapability
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic_ai.tools import RunContext

    from engram.indexing.contracts import RetrievalSwitches


@dataclass
class SwitchCache:
    """The last completed read of one knowledge base's stored retrieval switches, refreshed in the background."""

    read: Callable[[], Awaitable[RetrievalSwitches]]
    """Re-reads the switches. It must fail closed, answering every switch off, when the database read fails."""
    current: RetrievalSwitches
    """The last-completed read. Seed this with a real read taken at construction time."""
    _pending: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    def switches(self) -> RetrievalSwitches:
        """Answer the last completed read and schedule a background refresh.

        Returns:
            The switches from the last completed read.
        """
        self._kick_refresh()
        return self.current

    async def refresh(self) -> None:
        """Read the switches now and replace the cached value."""
        self.current = await self.read()

    def _kick_refresh(self) -> None:
        """Schedule one background `refresh()`, unless one is already running or no loop is running."""
        if self._pending is not None and not self._pending.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pending = loop.create_task(self.refresh())


class Earned(DynamicCapability[AgentDepsT]):
    """The wrapped capability, mounted per run only while the knowledge base's switch is on.

    Compose it under a host's own plugin switch, never the other way round.
    """

    def __init__(self, wrapped: AbstractCapability[AgentDepsT], *, earned: Callable[[], bool], id_suffix: str) -> None:
        """Wrap a capability behind a predicate read on every run.

        Args:
            wrapped: The capability mounted while `earned()` answers true.
            earned: Answers whether the knowledge base's switch is on. It must be synchronous and fast.
            id_suffix: The suffix of this capability's id, `earned:<id_suffix>`.
        """
        self.wrapped = wrapped
        self.earned = earned
        super().__init__(self._resolve, id=f"earned:{id_suffix}")

    def _resolve(self, _ctx: RunContext[AgentDepsT]) -> AbstractCapability[AgentDepsT] | None:
        return self.wrapped if self.earned() else None
