"""Resolving one service late, for the callers that cannot take it as an argument."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from core.resolver import Resolver

if TYPE_CHECKING:
    from wireup import AsyncContainer


@runtime_checkable
class Container(Resolver, Protocol):
    """The container surface an application entry point resolves from: the typed `get`, and `close` for whoever owns its lifetime."""

    async def close(self) -> None:
        """Release what the container holds."""
        ...


class Stateful(Protocol):
    """An application carrying the state its composition root wrote onto it."""

    @property
    def state(self) -> object:
        """Whatever the composition root put on the application."""
        ...


@dataclass(frozen=True, slots=True)
class ContainerResolver:
    """A `Resolver` backed by a wireup container.

    It raises on a `None` answer and hands back a plain `T`.
    """

    container: AsyncContainer

    async def get[T](self, klass: type[T]) -> T:
        """The service registered under `klass`.

        Args:
            klass: The concrete type the service is registered under.

        Returns:
            The service the container holds for `klass`.

        Raises:
            LookupError: when the container has no registration for `klass`.
        """
        service = await self.container.get(klass)
        if service is None:  # pragma: no cover (wireup raises on a missing registration rather than answering None)
            msg = f"the container has no registration for {klass.__name__}"
            raise LookupError(msg)
        return service

    async def close(self) -> None:
        """Release the container's resources."""
        await self.container.close()


def resolver_of(app: Stateful) -> Resolver:
    """The resolver the composition root put on the application.

    Args:
        app: The application whose state carries the resolver.

    Returns:
        The resolver found on `app.state`.

    Raises:
        TypeError: when the application carries no resolver.
    """
    resolver = getattr(app.state, "resolver", None)
    if not isinstance(resolver, Resolver):
        msg = f"the application carries a {type(resolver).__name__} where a resolver was expected"
        raise TypeError(msg)
    return resolver
