"""The project config loader: forgiving reads over `.aifactory/config.json`, and the project id."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from harness.features.sessions.project_config import CompactionConfig, MemoryConfig, ProjectConfig, project_config
from harness.features.sessions.service import project_id_of

if TYPE_CHECKING:
    from pathlib import Path


def _write_config(root: Path, payload: dict[str, object]) -> None:
    config_dir = root / ".aifactory"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")


class TestDefaults:
    def test_an_absent_config_file_answers_every_default(self, tmp_path: Path) -> None:
        assert project_config(tmp_path) == ProjectConfig()

    def test_an_unreadable_config_file_answers_every_default(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {})
        (tmp_path / ".aifactory" / "config.json").write_text("not json at all", encoding="utf-8")

        assert project_config(tmp_path) == ProjectConfig()

    def test_global_memory_defaults_on(self) -> None:
        assert ProjectConfig().memory.global_memory is True


class TestRoundTrip:
    def test_every_field_in_the_file_reaches_the_model(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            {
                "name": "notebook",
                "skills": ["team-skills", "extra/skills"],
                "compaction": {
                    "strategy": "provider-native",
                    "target_tokens": 50_000,
                    "summarizer_model": "anthropic:claude-haiku",
                    "recall_max_matches": 7,
                },
                "setup": {"profile": "reviewer", "model": "anthropic:claude-sonnet"},
                "memory": {"global_memory": False},
            },
        )

        config = project_config(tmp_path)

        assert config.name == "notebook"
        assert config.skills == ("team-skills", "extra/skills")
        assert config.compaction == CompactionConfig(
            strategy="provider-native", target_tokens=50_000, summarizer_model="anthropic:claude-haiku", recall_max_matches=7
        )
        assert config.setup.profile == "reviewer"
        assert config.setup.model == "anthropic:claude-sonnet"
        assert config.memory == MemoryConfig(global_memory=False)


class TestClosedSchema:
    def test_an_unknown_top_level_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"bogus": "field"})

    def test_an_unknown_nested_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"compaction": {"bogus": 1}})

    def test_an_unknown_key_inside_memory_raises(self) -> None:
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate({"memory": {"bogus": 1}})


class TestProjectId:
    def test_is_stable_for_the_same_root(self, tmp_path: Path) -> None:
        assert project_id_of(tmp_path) == project_id_of(tmp_path)

    def test_differs_for_a_different_root(self, tmp_path: Path) -> None:
        sibling = tmp_path / "sibling"
        sibling.mkdir()

        assert project_id_of(tmp_path) != project_id_of(sibling)


class TestRelativeSkillsResolveUnderRoot:
    def test_configured_skill_paths_resolve_under_the_project_root(self, tmp_path: Path) -> None:
        _write_config(tmp_path, {"skills": ["my-skills", "nested/more-skills"]})

        config = project_config(tmp_path)
        resolved = tuple(tmp_path / rel for rel in config.skills)

        assert resolved == (tmp_path / "my-skills", tmp_path / "nested" / "more-skills")
