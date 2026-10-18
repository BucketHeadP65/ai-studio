"""The harness's HTTP surface: the owner's review-and-activate plugin, reachable from the dashboard.

Same acts as `harness` (`test_harness_cli.py`'s plugin), over the identical on-disk stores --
these tests build authored artifacts directly through the stores, exactly as the CLI tests do, and
drive the routes through a bare FastAPI app, exactly as the terminal routes' tests do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import StepRegistry
from fastapi.routing import APIRoute
from pydantic_ai.messages import ModelResponse, TextPart

from harness.features.selfauthoring.controller import SelfAuthoringController
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles
from harness.features.selfauthoring.infrastructure.promotion import promoted_capability_types
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from tests.support.container import a_client, an_app
from tests.support.controllers import a_client_of
from tests.support.fakes.session_log import FakeSessionLog
from tests.support.fakes.sessions import FakeSessionPool
from tests.support.runtime import a_runtime
from tests.support.selfauthoring import FakeActivationLog, a_deployment

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from httpx import AsyncClient

    from harness.session import Runtime

STUDIO_ORIGIN = "http://localhost:3031"

_VALID_SOURCE = """
from pydantic_ai.capabilities import AbstractCapability


class Marker(AbstractCapability):
    '''Valid, does nothing.'''
"""

_PROMOTABLE_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class Marker(AbstractCapability):
    '''Meets the promotion contract: a declared serialization name (the class name, by default)
    and a dataclass declared on the class itself.'''
"""

_VALID_STEP_SOURCE = """
async def double(x: int) -> int:
    '''Doubles x.'''
    return x * 2
"""

_VALID_AGENT_DOCUMENT = "instructions: Read the ledger and say whether the quarter looks healthy.\n"


@pytest.fixture
def studio_app(tmp_path: Path) -> FastAPI:
    """The whole application, composed for this test, for the checks about what it publishes."""
    return an_app(state=tmp_path / "app-state")


@pytest.fixture
def session() -> Runtime:
    """The session `composition` and `put_selection` read: an empty registry and no conversation."""
    return a_runtime()


@pytest.fixture
def client(tmp_path: Path, session: Runtime) -> AsyncClient:
    """The harness controller alone, its stores rooted at `tmp_path`."""
    controller = SelfAuthoringController(a_deployment(tmp_path), FakeSessionLog(), FakeSessionPool(session))
    return a_client_of(controller)


def _custom_capabilities(tmp_path: Path) -> FileCustomCapabilities:
    return FileCustomCapabilities(tmp_path / "custom" / "capabilities", activations=FakeActivationLog())


def _step_store(tmp_path: Path) -> FileCustomSteps:
    return FileCustomSteps(tmp_path / "custom" / "steps", activations=FakeActivationLog())


def _agent_store(tmp_path: Path) -> FileCustomAgents:
    return FileCustomAgents(tmp_path / "custom" / "agents", activations=FakeActivationLog())


async def test_listing_an_empty_kind_answers_no_artifacts(client: AsyncClient) -> None:
    response = await client.get("/api/harness/capability")

    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "capability", "artifacts": []}


async def test_listing_a_pending_capability_says_activate_is_next(client: AsyncClient, tmp_path: Path) -> None:
    _custom_capabilities(tmp_path).write("marker", _VALID_SOURCE)

    response = await client.get("/api/harness/capability")

    assert response.status_code == 200, response.text
    [row] = response.json()["artifacts"]
    assert row["name"] == "marker"
    assert row["status"] == "disabled"
    assert row["descriptor"] == "Marker"
    assert row["last_error"] is None
    assert row["promoted"] is False
    assert row["next_step"] == "activate"


async def test_listing_a_broken_capability_carries_its_error_and_no_next_step(client: AsyncClient, tmp_path: Path) -> None:
    _custom_capabilities(tmp_path).write("broken", "this is not python at all !!!")

    response = await client.get("/api/harness/capability")

    assert response.status_code == 200, response.text
    [row] = response.json()["artifacts"]
    assert row["last_error"] is not None
    assert row["next_step"] is None, "broken code has nothing here to activate into"


