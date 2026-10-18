"""The sources vocabulary migration: the two `_meta/` files and their row keys, the cache folder, the schema keys, the privacy rows."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import yaml

from studio.migrations import wave4

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

OLD_SCHEMA: dict[str, JsonValue] = {
    "slug": "alpha",
    "page_types": [{"page_type": "note", "id_prefix": "note."}],
    "sources": {"enabled": True, "providers": ["intake", "youtube"], "intake": {"kinds": ["paper", "video"]}},
    "retrieval": {"collection": "alpha", "library": False},
}

OLD_QUEUE_ROWS = [
    {"key": "yt-abc", "url": "https://example.org/a", "kind": "video", "status": "resolved", "reason": None},
    {"key": "arxiv-1", "url": "https://example.org/b", "kind": "paper", "status": "requested", "reason": None},
]

OLD_WATCH_ROWS = [{"kind": "xml", "ref": "https://example.org/feed.xml", "max_items": 15, "last_scanned": None}]


def _knowledge_base(root: Path, *, schema: dict[str, JsonValue] | None = None) -> Path:
    """A folder in the old shape: the old queue file and cache, the old watch list, and a schema in the old keys."""
    (root / "_meta" / wave4.OLD_QUEUE_CACHE).mkdir(parents=True)
    (root / "_meta" / wave4.OLD_QUEUE_CACHE / "abc.md").write_text("cached\n", encoding="utf-8")
    (root / "_meta" / wave4.OLD_QUEUE_FILE).write_text(yaml.safe_dump(OLD_QUEUE_ROWS, sort_keys=False), encoding="utf-8")
    (root / "_meta" / wave4.OLD_WATCH_LIST_FILE).write_text(yaml.safe_dump(OLD_WATCH_ROWS, sort_keys=False), encoding="utf-8")
    (root / "knowledge-base-schema.json").write_text(json.dumps(schema if schema is not None else OLD_SCHEMA), encoding="utf-8")
    return root


def _settings(path: Path, roots: dict[str, Path], *, rows: dict[str, dict[str, JsonValue]] | None = None) -> Path:
    """A settings file listing `roots`, each row carrying whatever `rows` says beyond its path."""
    extra = rows if rows is not None else {}
    registry = {name: {"path": str(root), **extra.get(name, {})} for name, root in roots.items()}
    path.write_text(json.dumps({"knowledge_bases": registry}), encoding="utf-8")
    return path


def _obj(value: JsonValue) -> dict[str, JsonValue]:
    """`value` narrowed to a JSON object, for a fixture whose shape this test already knows."""
    assert isinstance(value, dict)
    return value


def _rows(path: Path) -> list[dict[str, JsonValue]]:
    """The YAML list at `path`, whose rows this test already knows are objects."""
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(rows, list)
    return [_obj(row) for row in rows]


class TestSchemaKeys:
    def test_the_connectors_the_queue_policy_and_the_privacy_switch_all_take_their_new_names(self):
        out = wave4.migrated_schema(OLD_SCHEMA)

        assert out is not None
        sources = _obj(out["sources"])
        assert sources["connectors"] == ["intake", "youtube"]
        assert "providers" not in sources
        assert _obj(sources["source_queue"])["source_types"] == ["paper", "video"]
        assert "intake" not in sources
        assert _obj(out["retrieval"])["private"] is True
        assert "library" not in _obj(out["retrieval"])

    def test_the_connector_names_themselves_are_data_and_never_rewritten(self):
        out = wave4.migrated_schema(OLD_SCHEMA)

        assert out is not None
        assert _obj(out["sources"])["connectors"] == ["intake", "youtube"]

    def test_a_schema_that_declared_itself_shared_simply_loses_the_key(self):
        shared: dict[str, JsonValue] = {"slug": "beta", "retrieval": {"collection": "beta", "library": True}}

        out = wave4.migrated_schema(shared)

        assert out is not None
        assert _obj(out["retrieval"]) == {"collection": "beta"}

    def test_a_schema_with_no_privacy_key_gains_nothing(self):
        quiet: dict[str, JsonValue] = {"slug": "beta", "retrieval": {"collection": "beta"}}

        assert wave4.migrated_schema(quiet) is None

    def test_a_schema_already_in_the_new_keys_is_left_alone(self):
        assert wave4.migrated_schema(wave4.migrated_schema(OLD_SCHEMA) or {}) is None


class TestOneKnowledgeBase:
    def test_both_files_the_cache_and_the_schema_all_move(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        report = wave4.Report()

        wave4.migrate_yaml_file(root, wave4.OLD_QUEUE_FILE, wave4.QUEUE_FILE, wave4.QUEUE_ROW_KEY, report=report)
        wave4.migrate_yaml_file(root, wave4.OLD_WATCH_LIST_FILE, wave4.WATCH_LIST_FILE, wave4.WATCH_ROW_KEY, report=report)
        wave4.migrate_queue_cache(root, report=report)
        wave4.migrate_schema_file(root, report=report)

        meta = root / "_meta"
        assert not (meta / wave4.OLD_QUEUE_FILE).exists()
        assert [row["source_type"] for row in _rows(meta / wave4.QUEUE_FILE)] == ["video", "paper"]
        assert [row["scanner"] for row in _rows(meta / wave4.WATCH_LIST_FILE)] == ["xml"]
        assert (meta / wave4.QUEUE_CACHE / "abc.md").is_file()
        assert not (meta / wave4.OLD_QUEUE_CACHE).exists()
        assert _obj(json.loads((root / "knowledge-base-schema.json").read_text(encoding="utf-8"))["sources"])["connectors"] == [
            "intake",
            "youtube",
        ]
        assert report.clean

    def test_every_other_field_of_a_queued_row_survives_the_re_key(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        report = wave4.Report()

        wave4.migrate_yaml_file(root, wave4.OLD_QUEUE_FILE, wave4.QUEUE_FILE, wave4.QUEUE_ROW_KEY, report=report)

        first = _rows(root / "_meta" / wave4.QUEUE_FILE)[0]
        assert first == {"key": "yt-abc", "url": "https://example.org/a", "source_type": "video", "status": "resolved", "reason": None}

    def test_a_file_already_under_its_new_name_still_gets_its_rows_re_keyed(self, tmp_path: Path):
        root = tmp_path / "alpha"
        (root / "_meta").mkdir(parents=True)
        (root / "_meta" / wave4.QUEUE_FILE).write_text(yaml.safe_dump(OLD_QUEUE_ROWS, sort_keys=False), encoding="utf-8")
        report = wave4.Report()

        wave4.migrate_yaml_file(root, wave4.OLD_QUEUE_FILE, wave4.QUEUE_FILE, wave4.QUEUE_ROW_KEY, report=report)

        assert [row["source_type"] for row in _rows(root / "_meta" / wave4.QUEUE_FILE)] == ["video", "paper"]
        assert report.clean

    def test_both_names_present_at_once_is_refused_and_neither_is_touched(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        (root / "_meta" / wave4.QUEUE_FILE).write_text(yaml.safe_dump([], sort_keys=False), encoding="utf-8")
        report = wave4.Report()

        wave4.migrate_yaml_file(root, wave4.OLD_QUEUE_FILE, wave4.QUEUE_FILE, wave4.QUEUE_ROW_KEY, report=report)

        assert not report.clean
        assert _rows(root / "_meta" / wave4.OLD_QUEUE_FILE)[0]["kind"] == "video"
        assert _rows(root / "_meta" / wave4.QUEUE_FILE) == []

    def test_an_unreadable_file_is_reported_and_left_alone(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        (root / "_meta" / wave4.OLD_QUEUE_FILE).write_text("source_type: [unclosed\n", encoding="utf-8")
        report = wave4.Report()

        wave4.migrate_yaml_file(root, wave4.OLD_QUEUE_FILE, wave4.QUEUE_FILE, wave4.QUEUE_ROW_KEY, report=report)

        assert not report.clean
        assert (root / "_meta" / wave4.OLD_QUEUE_FILE).is_file()

    def test_an_unreadable_schema_is_reported_and_left_alone(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha")
        (root / "knowledge-base-schema.json").write_text("{not json", encoding="utf-8")
        report = wave4.Report()

        wave4.migrate_schema_file(root, report=report)

        assert not report.clean
        assert (root / "knowledge-base-schema.json").read_text(encoding="utf-8") == "{not json"


class TestSettingsRows:
    def test_a_row_that_kept_out_of_the_library_becomes_a_private_row(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha}, rows={"alpha": {"library": False}})
        report = wave4.Report()

        wave4.migrate_settings(settings, report=report)

        row = _obj(_obj(json.loads(settings.read_text(encoding="utf-8"))["knowledge_bases"])["alpha"])
        assert row["private"] is True
        assert "library" not in row
        assert report.settings

    def test_a_row_that_declared_itself_in_the_library_simply_loses_the_key(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha}, rows={"alpha": {"library": True}})

        wave4.migrate_settings(settings, report=wave4.Report())

        row = _obj(_obj(json.loads(settings.read_text(encoding="utf-8"))["knowledge_bases"])["alpha"])
        assert "private" not in row
        assert "library" not in row

    def test_a_row_with_no_privacy_key_stays_shared_and_the_file_is_not_rewritten(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha})
        before = settings.read_text(encoding="utf-8")
        report = wave4.Report()

        wave4.migrate_settings(settings, report=report)

        assert settings.read_text(encoding="utf-8") == before
        assert report.settings == []


class TestWholeRun:
    def test_every_registered_folder_and_every_privacy_row_move(self, tmp_path: Path):
        alpha, beta = _knowledge_base(tmp_path / "alpha"), _knowledge_base(tmp_path / "beta")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha, "beta": beta}, rows={"beta": {"library": False}})

        assert wave4.needs_migration(settings)
        report = wave4.migrate(settings)

        assert len(report.schemas) == 2
        assert (alpha / "_meta" / wave4.QUEUE_FILE).is_file()
        assert (beta / "_meta" / wave4.WATCH_LIST_FILE).is_file()
        assert _obj(_obj(json.loads(settings.read_text(encoding="utf-8"))["knowledge_bases"])["beta"])["private"] is True
        assert report.clean

    def test_running_it_twice_finds_nothing_the_second_time(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha}, rows={"alpha": {"library": False}})

        wave4.migrate(settings)
        again = wave4.migrate(settings)

        assert not again.touched
        assert again.clean
        assert not wave4.needs_migration(settings)
        assert again.lines() == ["nothing to migrate: every _meta file, schema file and settings row already reads the new words"]

    def test_a_skipped_name_is_left_untouched(self, tmp_path: Path):
        alpha, beta = _knowledge_base(tmp_path / "alpha"), _knowledge_base(tmp_path / "beta")
        settings = _settings(
            tmp_path / "application_settings.json", {"alpha": alpha, "beta": beta}, rows={"beta": {"library": False}}
        )

        report = wave4.migrate(settings, skip=frozenset({"beta"}))

        assert (beta / "_meta" / wave4.OLD_QUEUE_FILE).is_file()
        assert (alpha / "_meta" / wave4.QUEUE_FILE).is_file()
        assert report.skipped
        assert "beta" in report.skipped[0]
        beta_row = _obj(_obj(json.loads(settings.read_text(encoding="utf-8"))["knowledge_bases"])["beta"])
        assert beta_row["library"] is False
        assert "private" not in beta_row

    def test_a_missing_folder_is_reported_and_the_others_still_move(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha")
        settings = _settings(tmp_path / "application_settings.json", {"alpha": alpha, "ghost": tmp_path / "ghost"})

        report = wave4.migrate(settings)

        assert (alpha / "_meta" / wave4.QUEUE_FILE).is_file()
        assert not report.clean
        assert "ghost" in report.refused[0]

    def test_a_knowledge_base_with_neither_file_is_simply_left_as_it_is(self, tmp_path: Path):
        bare = tmp_path / "bare"
        (bare / "_meta").mkdir(parents=True)
        settings = _settings(tmp_path / "application_settings.json", {"bare": bare})

        assert not wave4.needs_migration(settings)
        report = wave4.migrate(settings)

        assert not report.touched
        assert report.clean
