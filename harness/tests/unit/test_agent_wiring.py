"""The agent is a registration: the container hands back one session, built from the ports."""

from __future__ import annotations

import typing

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import TopicSink

from harness.adapters.store import DatabaseBinding
from harness.container import create_container
from harness.domain.ports import SessionEvents
from harness.features.authoring import wiring
from harness.features.scheduling.contracts import Scheduler
from harness.session import Runtime
from harness.settings import HarnessSettings
from tests.support.container import answering_from_memory, rooted_at

if TYPE_CHECKING:
    from pathlib import Path

REACHES_THE_DEPLOYMENT = frozenset({SessionEvents, TopicSink, Scheduler, DatabaseBinding})
"""The collaborators a unit test cannot resolve, and why each one.

The first three are built over the change channel or a database pool, so asking the container for
one dials Postgres. `DatabaseBinding` reads engram's own settings file to say which databases this
process opens, and that file belongs to a deployment rather than to this repository. `serving()`
answers for all four with fakes; here they are named instead, because answering for the very thing
a case resolves proves nothing.
"""


def collaborators() -> dict[str, type]:
    """Every type the session factory declares, read off the factory rather than listed here."""
    hints = typing.get_type_hints(wiring.session)
    hints.pop("return")
    return hints


def resolvable() -> list[tuple[str, type]]:
    """The collaborators this test resolves for real, in the order the factory declares them."""
    return [(name, kind) for name, kind in collaborators().items() if kind not in REACHES_THE_DEPLOYMENT]


class TestTheSessionComesFromTheContainer:
    async def test_it_resolves_once_and_stays_the_same_object(self, tmp_path: Path) -> None:
        container = create_container(rooted_at(HarnessSettings(), tmp_path))

        with answering_from_memory(container):
            first = await container.get(Runtime)
            second = await container.get(Runtime)

        assert first is second
        await container.close()


class TestEveryCollaboratorTheSessionTakesResolves:
    """Every registration in the graph is the real one, so what is proved is the wiring, not the fakes.

    Overriding `Runtime` proves only that the type is a known registration -- the factory's own body
    never runs, so none of its arguments is ever asked of the container. These cases ask for each of
    them instead. What is answered for is the I/O leaves alone (`IO_LEAVES`): each of those opens a
    socket or reads a file belonging to a deployment rather than to this checkout, so a case that
    resolved them for real would prove a machine's own state rather than this graph.
    """

    def test_every_declared_collaborator_is_either_resolved_here_or_named_as_out_of_reach(self) -> None:
        named = {name for name, kind in collaborators().items() if kind in REACHES_THE_DEPLOYMENT}
        covered = {name for name, _ in resolvable()} | named

        assert covered == set(collaborators())

    @pytest.mark.parametrize(("name", "kind"), resolvable(), ids=[name for name, _ in resolvable()])
    async def test_the_container_answers_for_it(self, name: str, kind: type, tmp_path: Path) -> None:
        container = create_container(rooted_at(HarnessSettings(), tmp_path))

        try:
            with answering_from_memory(container):
                answered = await container.get(kind)
                again = await container.get(kind)
        finally:
            await container.close()

        assert answered is not None, name
        assert answered is again, f"{name} is not a singleton"
