from dataclasses import dataclass

import pytest
import wireup

from wireup.errors import UnknownServiceRequestedError

from core.wiring.resolver import Container, ContainerResolver, resolver_of


@dataclass(frozen=True)
class Thing:
    """A registered service."""

    name: str


@dataclass(frozen=True)
class Missing:
    """A service nobody registered."""

    name: str


@dataclass
class State:
    """What a composition root writes onto its application."""

    resolver: object


@dataclass
class App:
    """An application carrying that state."""

    state: State


def container_of(thing: Thing) -> wireup.AsyncContainer:
    return wireup.create_async_container(injectables=[wireup.instance(thing, as_type=Thing)])


class TestContainerResolver:
    async def test_resolves_a_registered_type(self):
        thing = Thing(name="one")
        resolver = ContainerResolver(container_of(thing))

        assert await resolver.get(Thing) is thing

        await resolver.close()

    async def test_refuses_a_type_nobody_registered(self):
        resolver = ContainerResolver(container_of(Thing(name="one")))

        with pytest.raises(UnknownServiceRequestedError):
            await resolver.get(Missing)

        await resolver.close()


class TestResolverOf:
    async def test_answers_with_the_resolver_the_application_carries(self):
        resolver = ContainerResolver(container_of(Thing(name="one")))

        assert resolver_of(App(state=State(resolver=resolver))) is resolver

        await resolver.close()

    def test_refuses_an_application_carrying_something_else(self):
        with pytest.raises(TypeError, match="resolver"):
            resolver_of(App(state=State(resolver="not a resolver")))


class TestTheContainerSurface:
    async def test_the_resolver_is_the_container_surface_an_entry_point_owns(self):
        resolver = ContainerResolver(container_of(Thing(name="one")))

        assert isinstance(resolver, Container)

        await resolver.close()
