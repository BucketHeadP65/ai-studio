"""The functional plugins: the owner's off switches over the agent's built-in composition.

The catalog names what may be toggled and what is always on; the persisted set survives a restart;
`Switched` reads the set when a request is composed, so a flip needs no rebuild -- the same
no-restart promise the model knob already keeps.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING

import pytest

from pydantic_ai import FunctionToolset
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.usage import RunUsage

from harness import plugins
from harness.features.selfauthoring.controller import SelfAuthoringController
from tests.support.controllers import a_client_of
from tests.support.fakes.session_log import FakeSessionLog
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.runtime import a_runtime
from tests.support.selfauthoring import a_deployment
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient


class TestPluginState:
    def test_only_sql_is_closed_by_default(self, tmp_path: Path):
        assert plugins.disabled_plugins(a_state(tmp_path)) == frozenset({"sql"})
        assert plugins.plugin_enabled(a_state(tmp_path), "knowledge-base")
        assert not plugins.plugin_enabled(a_state(tmp_path), "sql")

    def test_a_flip_persists_and_reads_back(self, tmp_path: Path):
        plugins.set_plugin(a_state(tmp_path), "shell", enabled=False)
        plugins.set_plugin(a_state(tmp_path), "sql", enabled=True)

        assert not plugins.plugin_enabled(a_state(tmp_path), "shell")
        assert plugins.plugin_enabled(a_state(tmp_path), "sql")
        assert plugins.disabled_plugins(a_state(tmp_path)) == frozenset({"shell"})

    def test_an_always_on_name_refuses_the_toggle(self, tmp_path: Path):
        with pytest.raises(ValueError, match="always on"):
            plugins.set_plugin(a_state(tmp_path), "spend-limits", enabled=False)

    def test_an_unknown_name_refuses_the_toggle(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown plugin"):
            plugins.set_plugin(a_state(tmp_path), "teleport", enabled=False)

    def test_the_catalog_separates_toggleable_from_always_on(self):
        toggleable = {plugin.name for plugin in plugins.TOGGLEABLE}
        always_on = {plugin.name for plugin in plugins.ALWAYS_ON}

        assert "sql" in toggleable
        assert "knowledge-base" in toggleable
        assert "spend-limits" in always_on
        assert not toggleable & always_on

    def test_memory_is_toggleable_and_open_by_default(self, tmp_path: Path):
        assert "memory" not in plugins.DEFAULT_DISABLED
        assert plugins.plugin_enabled(a_state(tmp_path), "memory")
        plugins.check_toggleable("memory")

    def test_follow_me_is_toggleable_and_open_by_default(self, tmp_path: Path):
        assert "follow-me" in {plugin.name for plugin in plugins.TOGGLEABLE}
        assert plugins.plugin_enabled(a_state(tmp_path), "follow-me")
        plugins.check_toggleable("follow-me")


class _TwoReads(AbstractCapability[None]):
    """A stand-in plugin: two named tools and one literal voice."""

    def get_toolset(self) -> FunctionToolset[None]:
        toolset = FunctionToolset[None]()

        async def search(query: str) -> str:
            return query

        async def fetch(unit_id: str) -> str:
            return unit_id

        toolset.add_function(search, takes_ctx=False, name="search")
        toolset.add_function(fetch, takes_ctx=False, name="fetch")
        return toolset

    def get_instructions(self) -> str:
        return "Two reads, one plugin."


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _resolved(switched: plugins.Switched[None]) -> AbstractCapability[None] | None:
    """What this run mounts: the wrapped capability while the plugin is open, nothing while closed."""
    resolved = switched.capability_func(_ctx())
    assert not isinstance(resolved, Awaitable)
    return resolved


async def _visible(switched: plugins.Switched[None]) -> list[str]:
    capability = _resolved(switched)
    if capability is None:
        return []
    toolset = capability.get_toolset()
    assert isinstance(toolset, AbstractToolset)
    return sorted(await toolset.get_tools(_ctx()))


class TestSwitched:
    async def test_a_closed_door_mounts_nothing_and_an_open_one_mounts_the_capability(self, tmp_path: Path):
        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path))

        shown = await _visible(switched)
        plugins.set_plugin(a_state(tmp_path), "knowledge-base", enabled=False)
        hidden = await _visible(switched)

        assert shown == ["fetch", "search"]
        assert hidden == []

    def test_a_closed_door_falls_silent_because_nothing_is_mounted(self, tmp_path: Path):
        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path))

        open_capability = _resolved(switched)
        plugins.set_plugin(a_state(tmp_path), "knowledge-base", enabled=False)
        closed_capability = _resolved(switched)

        assert open_capability is not None
        assert open_capability.get_instructions() == "Two reads, one plugin."
        assert closed_capability is None

    async def test_the_flip_needs_no_rebuild(self, tmp_path: Path):
        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path))

        plugins.set_plugin(a_state(tmp_path), "knowledge-base", enabled=False)
        hidden = await _visible(switched)
        plugins.set_plugin(a_state(tmp_path), "knowledge-base", enabled=True)
        shown = await _visible(switched)

        assert hidden == []
        assert shown == ["fetch", "search"]

    def test_the_door_names_the_capability_for_durable_execution(self, tmp_path: Path):
        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path))

        assert switched.id == "plugin-knowledge-base"

    async def test_a_run_predicate_can_keep_an_open_door_shut(self, tmp_path: Path) -> None:
        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path), only=lambda _ctx: False)
        assert _resolved(switched) is None

        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path), only=lambda _ctx: True)
        assert await _visible(switched) == ["fetch", "search"]

    async def test_a_closed_door_beats_an_agreeing_predicate(self, tmp_path: Path) -> None:
        switched = plugins.Switched(_TwoReads(), plugin="knowledge-base", state=a_state(tmp_path), only=lambda _ctx: True)

        plugins.set_plugin(a_state(tmp_path), "knowledge-base", enabled=False)

        assert _resolved(switched) is None


@pytest.fixture
def client(tmp_path: Path) -> AsyncClient:
    """The harness controller alone, its stores rooted at `tmp_path`."""
    controller = SelfAuthoringController(a_deployment(tmp_path), FakeSessionLog(), FakeSessionPool(a_runtime()))
    return a_client_of(controller)


class TestDoorRoutes:
    async def test_the_listing_carries_every_door_with_its_state(self, client: AsyncClient):
        response = await client.get("/api/harness/plugins")

        assert response.status_code == 200, response.text
        rows = {row["name"]: row for row in response.json()["plugins"]}
        assert rows["knowledge-base"]["enabled"] is True
        assert rows["knowledge-base"]["fixed"] is False
        assert rows["sql"]["enabled"] is False
        assert rows["spend-limits"]["fixed"] is True
        assert rows["spend-limits"]["enabled"] is True
        assert all(row["description"] for row in rows.values())

    async def test_a_flip_answers_the_new_state_and_the_listing_agrees(self, client: AsyncClient):
        flipped = await client.post("/api/harness/plugins/shell/disable")
        listing = await client.get("/api/harness/plugins")

        assert flipped.status_code == 200, flipped.text
        assert flipped.json() == {"name": "shell", "enabled": False, "fixed": False, "description": flipped.json()["description"]}
        rows = {row["name"]: row for row in listing.json()["plugins"]}
        assert rows["shell"]["enabled"] is False

    async def test_enabling_sql_opens_the_default_closed_door(self, client: AsyncClient):
        response = await client.post("/api/harness/plugins/sql/enable")

        assert response.status_code == 200, response.text
        assert response.json()["enabled"] is True

    async def test_an_always_on_plugin_refuses_the_flip(self, client: AsyncClient):
        response = await client.post("/api/harness/plugins/spend-limits/disable")

        assert response.status_code == 400
        assert "always on" in response.json()["detail"]

    async def test_an_unknown_door_is_a_404(self, client: AsyncClient):
        response = await client.post("/api/harness/plugins/teleport/disable")

        assert response.status_code == 404
