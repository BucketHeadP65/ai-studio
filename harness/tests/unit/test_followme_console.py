"""The console ports answer through the very route handlers the dashboard buttons call, in process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import StepRegistry

from harness import plugins
from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.composition import RouteConsole
from harness.domain.events import PluginSet, RunLaunched
from harness.features.selfauthoring.controller import SelfAuthoringDeployment
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings, SelfAuthoringSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.state import a_session_log, a_state, a_throwaway_state, records_under
from tests.support.tiers import never_built

if TYPE_CHECKING:
    from pathlib import Path

    from harness.session import ProjectIndex


class _IndexPointingAtAGoneRoot:
    """A projection that remembers one session against a project directory that is not there any more."""

    def __init__(self, session_id: str, root: Path) -> None:
        self.session_id = session_id
        self.root = root

    def id_of(self, root: Path) -> str:
        return f"project-{root.name}"

    async def root_of(self, project_id: str) -> Path | None:
        return self.root if project_id == f"project-{self.root.name}" else None

    async def root_of_session(self, session_id: str) -> Path | None:
        return self.root if session_id == self.session_id else None



def _console_settings(home: Path) -> SelfAuthoringDeployment:
    """What a console act forwards to the route it stands in for, all of it under `home`."""
    state = a_state(home)
    return SelfAuthoringDeployment(
        state=state,
        self_authoring=SelfAuthoringSettings(custom_root=home / "custom"),
        run=RunSettings(),
        tiers=ModelTiers(state=state, models=ModelList(listing=None), build=never_built),
    )


def _pool(projects: ProjectIndex | None = None) -> SessionPool:
    """One pool over a session with nothing behind it, for a console act that only reads it."""
    default = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_id="s-default", session_log=NoSessionLog(), state=a_throwaway_state())
    return SessionPool(default, projects=projects)


async def test_toggling_a_plugin_flips_it_and_records_it(tmp_path: Path) -> None:
    pool = _pool()
    said = await RouteConsole(
        settings=_console_settings(tmp_path),
        records=records_under(tmp_path),
        session_log=a_session_log(tmp_path),
        sessions=APool(pool),
        topics=FakeBroadcaster(),
    ).toggle_plugin("sql", on=True)
    assert said == "Plugin sql is on."
    assert plugins.plugin_enabled(a_state(tmp_path), "sql")
    assert any(isinstance(r.event, PluginSet) and r.event.plugin == "sql" and r.event.enabled for r in a_session_log(tmp_path).events())


async def test_toggle_plugin_refuses_always_on_and_unknown_in_words(tmp_path: Path) -> None:
    pool = _pool()
    console = RouteConsole(
        settings=_console_settings(tmp_path),
        records=records_under(tmp_path),
        session_log=a_session_log(tmp_path),
        sessions=APool(pool),
        topics=FakeBroadcaster(),
    )
    assert "always on" in await console.toggle_plugin("spend-limits", on=False)
    assert "No plugin named" in await console.toggle_plugin("teleport", on=False)


async def test_set_model_refuses_an_unknown_model_in_words(tmp_path: Path) -> None:
    pool = _pool()
    said = await RouteConsole(
        settings=_console_settings(tmp_path),
        records=records_under(tmp_path),
        session_log=a_session_log(tmp_path),
        sessions=APool(pool),
        topics=FakeBroadcaster(),
    ).set_model("model-nobody-sells")
    assert said.startswith("Refused:")


async def test_run_control_answers_the_route_sentence_for_a_session_with_nothing_running(tmp_path: Path) -> None:
    pool = _pool()
    console = RouteConsole(
        settings=_console_settings(tmp_path),
        records=records_under(tmp_path),
        session_log=a_session_log(tmp_path),
        sessions=APool(pool),
        topics=FakeBroadcaster(),
    )
    assert await console.pause("s-default") == "Refused: nothing is running."
    assert await console.stop("s-default") == "Refused: nothing is running."
    assert await console.set_automode("s-default", on=True) == "Refused: nothing is running."
    assert await console.decide("s-default", approve=True, feedback="") == "Refused: nothing is waiting for a decision."
    assert await console.pause("00000000-0000-0000-0000-000000000000") == "Refused: no session with that id here."
    assert not any(isinstance(r.event, RunLaunched) for r in a_session_log(tmp_path).events())


async def test_an_unknown_id_and_a_gone_project_root_are_two_different_refusals(tmp_path: Path) -> None:
    gone = tmp_path / "a-project-that-moved"
    pool = _pool(projects=_IndexPointingAtAGoneRoot("s-gone", gone))
    console = RouteConsole(
        settings=_console_settings(tmp_path),
        records=records_under(tmp_path),
        session_log=a_session_log(tmp_path),
        sessions=APool(pool),
        topics=FakeBroadcaster(),
    )
    unknown = await console.pause("00000000-0000-0000-0000-000000000000")
    vanished = await console.stop("s-gone")
    assert unknown == "Refused: no session with that id here."
    assert vanished != unknown
    assert str(gone) in vanished
