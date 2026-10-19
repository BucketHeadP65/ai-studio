"""The settings-file adapter behind the registry of knowledge bases writes one whole change, or nothing."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from studio.knowledge_base.http.exceptions import KnowledgeBaseRefusal, KnowledgeBaseRefusedError
from studio.knowledge_base.http.infrastructure.registered_knowledge_bases import SettingsFileRegisteredKnowledgeBases
from studio.knowledge_base.settings import KnowledgeBaseEntry, Schedule
from tests.support.http_app import SETTINGS_JSON

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import JsonValue


def _knowledge_base(root: Path) -> Path:
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return root


def _write(settings_path: Path, document: dict[str, JsonValue]) -> None:
    settings_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _written(document: dict[str, JsonValue]) -> str:
    return json.dumps(document, indent=2) + "\n"


class TestWrites:
    def test_each_write_changes_one_registered_knowledge_base_and_keeps_every_other_key(self, tmp_path: Path):
        demo = _knowledge_base(tmp_path / "demo")
        other = _knowledge_base(tmp_path / "other")
        settings_path = tmp_path / "application_settings.json"
        document: dict[str, JsonValue] = {
            **SETTINGS_JSON,
            "knowledge_bases": {"demo": {"path": str(demo), "description": "d"}, "stale": {"path": str(tmp_path / "gone")}},
        }
        _write(settings_path, document)
        registered = SettingsFileRegisteredKnowledgeBases(settings_path)

        registered.register("other", KnowledgeBaseEntry(path=other, description="o", private=True))
        registered.write_schedule("stale", Schedule(every_seconds=600))
        removed = registered.unregister("demo")

        expected: dict[str, JsonValue] = {
            **SETTINGS_JSON,
            "knowledge_bases": {
                "stale": {"path": str(tmp_path / "gone"), "schedule": {"every_seconds": 600}},
                "other": {"path": str(other), "description": "o", "private": True},
            },
        }
        assert settings_path.read_text(encoding="utf-8") == _written(expected)
        assert removed == (demo, "d")
        assert not settings_path.with_name(f"{settings_path.name}.beltcheck.tmp").exists()

    def test_register_over_a_knowledge_bases_value_that_is_not_an_object_is_refused_and_writes_nothing(self, tmp_path: Path):
        demo = _knowledge_base(tmp_path / "demo")
        settings_path = tmp_path / "application_settings.json"
        _write(settings_path, {**SETTINGS_JSON, "knowledge_bases": []})
        before = settings_path.read_bytes()

        with pytest.raises(KnowledgeBaseRefusedError) as refused:
            SettingsFileRegisteredKnowledgeBases(settings_path).register("demo", KnowledgeBaseEntry(path=demo, description=""))

        assert refused.value.refusal is KnowledgeBaseRefusal.REGISTRY_WRITE_REFUSED
        assert settings_path.read_bytes() == before


_WRITES: tuple[Callable[[SettingsFileRegisteredKnowledgeBases, Path], tuple[Path, str] | None], ...] = (
    lambda registered, root: registered.register("other", KnowledgeBaseEntry(path=_knowledge_base(root / "other"), description="")),
    lambda registered, _root: registered.write_schedule("demo", Schedule(every_seconds=600)),
    lambda registered, _root: registered.unregister("demo"),
)


class TestRefusedWrites:
    @pytest.mark.parametrize("write", _WRITES, ids=("register", "write_schedule", "unregister"))
    def test_a_write_that_would_not_load_is_refused_and_writes_nothing(
        self, tmp_path: Path, write: Callable[[SettingsFileRegisteredKnowledgeBases, Path], tuple[Path, str] | None]
    ):
        demo = _knowledge_base(tmp_path / "demo")
        settings_path = tmp_path / "application_settings.json"
        _write(
            settings_path,
            {**SETTINGS_JSON, "knowledge_bases": {"demo": {"path": str(demo), "description": ""}}, "retrieval": {"dialect": "nonsense"}},
        )
        before = settings_path.read_bytes()

        with pytest.raises(KnowledgeBaseRefusedError) as refused:
            write(SettingsFileRegisteredKnowledgeBases(settings_path), tmp_path)

        assert refused.value.refusal is KnowledgeBaseRefusal.REGISTRY_WRITE_REFUSED
        assert settings_path.read_bytes() == before
        assert not settings_path.with_name(f"{settings_path.name}.beltcheck.tmp").exists()
