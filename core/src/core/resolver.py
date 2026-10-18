"""The dependency-resolution port: ask for a type, get an instance of it.

A consumer that pulls services out of a dependency-injection container by type depends on this port
rather than on a concrete container. An adapter over a real container satisfies it structurally.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Resolver(Protocol):
    """Resolves a registered service to an instance of its own type."""

    async def get[T](self, klass: type[T]) -> T:
        """Return the service registered under `klass`.

        Args:
            klass: The type the service is registered under.

        Returns:
            An instance of `klass`.
        """
        ...
