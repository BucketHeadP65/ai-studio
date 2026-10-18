"""The pool this process holds, resolved when something needs it rather than when it was built."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness.session import SessionPool

if TYPE_CHECKING:
    from core.resolver import Resolver


class LiveSessions:
    """`PooledSessions` over this application's own resolver, which looks the pool up on every call.

    It caches nothing. The resolver serves one pool per container.
    """

    def __init__(self, resolver: Resolver) -> None:
        """Look the pool up through `resolver`.

        Args:
            resolver: The resolver this application owns.
        """
        self._resolver = resolver

    async def pool(self) -> SessionPool:
        """The pool this process is holding its sessions in.

        Returns:
            The one pool the resolver serves for this container.
        """
        return await self._resolver.get(SessionPool)
