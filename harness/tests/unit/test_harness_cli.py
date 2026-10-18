"""`harness` -- the owner's CLI to list/activate/disable the agent's own custom items.

Exercised through `CliRunner` against the real root app (`harness.cli`), so `harness list` and
friends are real, wired commands, not just command bodies tested in isolation.
"""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import StepRegistry
from typer.testing import CliRunner

import harness.settings

from harness.cli import app
from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.promotion import promoted_capability_types
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from tests.support.selfauthoring import FakeActivationLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

runner = CliRunner()

_VALID_SOURCE = """
from pydantic_ai.capabilities import AbstractCapability


class Marker(AbstractCapability):
    '''Valid, does nothing.'''
"""

_VALID_STEP_SOURCE = """
async def double(x: int) -> int:
    '''Doubles x.'''
    return x * 2
"""

_VALID_AGENT_DOCUMENT = "instructions: Read the ledger and say whether the quarter looks healthy.\n"

_PROMOTABLE_SOURCE = """
from dataclasses import dataclass

from pydantic_ai.capabilities import AbstractCapability


@dataclass
class Marker(AbstractCapability):
    '''Meets the promotion contract: a declared serialization name (the class name, by default)
    and a dataclass declared on the class itself.'''
"""


@pytest.fixture(autouse=True)
def _off_to_one_side(tmp_path: Path) -> Iterator[None]:
    """Point every `harness` invocation in this file at a `tmp_path`-scoped custom root and state home.

    Both are exports, the way a deployment sets them: every console verb builds its own
    `HarnessSettings()` per invocation, so the environment is where a test moves the custom root
    and the home the activation log a verb attests to lands under.
    """
    os.environ["APP_SELF_AUTHORING__CUSTOM_ROOT"] = str(tmp_path / "custom")
    os.environ["APP_STATE__HOME"] = str(tmp_path / ".state")
    try:
        yield
    finally:
        del os.environ["APP_SELF_AUTHORING__CUSTOM_ROOT"]
        del os.environ["APP_STATE__HOME"]


def _store(tmp_path: Path) -> FileCustomCapabilities:
    return FileCustomCapabilities(tmp_path / "custom" / "capabilities", activations=FakeActivationLog())


def _step_store(tmp_path: Path) -> FileCustomSteps:
    return FileCustomSteps(tmp_path / "custom" / "steps", activations=FakeActivationLog())


def _agent_store(tmp_path: Path) -> FileCustomAgents:
    return FileCustomAgents(tmp_path / "custom" / "agents", activations=FakeActivationLog())


def test_list_reports_none_custom_yet() -> None:
    result = runner.invoke(app, ["list", "capability"])

    assert result.exit_code == 0
    assert "No custom capability yet." in result.stdout


def test_list_shows_a_pending_entry(tmp_path: Path) -> None:
    _store(tmp_path).write("marker", _VALID_SOURCE)

    result = runner.invoke(app, ["list", "capability"])

    assert result.exit_code == 0
    assert "marker  [disabled]  Marker" in result.stdout


def test_activate_flips_it_live(tmp_path: Path) -> None:
    _store(tmp_path).write("marker", _VALID_SOURCE)

    result = runner.invoke(app, ["activate", "capability", "marker"])

    assert result.exit_code == 0
    assert "Activated capability 'marker'." in result.stdout
    [instance] = _store(tmp_path).load_active()
    assert type(instance).__name__ == "Marker"


def test_activating_an_unknown_name_fails_cleanly() -> None:
    result = runner.invoke(app, ["activate", "capability", "nobody"])

    assert result.exit_code == 1
    assert "No custom capability named 'nobody'." in result.stdout


def test_activating_a_capability_with_a_stored_error_fails_cleanly(tmp_path: Path) -> None:
    record = _store(tmp_path).write("broken", "this is not python at all !!!")
    assert record.last_error is not None

    result = runner.invoke(app, ["activate", "capability", "broken"])

    assert result.exit_code == 1
    assert record.last_error in result.stdout