async def test_listing_an_active_unpromoted_capability_says_promote_is_next(client: AsyncClient, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    store.write("marker", _PROMOTABLE_SOURCE)
    store.activate("marker")

    response = await client.get("/api/harness/capability")

    assert response.status_code == 200, response.text
    [row] = response.json()["artifacts"]
    assert row["status"] == "active"
    assert row["promoted"] is False
    assert row["next_step"] == "promote"


async def test_listing_a_promoted_capability_has_no_further_next_step(client: AsyncClient, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    store.write("marker", _PROMOTABLE_SOURCE)
    store.activate("marker")
    promoted = await client.post("/api/harness/capability/marker/promote")
    assert promoted.status_code == 200, promoted.text

    response = await client.get("/api/harness/capability")

    assert response.status_code == 200, response.text
    [row] = response.json()["artifacts"]
    assert row["promoted"] is True
    assert row["next_step"] is None


async def test_a_step_and_an_agent_never_carry_a_promoted_flag(client: AsyncClient, tmp_path: Path) -> None:
    _step_store(tmp_path).write("double", _VALID_STEP_SOURCE)
    _agent_store(tmp_path).write("weigher", _VALID_AGENT_DOCUMENT)

    step_response = await client.get("/api/harness/step")
    agent_response = await client.get("/api/harness/agent")

    assert step_response.status_code == 200, step_response.text
    assert agent_response.status_code == 200, agent_response.text
    [step_row] = step_response.json()["artifacts"]
    [agent_row] = agent_response.json()["artifacts"]
    assert step_row["promoted"] is None
    assert agent_row["promoted"] is None
    assert step_row["descriptor"] == "double"
    assert agent_row["descriptor"] == "(host default model)"


async def test_activating_a_capability_flips_it_live(client: AsyncClient, tmp_path: Path) -> None:
    _custom_capabilities(tmp_path).write("marker", _VALID_SOURCE)

    response = await client.post("/api/harness/capability/marker/activate")

    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "capability", "name": "marker", "status": "active"}
    [instance] = _custom_capabilities(tmp_path).load_active()
    assert type(instance).__name__ == "Marker"


async def test_activating_an_unknown_capability_name_is_a_404(client: AsyncClient) -> None:
    response = await client.post("/api/harness/capability/nobody/activate")

    assert response.status_code == 404
    assert "nobody" in response.json()["detail"]


async def test_activating_a_capability_with_a_stored_error_is_a_400(client: AsyncClient, tmp_path: Path) -> None:
    record = _custom_capabilities(tmp_path).write("broken", "this is not python at all !!!")
    assert record.last_error is not None

    response = await client.post("/api/harness/capability/broken/activate")

    assert response.status_code == 400
    assert record.last_error in response.json()["detail"]


async def test_disabling_a_capability_stops_it_loading(client: AsyncClient, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")

    response = await client.post("/api/harness/capability/marker/disable")

    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "capability", "name": "marker", "status": "disabled"}
    assert _custom_capabilities(tmp_path).load_active() == []


async def test_disabling_an_unknown_name_is_a_404(client: AsyncClient) -> None:
    response = await client.post("/api/harness/step/nobody/disable")

    assert response.status_code == 404
    assert "nobody" in response.json()["detail"]


async def test_activating_a_step_registers_its_functions(client: AsyncClient, tmp_path: Path) -> None:
    _step_store(tmp_path).write("double", _VALID_STEP_SOURCE)

    response = await client.post("/api/harness/step/double/activate")

    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "step", "name": "double", "status": "active"}
    registry = StepRegistry()
    assert _step_store(tmp_path).load_active(registry) == ("double",)


async def test_activating_an_agent_makes_it_resolvable(client: AsyncClient, tmp_path: Path) -> None:
    _agent_store(tmp_path).write("weigher", _VALID_AGENT_DOCUMENT)

    response = await client.post("/api/harness/agent/weigher/activate")

    assert response.status_code == 200, response.text
    document = _agent_store(tmp_path).resolve_active("weigher")
    assert document["instructions"] == "Read the ledger and say whether the quarter looks healthy."


