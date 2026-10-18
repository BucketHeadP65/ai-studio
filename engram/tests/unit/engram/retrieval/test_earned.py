"""`Earned` mounts nothing until a knowledge base's stored switch says it may, and `SwitchCache` feeds it.

`Earned` mirrors `harness/src/harness/doors.py`'s `Switched` (same `DynamicCapability` shape, a
different predicate) -- these tests mirror `harness/tests/unit/test_doors.py::TestSwitched` for
that reason: `capability_func(ctx)` is the sync resolve every door of this shape answers through.
`SwitchCache` is tested in isolation, with a controllable fake read, driving `refresh()` directly
rather than the background-task path -- the deterministic route the module docstring names.
"""

from __future__ import annotations

import asyncio

from collections.abc import Awaitable
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from engram.indexing.contracts import RetrievalSwitches
from engram.retrieval.earned import Earned, SwitchCache


class _StubCapability(AbstractCapability[None]):
    """A stand-in wrapped capability: these tests are about earning, never about its own shape."""


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _resolved(earned: Earned[None]) -> AbstractCapability[None] | None:
    """What this run mounts: the wrapped capability while earned, nothing while not."""
    resolved = earned.capability_func(_ctx())
    assert not isinstance(resolved, Awaitable)
    return resolved


class TestEarned:
    def test_not_earned_mounts_nothing(self) -> None:
        earned = Earned(_StubCapability(), earned=lambda: False, id_suffix="sql:kitchen")

        assert _resolved(earned) is None

    def test_earned_mounts_the_wrapped_capability(self) -> None:
        wrapped = _StubCapability()
        earned = Earned(wrapped, earned=lambda: True, id_suffix="sql:kitchen")

        assert _resolved(earned) is wrapped

    def test_the_id_is_stable_across_runs(self) -> None:
        earned = Earned(_StubCapability(), earned=lambda: True, id_suffix="sql:kitchen")

        assert earned.id == "earned:sql:kitchen"
        first_run = _resolved(earned)
        second_run = _resolved(earned)
        assert earned.id == "earned:sql:kitchen"
        assert first_run is second_run  # the same wrapped instance, not a fresh one per run


@dataclass
class _FakeSwitchRead:
    """A controllable async switches source: the value `refresh()` reads next, and a call count."""

    value: RetrievalSwitches
    calls: int = 0

    async def __call__(self) -> RetrievalSwitches:
        self.calls += 1
        return self.value


class TestSwitchCache:
    async def test_seeded_on_answers_the_seed(self) -> None:
        read = _FakeSwitchRead(RetrievalSwitches(sql=True))
        cache = SwitchCache(read=read, current=RetrievalSwitches(sql=True))

        assert cache.switches().sql is True

    async def test_refresh_replaces_the_cached_value(self) -> None:
        read = _FakeSwitchRead(RetrievalSwitches(sql=True))
        cache = SwitchCache(read=read, current=RetrievalSwitches(sql=True))
        read.value = RetrievalSwitches(sql=False)

        await cache.refresh()

        assert cache.switches().sql is False

    async def test_earned_through_the_cache_goes_dark_after_a_flip_and_a_refresh(self) -> None:
        """The end-to-end contract `Earned` relies on: a flip lands via `refresh()`, not scheduling."""
        read = _FakeSwitchRead(RetrievalSwitches(sql=True))
        cache = SwitchCache(read=read, current=RetrievalSwitches(sql=True))
        wrapped = _StubCapability()
        earned = Earned(wrapped, earned=lambda: cache.switches().sql, id_suffix="sql:kitchen")
        assert _resolved(earned) is wrapped

        read.value = RetrievalSwitches(sql=False)
        await cache.refresh()

        assert _resolved(earned) is None

    async def test_switches_kicks_a_background_refresh_that_lands_by_the_next_read(self) -> None:
        read = _FakeSwitchRead(RetrievalSwitches(sql=True))
        cache = SwitchCache(read=read, current=RetrievalSwitches(sql=False))  # seed deliberately stale

        first = cache.switches()  # answers the stale seed, kicks a background refresh
        await asyncio.sleep(0)  # let the kicked task run to completion
        second = cache.switches()

        assert first.sql is False
        assert second.sql is True
        assert read.calls == 1

    async def test_switches_does_not_duplicate_an_in_flight_refresh(self) -> None:
        gate = asyncio.Event()
        calls = 0

        async def slow_read() -> RetrievalSwitches:
            nonlocal calls
            calls += 1
            await gate.wait()
            return RetrievalSwitches(sql=True)

        cache = SwitchCache(read=slow_read, current=RetrievalSwitches(sql=False))

        cache.switches()  # kicks the first refresh, now blocked on `gate`
        await asyncio.sleep(0)
        cache.switches()  # a second read while the first refresh is still in flight

        gate.set()
        await asyncio.sleep(0)

        assert calls == 1

    def test_switches_with_no_running_loop_answers_the_cache_without_raising(self) -> None:
        """A sync caller outside any event loop still gets an answer -- the background kick just skips."""
        read = _FakeSwitchRead(RetrievalSwitches(sql=True))
        cache = SwitchCache(read=read, current=RetrievalSwitches(sql=True))

        assert cache.switches().sql is True
        assert read.calls == 0