def test_disable_stops_it_loading(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("marker", _VALID_SOURCE)
    store.activate("marker")

    result = runner.invoke(app, ["disable", "capability", "marker"])

    assert result.exit_code == 0
    assert "Disabled capability 'marker'." in result.stdout
    assert _store(tmp_path).load_active() == []


def test_an_unwired_kind_is_refused_by_name() -> None:
    """No fourth kind remains unwired after `agent` -- workflows live in their own store, never this one."""
    result = runner.invoke(app, ["list", "workflow"])

    assert result.exit_code == 1
    assert "unknown custom-item kind 'workflow'" in result.stdout


def test_list_reports_none_custom_yet_for_step() -> None:
    result = runner.invoke(app, ["list", "step"])

    assert result.exit_code == 0
    assert "No custom step yet." in result.stdout


def test_list_shows_a_pending_step_entry(tmp_path: Path) -> None:
    _step_store(tmp_path).write("double", _VALID_STEP_SOURCE)

    result = runner.invoke(app, ["list", "step"])

    assert result.exit_code == 0
    assert "double  [disabled]  double" in result.stdout


def test_activate_flips_a_step_live(tmp_path: Path) -> None:
    _step_store(tmp_path).write("double", _VALID_STEP_SOURCE)

    result = runner.invoke(app, ["activate", "step", "double"])

    assert result.exit_code == 0
    assert "Activated step 'double'." in result.stdout
    registry = StepRegistry()
    assert _step_store(tmp_path).load_active(registry) == ("double",)


def test_activating_an_unknown_step_name_fails_cleanly() -> None:
    result = runner.invoke(app, ["activate", "step", "nobody"])

    assert result.exit_code == 1
    assert "No custom step named 'nobody'." in result.stdout


def test_disable_stops_a_step_loading(tmp_path: Path) -> None:
    store = _step_store(tmp_path)
    store.write("double", _VALID_STEP_SOURCE)
    store.activate("double")

    result = runner.invoke(app, ["disable", "step", "double"])

    assert result.exit_code == 0
    assert "Disabled step 'double'." in result.stdout
    assert _step_store(tmp_path).load_active(StepRegistry()) == ()


def test_list_reports_none_custom_yet_for_agent() -> None:
    result = runner.invoke(app, ["list", "agent"])

    assert result.exit_code == 0
    assert "No custom agent yet." in result.stdout


def test_list_shows_a_pending_agent_entry(tmp_path: Path) -> None:
    _agent_store(tmp_path).write("weigher", _VALID_AGENT_DOCUMENT)

    result = runner.invoke(app, ["list", "agent"])

    assert result.exit_code == 0
    assert "weigher  [disabled]  (host default model)" in result.stdout


def test_activate_flips_an_agent_resolvable(tmp_path: Path) -> None:
    _agent_store(tmp_path).write("weigher", _VALID_AGENT_DOCUMENT)

    result = runner.invoke(app, ["activate", "agent", "weigher"])

    assert result.exit_code == 0
    assert "Activated agent 'weigher'." in result.stdout
    document = _agent_store(tmp_path).resolve_active("weigher")
    assert document["instructions"] == "Read the ledger and say whether the quarter looks healthy."


def test_activating_an_unknown_agent_name_fails_cleanly() -> None:
    result = runner.invoke(app, ["activate", "agent", "nobody"])

    assert result.exit_code == 1
    assert "No custom agent named 'nobody'." in result.stdout


def test_disable_stops_an_agent_resolving(tmp_path: Path) -> None:
    store = _agent_store(tmp_path)
    store.write("weigher", _VALID_AGENT_DOCUMENT)
    store.activate("weigher")

    result = runner.invoke(app, ["disable", "agent", "weigher"])

    assert result.exit_code == 0
    assert "Disabled agent 'weigher'." in result.stdout
    with pytest.raises(LookupError):
        _agent_store(tmp_path).resolve_active("weigher")


def test_promote_requires_active_first(tmp_path: Path) -> None:
    _store(tmp_path).write("marker", _PROMOTABLE_SOURCE)

    result = runner.invoke(app, ["promote", "capability", "marker"])

    assert result.exit_code == 1
    assert "harness activate capability marker" in result.stdout


def test_promote_flips_it_into_the_registry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("marker", _PROMOTABLE_SOURCE)
    store.activate("marker")

    result = runner.invoke(app, ["promote", "capability", "marker"])

    assert result.exit_code == 0
    assert "Promoted capability 'marker' (Marker)." in result.stdout
    [promoted] = promoted_capability_types(store)
    assert promoted.__name__ == "Marker"


def test_promoting_an_unknown_capability_fails_cleanly() -> None:
    result = runner.invoke(app, ["promote", "capability", "nobody"])

    assert result.exit_code == 1
    assert "No custom capability named 'nobody'." in result.stdout


def test_promoting_a_non_capability_kind_is_refused(tmp_path: Path) -> None:
    _agent_store(tmp_path).write("weigher", _VALID_AGENT_DOCUMENT)

    result = runner.invoke(app, ["promote", "agent", "weigher"])

    assert result.exit_code == 1
    assert "promotion applies only to capabilities" in result.stdout


def test_demote_removes_it_from_the_registry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write("marker", _PROMOTABLE_SOURCE)
    store.activate("marker")
    runner.invoke(app, ["promote", "capability", "marker"])

    result = runner.invoke(app, ["demote", "capability", "marker"])

    assert result.exit_code == 0
    assert "Demoted capability 'marker'." in result.stdout
    assert promoted_capability_types(store) == ()


def test_demoting_an_unpromoted_capability_fails_cleanly() -> None:
    result = runner.invoke(app, ["demote", "capability", "nobody"])

    assert result.exit_code == 1
    assert "Capability 'nobody' is not currently promoted." in result.stdout


def test_demoting_a_non_capability_kind_is_refused(tmp_path: Path) -> None:
    _step_store(tmp_path).write("double", _VALID_STEP_SOURCE)

    result = runner.invoke(app, ["demote", "step", "double"])

    assert result.exit_code == 1
    assert "promotion applies only to capabilities" in result.stdout
# --- delete ---------------------------------------------------------------------------------


def test_delete_removes_a_disabled_artifact(tmp_path: Path) -> None:
    store = FileCustomSteps(tmp_path / "custom" / "steps", activations=FakeActivationLog())
    store.write("double", _VALID_STEP_SOURCE)

    result = runner.invoke(app, ["delete", "step", "double"])

    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output
    assert store.list_all() == []


def test_delete_refuses_an_active_artifact(tmp_path: Path) -> None:
    store = FileCustomSteps(tmp_path / "custom" / "steps", activations=FakeActivationLog())
    store.write("double", _VALID_STEP_SOURCE)
    store.activate("double")

    result = runner.invoke(app, ["delete", "step", "double"])

    assert result.exit_code == 1
    assert "disable" in result.output


def test_deleting_an_unknown_name_fails_cleanly() -> None:
    result = runner.invoke(app, ["delete", "agent", "ghost"])

    assert result.exit_code == 1
    assert "ghost" in result.output


# --- plugins ----------------------------------------------------------------------------------


def test_plugins_lists_every_plugin_with_its_state() -> None:
    result = runner.invoke(app, ["plugins"])

    assert result.exit_code == 0, result.output
    assert "knowledge-base" in result.output
    assert "sql" in result.output
    assert "[off]" in result.output
    assert "always on" in result.output


def test_a_plugin_turned_off_then_on_round_trips() -> None:
    closed = runner.invoke(app, ["plugins", "--off", "shell"])
    listing = runner.invoke(app, ["plugins"])
    opened = runner.invoke(app, ["plugins", "--on", "shell"])

    assert closed.exit_code == 0, closed.output
    assert "shell" in closed.output
    assert "shell  [off]" in listing.output
    assert opened.exit_code == 0, opened.output


def test_plugins_refuses_an_always_on_name() -> None:
    result = runner.invoke(app, ["plugins", "--off", "spend-limits"])

    assert result.exit_code == 1
    assert "always on" in result.output
# --- profile selection ----------------------------------------------------------------------


def test_profile_selection_round_trips(tmp_path: Path) -> None:
    from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles

    store = FileCustomProfiles(tmp_path / "custom" / "profiles", activations=FakeActivationLog())
    store.write("researcher", "plugins: [knowledge-base]\n")
    store.activate("researcher")

    chosen = runner.invoke(app, ["profile", "researcher"])
    cleared = runner.invoke(app, ["profile", "--clear"])

    assert chosen.exit_code == 0, chosen.output
    assert "researcher" in chosen.output
    assert cleared.exit_code == 0, cleared.output


def test_selecting_a_pending_profile_fails_cleanly(tmp_path: Path) -> None:
    from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles

    FileCustomProfiles(tmp_path / "custom" / "profiles", activations=FakeActivationLog()).write("researcher", "plugins: [knowledge-base]\n")

    result = runner.invoke(app, ["profile", "researcher"])

    assert result.exit_code == 1
    assert "active" in result.output


CONSOLE_VERBS_A_RUN_MUST_NOT_REACH = [
    ["activate", "capability", "marker"],
    ["disable", "capability", "marker"],
    ["delete", "capability", "marker"],
    ["promote", "capability", "marker"],
    ["demote", "capability", "marker"],
    ["plugins", "--on", "sql"],
    ["plugins", "--off", "sql"],
    ["profile", "reviewer"],
    ["profile", "--clear"],
]


@pytest.mark.parametrize("argv", CONSOLE_VERBS_A_RUN_MUST_NOT_REACH)
def test_a_console_verb_refuses_a_caller_with_nobody_to_ask(argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every verb that decides what a later session holds is the owner's, whatever path reaches it.

    A worker and a scheduled step both hold a shell with this CLI on its path, so the refusal has to
    live in the CLI rather than in a list of denied command names: `uv run harness`, `python -m
    harness.cli` and a wrapper script all spell the command differently and all arrive here.
    """
    _store(tmp_path).write("marker", _VALID_SOURCE)
    monkeypatch.setenv(harness.settings.CONSOLE_CLOSED_ENV, "1")

    result = runner.invoke(app, argv)

    assert result.exit_code == 1, f"{argv} ran for a caller with nobody to ask"
    assert "the owner's to make" in result.stdout
    assert "Ask the person" in result.stdout


@pytest.mark.parametrize("argv", [["list", "capability"], ["plugins"], ["profile"]])
def test_a_read_stays_open_to_a_caller_with_nobody_to_ask(argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reading the state changes nothing, and a run blinded to it gains nobody any safety."""
    _store(tmp_path).write("marker", _VALID_SOURCE)
    monkeypatch.setenv(harness.settings.CONSOLE_CLOSED_ENV, "1")

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, f"{argv} was refused, but it only reads"


def test_the_owner_at_a_terminal_reaches_every_console_verb(tmp_path: Path) -> None:
    """The refusal is about the caller having nobody to ask, never about the verb itself."""
    _store(tmp_path).write("marker", _VALID_SOURCE)

    result = runner.invoke(app, ["activate", "capability", "marker"])

    assert result.exit_code == 0
