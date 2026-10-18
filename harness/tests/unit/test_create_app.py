"""The composition root: what `create_app()` builds, and that it builds a fresh one every time."""

from __future__ import annotations

import re

from pathlib import Path

from core.observability import PriceUpdater, Tracing
from core.resolver import Resolver

import harness.container as container_module

from harness.main import create_app
from harness.settings import HarnessSettings
from tests.support.fakes.prices import FakePriceUpdater
from tests.support.fakes.tracing import FakeTracing
from tests.support.serving import a_served_client


class TestOneContainerPerApplication:
    def test_each_application_builds_its_own(self) -> None:
        first = create_app(HarnessSettings())
        second = create_app(HarnessSettings())

        assert first is not second
        assert first.state.wireup_container is not second.state.wireup_container

    def test_the_container_module_holds_none_of_its_own(self) -> None:
        assert not hasattr(container_module, "container")


class TestTheResolverIsOnTheApplication:
    def test_the_state_carries_one(self) -> None:
        built = create_app(HarnessSettings())

        assert isinstance(built.state.resolver, Resolver)


class TestTheContainerServesEverySection:
    async def test_each_section_resolves_under_its_own_type(self) -> None:
        settings = HarnessSettings()
        container = container_module.create_container(settings)

        assert await container.get(type(settings.run)) is settings.run
        assert await container.get(type(settings.terminal)) is settings.terminal

        await container.close()


class TestTheLifespanStartsNothingOfItsOwn:
    """Everything the boot switches on process-wide comes from the container, so a test can answer for it.

    Tracing configures the whole process and the price refresh polls in a daemon thread. Built
    inside the lifespan rather than resolved, a test that composes an application would ship spans
    with this deployment's own token and leave a thread fetching prices behind it.
    """

    async def test_it_starts_and_stops_the_ones_it_was_handed(self, tmp_path: Path) -> None:
        tracing, prices = FakeTracing(), FakePriceUpdater()

        async with a_served_client(state=tmp_path, answering={Tracing: tracing, PriceUpdater: prices}):
            assert tracing.started, "the lifespan built its own tracing instead of resolving one"
            assert prices.started, "the lifespan built its own price updater instead of resolving one"
            assert not prices.stopped

        assert prices.stopped, "the price refresh outlived the application that started it"


class TestNothingImportsAnApplicationFromTheModule:
    """One shared application is one shared container, and a test that takes it shares state with every other one."""

    def test_the_main_module_holds_no_application_of_its_own(self) -> None:
        import harness.main as main_module

        assert not hasattr(main_module, "app")

    def test_no_test_file_imports_one(self) -> None:
        tests = Path(__file__).resolve().parents[1]
        offenders = sorted(
            path.relative_to(tests).as_posix()
            for path in tests.rglob("test_*.py")
            if re.search(r"^from harness\.main import .*\bapp\b", path.read_text(encoding="utf-8"), re.MULTILINE)
        )

        assert offenders == []
