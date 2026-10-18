"""Profiles: a named set of plugins, custom items, and standing instructions.

The fourth custom-item kind. A profile lands pending like the others and only the owner activates
it; SELECTION is the further owner act that puts one active profile in effect -- its plugins
replace the hand-set file, its custom-item lists are reconciled to active, its instructions join
the agent's voice. Selection validates the whole bundle first and refuses broken references
outright, never at first use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from harness import plugins
from harness.features.selfauthoring.exceptions import SelectionError
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from harness.features.selfauthoring.service import select_profile, selected_profile_name, selected_profile_voice
from tests.support.selfauthoring import FakeActivationLog
from tests.support.state import a_state

if TYPE_CHECKING:
    from pathlib import Path

_VALID_CAPABILITY = """
from pydantic_ai.capabilities import AbstractCapability


class Marker(AbstractCapability):
    '''Valid, does nothing.'''
"""

_VALID_STEP = """
async def double(x: int) -> int:
    '''Doubles x.'''
    return x * 2
"""

KNOWLEDGE_BASE_ONLY = """\
description: Retrieval work only.
instructions: Stay inside the knowledge base; retrieval quality over speed.
plugins: [knowledge-base, leveled-reads]
"""


def _store(tmp_path: Path) -> tuple[FileCustomProfiles, FakeActivationLog]:
    activations = FakeActivationLog()
    return FileCustomProfiles(tmp_path / "custom" / "profiles", activations=activations), activations


class TestProfileStore:
    def test_a_cleanly_validated_write_lands_disabled(self, tmp_path: Path):
        store, _activations = _store(tmp_path)

        record = store.write("researcher", KNOWLEDGE_BASE_ONLY)

        assert record.status == "disabled"
        assert record.last_error is None
        assert record.plugins == ["knowledge-base", "leveled-reads"]

    def test_an_unknown_door_is_refused_at_write_naming_it(self, tmp_path: Path):
        store, _activations = _store(tmp_path)

        record = store.write("teleporter", "plugins: [teleport]\n")

        assert record.last_error is not None
        assert "teleport" in record.last_error

    def test_an_always_on_plugin_is_refused_at_write(self, tmp_path: Path):
        store, _activations = _store(tmp_path)

        record = store.write("bad", "plugins: [spend-limits]\n")

        assert record.last_error is not None
        assert "always on" in record.last_error

    def test_delete_removes_a_disabled_profile_and_attests(self, tmp_path: Path):
        store, activations = _store(tmp_path)
        store.write("researcher", KNOWLEDGE_BASE_ONLY)

        assert store.delete("researcher")
        assert store.list_all() == []
        assert (activations.events[-1].name, activations.events[-1].action) == ("researcher", "delete")


class TestSelection:
    def test_selecting_an_active_profile_persists_and_its_plugins_take_effect(self, tmp_path: Path):
        store, _activations = _store(tmp_path)
        store.write("researcher", KNOWLEDGE_BASE_ONLY)
        store.activate("researcher")

        select_profile("researcher", root=tmp_path / "custom", state=a_state(tmp_path))

        assert selected_profile_name(a_state(tmp_path)) == "researcher"
        assert plugins.plugin_enabled(a_state(tmp_path), "knowledge-base")
        assert not plugins.plugin_enabled(a_state(tmp_path), "shell")
        assert not plugins.plugin_enabled(a_state(tmp_path), "sql")

    def test_clearing_the_selection_hands_the_plugins_back_to_the_file(self, tmp_path: Path):
        store, _activations = _store(tmp_path)
        store.write("researcher", KNOWLEDGE_BASE_ONLY)
        store.activate("researcher")
        select_profile("researcher", root=tmp_path / "custom", state=a_state(tmp_path))

        select_profile(None, root=tmp_path / "custom", state=a_state(tmp_path))

        assert selected_profile_name(a_state(tmp_path)) is None
        assert plugins.plugin_enabled(a_state(tmp_path), "shell")

    def test_a_pending_profile_cannot_be_selected(self, tmp_path: Path):
        store, _activations = _store(tmp_path)
        store.write("researcher", KNOWLEDGE_BASE_ONLY)

        with pytest.raises(SelectionError, match="active"):
            select_profile("researcher", root=tmp_path / "custom", state=a_state(tmp_path))

    def test_an_unknown_profile_cannot_be_selected(self, tmp_path: Path):
        with pytest.raises(SelectionError, match="ghost"):
            select_profile("ghost", root=tmp_path / "custom", state=a_state(tmp_path))

    def test_a_profile_naming_a_missing_capability_refuses_selection_whole(self, tmp_path: Path):
        store, _activations = _store(tmp_path)
        store.write("wanting", "plugins: [knowledge-base]\ncapabilities: [nonexistent_cap]\n")
        store.activate("wanting")

        with pytest.raises(SelectionError, match="nonexistent_cap"):
            select_profile("wanting", root=tmp_path / "custom", state=a_state(tmp_path))
        assert selected_profile_name(a_state(tmp_path)) is None

    def test_selection_reconciles_custom_actives_to_the_profile(self, tmp_path: Path):
        root = tmp_path / "custom"
        capabilities = FileCustomCapabilities(root / "capabilities", activations=FakeActivationLog())
        capabilities.write("wanted", _VALID_CAPABILITY)
        capabilities.write("unwanted", _VALID_CAPABILITY)
        capabilities.activate("unwanted")
        steps = FileCustomSteps(root / "steps", activations=FakeActivationLog())
        steps.write("double", _VALID_STEP)
        store, _activations = _store(tmp_path)
        store.write("worker", "plugins: [knowledge-base]\ncapabilities: [wanted]\nscripts: [double]\n")
        store.activate("worker")

        select_profile("worker", root=root, state=a_state(tmp_path))

        by_name = {record.name: record.status for record in capabilities.list_all()}
        assert by_name == {"wanted": "active", "unwanted": "disabled"}
        assert [record.status for record in steps.list_all()] == ["active"]

    def test_the_selected_profiles_instructions_join_the_voice(self, tmp_path: Path):
        store, _activations = _store(tmp_path)
        store.write("researcher", KNOWLEDGE_BASE_ONLY)
        store.activate("researcher")
        select_profile("researcher", root=tmp_path / "custom", state=a_state(tmp_path))

        assert selected_profile_voice(a_state(tmp_path)) == "Stay inside the knowledge base; retrieval quality over speed."

    def test_no_selection_means_no_extra_voice(self, tmp_path: Path) -> None:
        assert selected_profile_voice(a_state(tmp_path)) is None
