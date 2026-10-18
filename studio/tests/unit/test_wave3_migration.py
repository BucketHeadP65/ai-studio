"""The knowledge base vocabulary migration: the schema file, the vocabulary folder, the dashboard, the settings key."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from studio.migrations import wave3

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

OLD_SCHEMA: dict[str, JsonValue] = {
    "slug": "alpha",
    "version": "0.1",
    "purpose": "a knowledge base",
    "path_root": "corpus",
    "page_types": [{"kind": "note", "id_prefix": "note."}, {"kind": "paper", "id_prefix": "paper."}],
    "claim_kinds": [{"value": "benchmark-result", "definition": "a measured result"}],
    "claims": {"quote_required_kinds": ["benchmark-result"]},
    "sources": {"enabled": True, "providers": ["intake"], "digest_kinds": {"paper": "paper"}, "intake": {"kinds": ["paper", "video"]}},
    "compounding": {"entries": [{"name": "flag-ideas", "to": ["flag"], "from": {"plane": "pages", "kinds": ["paper"]}}]},
    "retrieval": {"collection": "alpha", "group_by": "kind"},
}


def _knowledge_base(root: Path, *, dashboard: str = "") -> Path:
    """A folder in the old shape: the old schema file, the old vocabulary folder, and a dashboard."""
    root.mkdir(parents=True)
    (root / wave3.OLD_SCHEMA_FILE).write_text(json.dumps(OLD_SCHEMA), encoding="utf-8")
    (root / "_meta" / wave3.OLD_VOCABULARY_DIR).mkdir(parents=True)
    (root / "_meta" / wave3.OLD_VOCABULARY_DIR / "facets.md").write_text("# facets\n", encoding="utf-8")
    if dashboard:
        (root / "_meta" / "dashboard.md").write_text(dashboard, encoding="utf-8")
    return root


def _settings(path: Path, roots: dict[str, Path]) -> Path:
    """A settings file listing `roots` under the old registry key."""
    path.write_text(json.dumps({"corpora": {name: {"path": str(root)} for name, root in roots.items()}}), encoding="utf-8")
    return path


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    """`value` narrowed to a JSON object, for a fixture whose shape this test already knows."""
    assert isinstance(value, dict)
    return value


def _arr(value: JsonValue) -> list[JsonValue]:
    """`value` narrowed to a JSON array, for a fixture whose shape this test already knows."""
    assert isinstance(value, list)
    return value


class TestSchemaKeys:
    def test_every_page_type_key_and_type_vocabulary_takes_its_new_name(self):
        out = wave3.migrated_schema(OLD_SCHEMA)

        assert out is not None
        assert [_obj(entry)["page_type"] for entry in _arr(out["page_types"])] == ["note", "paper"]
        assert "claim_types" in out
        assert "claim_kinds" not in out
        assert _obj(out["claims"])["quote_required_types"] == ["benchmark-result"]
        assert _obj(out["sources"])["digest_types"] == {"paper": "paper"}
        assert _obj(_obj(_arr(_obj(out["compounding"])["entries"])[0])["from"])["page_types"] == ["paper"]
        assert out["path_root"] == "knowledge_base"
        assert _obj(out["retrieval"])["group_by"] == "page_type"

    def test_the_source_types_keep_their_key(self):
        out = wave3.migrated_schema(OLD_SCHEMA)

        assert out is not None
        assert _obj(_obj(out["sources"])["intake"])["kinds"] == ["paper", "video"]

    def test_a_schema_already_in_the_new_keys_is_left_alone(self):
        assert wave3.migrated_schema(wave3.migrated_schema(OLD_SCHEMA) or {}) is None


class TestOneKnowledgeBase:
    def test_the_file_the_folder_and_the_dashboard_all_move(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", dashboard="# Health\n\n<!-- COMPILE-LINT:BEGIN (managed by `memory compile`) -->\nok\n<!-- COMPILE-LINT:END -->\n")
        report = wave3.Report()

        wave3.migrate_schema_file(root, report=report)
        wave3.migrate_vocabulary_folder(root, report=report)
        wave3.migrate_dashboard(root, report=report)

        assert (root / wave3.SCHEMA_FILE).is_file()
        assert not (root / wave3.OLD_SCHEMA_FILE).exists()
        assert (root / "_meta" / wave3.VOCABULARY_DIR / "facets.md").is_file()
        assert "<!-- INDEX-VALIDATION:BEGIN (managed by `studio index`) -->" in (root / "_meta" / "dashboard.md").read_text(encoding="utf-8")
        assert report.clean

    def test_a_folder_already_carrying_the_new_file_keeps_it(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        (root / wave3.SCHEMA_FILE).write_text(json.dumps({"slug": "kept"}), encoding="utf-8")
        report = wave3.Report()

        wave3.migrate_schema_file(root, report=report)

        assert json.loads((root / wave3.SCHEMA_FILE).read_text(encoding="utf-8"))["slug"] == "kept"
        assert report.schemas == []

    def test_an_unreadable_schema_is_reported_and_left_alone(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        (root / wave3.OLD_SCHEMA_FILE).write_text("{not json", encoding="utf-8")
        report = wave3.Report()

        wave3.migrate_schema_file(root, report=report)

        assert not report.clean
        assert (root / wave3.OLD_SCHEMA_FILE).is_file()


class TestWholeRun:
    def test_every_registered_folder_and_the_settings_key_move(self, tmp_path: Path):
        alpha, beta = _knowledge_base(tmp_path / "alpha"), _knowledge_base(tmp_path / "beta")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha, "beta": beta})

        assert wave3.needs_migration(settings)
        report = wave3.migrate(settings)

        assert len(report.schemas) == 2
        assert json.loads(settings.read_text(encoding="utf-8"))["knowledge_bases"].keys() == {"alpha", "beta"}
        assert report.clean

    def test_a_skipped_name_is_left_untouched(self, tmp_path: Path):
        alpha, beta = _knowledge_base(tmp_path / "alpha"), _knowledge_base(tmp_path / "beta")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha, "beta": beta})

        report = wave3.migrate(settings, skip=frozenset({"beta"}))

        assert (beta / wave3.OLD_SCHEMA_FILE).is_file()
        assert report.skipped
        assert "beta" in report.skipped[0]

    def test_a_missing_folder_is_reported_and_the_others_still_move(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha, "ghost": tmp_path / "ghost"})

        report = wave3.migrate(settings)

        assert (alpha / wave3.SCHEMA_FILE).is_file()
        assert not report.clean

    def test_a_second_run_finds_nothing(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha})
        wave3.migrate(settings)

        again = wave3.migrate(settings)

        assert not again.touched
        assert again.lines() == ["nothing to migrate: every schema file, vocabulary folder and settings key already reads the new words"]
        assert not wave3.needs_migration(settings)
