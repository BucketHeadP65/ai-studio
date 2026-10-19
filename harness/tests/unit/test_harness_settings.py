"""The settings root: every section mandatory, the file carries them all, the environment overrides a leaf."""

from __future__ import annotations

import os

from typing import TYPE_CHECKING

import pytest

from core.wiring.contracts import sections_of
from pydantic import ValidationError

from harness import settings as settings_module
from harness.settings import HARNESS_SETTINGS_JSON, HarnessSettings, RunSettings, StateSettings

if TYPE_CHECKING:
    from pathlib import Path

SECTION_NAMES = (
    "run",
    "store",
    "models",
    "self_authoring",
    "terminal",
    "web_search",
    "studio_api",
    "knowledge_base_guidance",
    "library_search",
    "state",
    "inspection",
    "skill_hint",
    "logging",
)


class TestTheFileCarriesEverySection:
    def test_the_committed_file_is_on_disk(self) -> None:
        assert HARNESS_SETTINGS_JSON.is_file()

    def test_it_boots_with_no_environment_at_all(self) -> None:
        built = HarnessSettings()

        assert built.run.run_engine == "in_process"
        assert built.terminal.enabled is False

    @pytest.mark.parametrize("name", SECTION_NAMES)
    def test_every_section_is_present(self, name: str) -> None:
        assert getattr(HarnessSettings(), name) is not None

    def test_a_missing_section_refuses(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.json"
        empty.write_text("{}", encoding="utf-8")

        with pytest.raises(ValidationError):
            HarnessSettings(json_file=empty)


class TestTheEnvironmentOverridesOneLeaf:
    def test_the_nested_name_wins_over_the_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_RUN__RUN_ENGINE", "temporal")

        assert HarnessSettings().run.run_engine == "temporal"

    def test_the_other_leaves_of_that_section_survive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APP_RUN__RUN_ENGINE", "temporal")

        assert HarnessSettings().run.archive_days == 30


class TestAnInitKwargWinsOutright:
    def test_a_whole_section_passed_in_replaces_the_file(self) -> None:
        built = HarnessSettings(run=RunSettings(run_engine="temporal", archive_days=0))

        assert (built.run.run_engine, built.run.archive_days) == ("temporal", 0)


class TestTheSectionsDerive:
    def test_every_section_becomes_one_registration(self) -> None:
        assert len(sections_of(HarnessSettings())) == len(SECTION_NAMES)


class TestTheStateHome:
    def test_every_path_hangs_off_the_one_home(self, tmp_path: Path) -> None:
        paths = StateSettings(home=tmp_path)

        assert paths.session_log == tmp_path / "session-log.jsonl"
        assert paths.evidence_log == tmp_path / "evidence.jsonl"
        assert paths.activation_log == tmp_path / "activations.jsonl"
        assert paths.external_agents == tmp_path / "external-agents.json"
        assert paths.draft == tmp_path / "draft.json"
        assert paths.archive == tmp_path / "runs" / "archive"
        assert paths.run_events("r1") == tmp_path / "runs" / "r1" / "events.jsonl"

    def test_the_records_a_run_and_a_switch_write_hang_off_it_too(self, tmp_path: Path) -> None:
        paths = StateSettings(home=tmp_path)

        assert paths.spend == tmp_path / "runs" / "spend"
        assert paths.sessions == tmp_path / "sessions"
        assert paths.model_tiers == tmp_path / "model-tiers.json"
        assert paths.disabled_plugins == tmp_path / "disabled-plugins.json"
        assert paths.selected_profile == tmp_path / "selected-profile.json"

    def test_the_waiting_run_is_one_path_the_project_rule_decides(self, tmp_path: Path) -> None:
        paths = StateSettings(home=tmp_path)

        assert paths.waiting_run(project=True) == tmp_path / "waiting-run.json"
        assert paths.waiting_run(project=False) == tmp_path / "run-log.json"

    def test_reading_a_path_creates_nothing(self, tmp_path: Path) -> None:
        """Every path is a pure answer: only a writer may bring a directory into being."""
        home = tmp_path / "unborn"
        paths = StateSettings(home=home)

        for path in (
            paths.session_log,
            paths.evidence_log,
            paths.activation_log,
            paths.external_agents,
            paths.draft,
            paths.archive,
            paths.spend,
            paths.sessions,
            paths.model_tiers,
            paths.disabled_plugins,
            paths.selected_profile,
            paths.waiting_run(project=True),
            paths.waiting_run(project=False),
            paths.run_events("r1"),
        ):
            assert not path.exists(), f"{path} was created by reading it"
        assert not home.exists(), "the home itself was created by reading a path under it"


class TestNothingReadsSettingsOffTheModule:
    """Every knob a caller reads is a section it was handed, so no reader is cached on this module."""

    def test_no_cached_reader_survives(self) -> None:
        gone = (
            "harness_settings",
            "run_settings",
            "store_settings",
            "self_authoring_settings",
            "terminal_settings",
            "web_search_settings",
            "studio_api_settings",
            "knowledge_base_guidance_settings",
            "library_search_settings",
            "creds",
            "external_agent_registry",
            "logfire_read_creds",
        )

        assert [name for name in gone if hasattr(settings_module, name)] == []

    def test_the_module_caches_nothing_at_all(self) -> None:
        """A `@cache` here would answer a later request from an environment that has since changed."""
        cached = [
            name
            for name, value in vars(settings_module).items()
            if callable(value) and hasattr(value, "cache_clear") and getattr(value, "__module__", "") == settings_module.__name__
        ]

        assert cached == []

    def test_a_changed_environment_reaches_the_very_next_root(self) -> None:
        """Nothing is read once per process, so an export set now is what the next `HarnessSettings()` says."""
        os.environ["APP_RUN__RUN_ENGINE"] = "temporal"
        try:
            assert HarnessSettings().run.run_engine == "temporal"
        finally:
            del os.environ["APP_RUN__RUN_ENGINE"]

        assert HarnessSettings().run.run_engine == "in_process"
