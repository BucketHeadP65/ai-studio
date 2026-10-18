"""Unit tests for `KnowledgeBaseEntry` — the vocabulary-member shape `ApplicationSettings.knowledge_bases` holds.

Entry paths carry the same shape check `KnowledgeBaseSettings.path` enforces (a `knowledge-base-schema.json`, or the
legacy `Units/` + `_meta/` pair), so a mini knowledge base is built in `tmp_path` exactly like the settings
tests do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from engram.config import IngestionSettings, KnowledgeBaseEntry, resolve_active_knowledge_base_path
from engram.indexing.contracts import DEFAULT_CHUNK_SIZES
from engram.indexing.domain.mirror_chunks import MIRROR_CHUNK_TOKEN_BUDGET

if TYPE_CHECKING:
    from pathlib import Path


def _make_knowledge_base(root: Path) -> Path:
    """A minimal directory that passes the knowledge base shape validator."""
    (root / "Units").mkdir(parents=True)
    (root / "_meta").mkdir()
    return root


class TestKnowledgeBaseEntry:
    def test_accepts_a_directory_with_units_and_meta(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path)

        entry = KnowledgeBaseEntry(path=knowledge_base)

        assert entry.path == knowledge_base
        assert entry.description == ""

    def test_accepts_a_directory_with_only_a_knowledge_base_schema_json(self, tmp_path: Path):
        (tmp_path / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")

        entry = KnowledgeBaseEntry(path=tmp_path)

        assert entry.path == tmp_path

    def test_rejects_a_missing_path(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            KnowledgeBaseEntry(path=tmp_path / "does-not-exist")

    def test_rejects_a_directory_without_either_shape(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            KnowledgeBaseEntry(path=tmp_path)

    def test_description_defaults_to_empty(self, tmp_path: Path):
        entry = KnowledgeBaseEntry(path=_make_knowledge_base(tmp_path))

        assert entry.description == ""

    def test_description_is_honoured(self, tmp_path: Path):
        entry = KnowledgeBaseEntry(path=_make_knowledge_base(tmp_path), description="the research knowledge_base")

        assert entry.description == "the research knowledge_base"

    def test_is_frozen(self, tmp_path: Path):
        entry = KnowledgeBaseEntry(path=_make_knowledge_base(tmp_path))

        with pytest.raises(ValidationError):
            entry.description = "changed"


class TestResolveActiveKnowledgeBasePath:
    """The raw-mapping resolver: the one rule both apps read the settings file's active knowledge base by."""

    def test_old_shape_reads_knowledge_base_path_directly(self):
        settings = {"knowledge_base": {"path": "/kb/primary"}}

        assert resolve_active_knowledge_base_path(settings) == "/kb/primary"

    def test_an_empty_registry_behaves_like_the_old_shape(self):
        settings = {"knowledge_base": {"path": "/kb/primary"}, "knowledge_bases": {}}

        assert resolve_active_knowledge_base_path(settings) == "/kb/primary"

    def test_a_registry_prefers_the_entry_matching_knowledge_base_path(self):
        settings = {"knowledge_base": {"path": "/kb/primary"}, "knowledge_bases": {"primary": {"path": "/kb/primary"}, "other": {"path": "/kb/other"}}}

        assert resolve_active_knowledge_base_path(settings) == "/kb/primary"

    def test_a_registry_falls_back_to_the_sole_entry_when_knowledge_base_path_is_stale(self):
        settings = {"knowledge_base": {"path": "/kb/stale"}, "knowledge_bases": {"primary": {"path": "/kb/primary"}}}

        assert resolve_active_knowledge_base_path(settings) == "/kb/primary"

    def test_a_multi_entry_registry_with_no_match_raises(self):
        settings = {"knowledge_base": {"path": "/kb/stale"}, "knowledge_bases": {"primary": {"path": "/kb/primary"}, "other": {"path": "/kb/other"}}}

        with pytest.raises(ValueError, match=r"knowledge_base\.path"):
            resolve_active_knowledge_base_path(settings)

    def test_a_named_multi_entry_registry_with_no_knowledge_base_key_resolves_by_name(self):
        settings = {"knowledge_bases": {"primary": {"path": "/kb/primary"}, "research": {"path": "/kb/research"}}}

        assert resolve_active_knowledge_base_path(settings, name="research") == "/kb/research"

    def test_an_unknown_name_falls_through_to_the_existing_behavior(self):
        settings = {"knowledge_base": {"path": "/kb/primary"}, "knowledge_bases": {"primary": {"path": "/kb/primary"}}}

        assert resolve_active_knowledge_base_path(settings, name="research") == "/kb/primary"


class TestIngestionChunkSizes:
    def test_the_chunk_size_settings_default_to_the_indexers_own_sizes(self) -> None:
        settings = IngestionSettings()

        assert (settings.chunk_target_tokens, settings.chunk_max_tokens) == (DEFAULT_CHUNK_SIZES.target_tokens, DEFAULT_CHUNK_SIZES.max_tokens)

    def test_a_chunk_target_over_the_maximum_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="chunk_target_tokens"):
            IngestionSettings(chunk_target_tokens=900, chunk_max_tokens=800)

    @pytest.mark.parametrize("field", ["chunk_target_tokens", "chunk_max_tokens"])
    def test_a_chunk_size_that_is_not_positive_is_refused(self, field: str) -> None:
        with pytest.raises(ValidationError, match=field):
            IngestionSettings.model_validate({field: 0})

    def test_the_chunk_maximum_is_bounded_by_the_indexers_own_ceiling(self) -> None:
        assert IngestionSettings(chunk_max_tokens=MIRROR_CHUNK_TOKEN_BUDGET).chunk_max_tokens == MIRROR_CHUNK_TOKEN_BUDGET
        with pytest.raises(ValidationError, match="chunk_max_tokens"):
            IngestionSettings(chunk_max_tokens=MIRROR_CHUNK_TOKEN_BUDGET + 1)
