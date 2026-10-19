"""Unit specs for `engram_connectors.zotero.domain`, the library settings and how they are read out of `SourceResources`."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engram.config import KnowledgeBaseSettings, SourceResources

from engram_connectors.zotero.contracts import LibrarySettings
from engram_connectors.zotero.domain import library_settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue


def _resources(tmp_path: Path, settings: Mapping[str, Mapping[str, JsonValue]] | None = None) -> SourceResources:
    (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return SourceResources(knowledge_base=KnowledgeBaseSettings(path=tmp_path), settings=settings if settings is not None else {})


class TestLibrarySettings:
    def test_data_dir_defaults_to_home_zotero(self):
        settings = LibrarySettings()

        assert settings.data_dir == Path.home() / "Zotero"
        assert settings.base_attachment_dir is None
        assert settings.collection == ""

    def test_sqlite_and_storage_paths_derive_from_data_dir(self, tmp_path: Path):
        settings = LibrarySettings(data_dir=tmp_path)

        assert settings.sqlite_path == tmp_path / "zotero.sqlite"
        assert settings.storage_dir == tmp_path / "storage"

    def test_fields_parse_from_init_kwargs(self, tmp_path: Path):
        settings = LibrarySettings(data_dir=tmp_path, base_attachment_dir=tmp_path / "linked", collection="RAG")

        assert settings.base_attachment_dir == tmp_path / "linked"
        assert settings.collection == "RAG"


class TestLibrarySettingsFromSourceResources:
    def test_an_absent_block_reads_as_the_default_library(self, tmp_path: Path):
        assert library_settings(_resources(tmp_path)) == LibrarySettings()

    def test_a_block_validates_into_the_settings(self, tmp_path: Path):
        data_dir = tmp_path / "library"
        data_dir.mkdir()

        settings = library_settings(_resources(tmp_path, {"zotero": {"data_dir": str(data_dir), "collection": "Reading"}}))

        assert settings.data_dir == data_dir
        assert settings.collection == "Reading"
        assert settings.sqlite_path == data_dir / "zotero.sqlite"