async def test_promoting_flips_it_into_the_registry(client: AsyncClient, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    store.write("marker", _PROMOTABLE_SOURCE)
    store.activate("marker")

    response = await client.post("/api/harness/capability/marker/promote")

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "marker", "promoted": True}
    [promoted] = promoted_capability_types(store)
    assert promoted.__name__ == "Marker"


async def test_promoting_before_activating_is_a_400(client: AsyncClient, tmp_path: Path) -> None:
    _custom_capabilities(tmp_path).write("marker", _PROMOTABLE_SOURCE)

    response = await client.post("/api/harness/capability/marker/promote")

    assert response.status_code == 400
    assert "activate" in response.json()["detail"]


async def test_promoting_an_unknown_capability_is_a_404(client: AsyncClient) -> None:
    response = await client.post("/api/harness/capability/nobody/promote")

    assert response.status_code == 404
    assert "nobody" in response.json()["detail"]


async def test_demoting_removes_it_from_the_registry(client: AsyncClient, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    store.write("marker", _PROMOTABLE_SOURCE)
    store.activate("marker")
    promoted = await client.post("/api/harness/capability/marker/promote")
    assert promoted.status_code == 200, promoted.text

    response = await client.post("/api/harness/capability/marker/demote")

    assert response.status_code == 200, response.text
    assert response.json() == {"name": "marker", "promoted": False}
    assert promoted_capability_types(store) == ()


async def test_demoting_an_unpromoted_capability_is_a_404(client: AsyncClient) -> None:
    response = await client.post("/api/harness/capability/nobody/demote")

    assert response.status_code == 404


async def test_an_unwired_kind_in_the_path_is_a_422(client: AsyncClient) -> None:
    response = await client.get("/api/harness/workflow")

    assert response.status_code == 422, "the closed set of kinds is enforced at the plugin, not deep inside a store lookup"


async def test_an_unwired_kind_refuses_activation_too(client: AsyncClient) -> None:
    response = await client.post("/api/harness/workflow/anything/activate")

    assert response.status_code == 422


def test_the_harness_routes_are_declared_on_the_router() -> None:
    """The declared paths: an application binds these to an instance once its lifespan has run."""
    declared = {route.path for route in SelfAuthoringController.router.routes if isinstance(route, APIRoute)}

    assert "/api/harness/{kind}" in declared
    assert "/api/harness/{kind}/{name}/activate" in declared
    assert "/api/harness/{kind}/{name}/disable" in declared
    assert "/api/harness/capability/{name}/promote" in declared
    assert "/api/harness/capability/{name}/demote" in declared


async def test_the_browser_may_activate_from_the_dashboard_origin(studio_app: FastAPI) -> None:
    """CORS: a preflight from the dashboard's own origin is answered, like every other /api route."""
    async with a_client(studio_app) as client:
        response = await client.options(
            "/api/harness/capability/marker/activate",
            headers={"Origin": STUDIO_ORIGIN, "Access-Control-Request-Method": "POST"},
        )

    assert response.headers.get("access-control-allow-origin") == STUDIO_ORIGIN


async def _double_step(x: int) -> int:
    """Doubles x."""
    return x * 2


async def test_the_composition_reports_the_live_world(client: AsyncClient, tmp_path: Path, session: Runtime) -> None:
    """Model, engine, the registry's own step names, the promoted set, and per-kind active counts."""
    session.registry.register(_double_step, name="double")
    store = _custom_capabilities(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")

    response = await client.get("/api/harness/composition")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"]
    assert body["run_engine"] in {"in_process", "temporal"}
    assert body["steps_registered"] == ["double"]
    assert body["promoted"] == []
    assert body["active"] == {"capability": 1, "step": 0, "agent": 0, "profile": 0}


def test_the_composition_route_is_declared_on_the_router() -> None:
    declared = {route.path for route in SelfAuthoringController.router.routes if isinstance(route, APIRoute)}

    assert "/api/harness/composition" in declared
# --- DELETE /api/harness/{kind}/{name} ------------------------------------------------------


async def test_deleting_a_disabled_step_removes_it(client: AsyncClient, tmp_path: Path) -> None:
    _step_store(tmp_path).write("double", _VALID_STEP_SOURCE)

    response = await client.delete("/api/harness/step/double")
    listing = await client.get("/api/harness/step")

    assert response.status_code == 200, response.text
    assert response.json() == {"kind": "step", "name": "double", "deleted": True}
    assert listing.json()["artifacts"] == []


async def test_deleting_an_active_capability_is_refused_naming_the_fix(client: AsyncClient, tmp_path: Path) -> None:
    store = _custom_capabilities(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")

    response = await client.delete("/api/harness/capability/marker")

    assert response.status_code == 400
    assert "disable" in response.json()["detail"]


async def test_deleting_an_unknown_agent_is_a_404(client: AsyncClient) -> None:
    response = await client.delete("/api/harness/agent/ghost")

    assert response.status_code == 404
# --- profiles: the fourth kind, and the selection ------------------------------------------


def _profile_store(tmp_path: Path) -> FileCustomProfiles:
    return FileCustomProfiles(tmp_path / "custom" / "profiles", activations=FakeActivationLog())


async def test_listing_profiles_shows_their_plugins_as_the_descriptor(client: AsyncClient, tmp_path: Path) -> None:
    _profile_store(tmp_path).write("researcher", "plugins: [knowledge-base, leveled-reads]\n")

    response = await client.get("/api/harness/profile")

    assert response.status_code == 200, response.text
    [row] = response.json()["artifacts"]
    assert row["name"] == "researcher"
    assert row["status"] == "disabled"
    assert "knowledge-base" in row["descriptor"]


async def test_selection_round_trips_and_reports_the_profile_in_effect(client: AsyncClient, tmp_path: Path) -> None:
    store = _profile_store(tmp_path)
    store.write("researcher", "plugins: [knowledge-base]\n")
    store.activate("researcher")

    empty = await client.get("/api/harness/selection")
    put = await client.put("/api/harness/selection", json={"name": "researcher"})
    chosen = await client.get("/api/harness/selection")
    cleared = await client.put("/api/harness/selection", json={"name": None})

    assert empty.json() == {"name": None}
    assert put.status_code == 200, put.text
    assert chosen.json() == {"name": "researcher"}
    assert cleared.json() == {"name": None}


async def test_selecting_a_pending_profile_is_refused(client: AsyncClient, tmp_path: Path) -> None:
    _profile_store(tmp_path).write("researcher", "plugins: [knowledge-base]\n")

    response = await client.put("/api/harness/selection", json={"name": "researcher"})

    assert response.status_code == 400
    assert "active" in response.json()["detail"]


async def test_switching_mid_conversation_is_refused_naming_the_fix(client: AsyncClient, tmp_path: Path, session: Runtime) -> None:
    session.conversation.history.append(ModelResponse(parts=[TextPart("a turn")]))
    store = _profile_store(tmp_path)
    store.write("researcher", "plugins: [knowledge-base]\n")
    store.activate("researcher")

    response = await client.put("/api/harness/selection", json={"name": "researcher"})

    assert response.status_code == 409
    assert "Forget" in response.json()["detail"]


async def test_the_plugins_follow_the_selection_and_the_switch_refuses(client: AsyncClient, tmp_path: Path) -> None:
    store = _profile_store(tmp_path)
    store.write("researcher", "plugins: [knowledge-base]\n")
    store.activate("researcher")
    await client.put("/api/harness/selection", json={"name": "researcher"})

    listing = await client.get("/api/harness/plugins")
    flip = await client.post("/api/harness/plugins/shell/enable")

    rows = {row["name"]: row for row in listing.json()["plugins"]}
    assert rows["knowledge-base"]["enabled"] is True
    assert rows["shell"]["enabled"] is False
    assert flip.status_code == 400
    assert "researcher" in flip.json()["detail"]
