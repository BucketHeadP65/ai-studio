"""Unit tests for `KnowledgeBaseEntry` — the vocabulary-member shape `ApplicationSettings.knowledge_bases` holds.

Entry paths carry the same shape check `KnowledgeBaseSettings.path` enforces (a `knowledge-base-schema.json` at the
root), so a mini knowledge base is built in `tmp_path` exactly like the settings tests do.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest

from pydantic import ValidationError

from engram.config import IngestionSettings, KnowledgeBaseEntry
from engram.indexing.contracts import DEFAULT_CHUNK_SIZES, DEFAULT_PLANNER_CEILING_TOKENS
from engram.indexing.domain.mirror_chunks import MIRROR_CHUNK_TOKEN_BUDGET
from engram.indexing.service.cut_plan_stage import DEFAULT_PLANNER_CONCURRENCY, DEFAULT_PLANNER_DEADLINE_SECONDS


def _make_knowledge_base(root: Path) -> Path:
    """A minimal directory that passes the knowledge base shape validator."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return root


class TestKnowledgeBaseEntry:
    def test_accepts_a_directory_with_a_knowledge_base_schema_json(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path)

        entry = KnowledgeBaseEntry(path=knowledge_base)

        assert entry.path == knowledge_base
        assert entry.description == ""

    def test_rejects_a_directory_with_units_and_meta_but_no_schema(self, tmp_path: Path):
        (tmp_path / "Units").mkdir()
        (tmp_path / "_meta").mkdir()

        with pytest.raises(ValidationError, match=r"knowledge-base-schema\.json"):
            KnowledgeBaseEntry(path=tmp_path)

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


class TestIngestionChunkPlanner:
    def test_the_chunk_planner_is_off_until_the_settings_name_a_deployment(self) -> None:
        assert IngestionSettings().chunk_planner_model == ""

    def test_the_example_settings_leave_the_chunk_planner_off(self) -> None:
        example = json.loads((Path(__file__).parents[3] / "config" / "application_settings.example.json").read_text(encoding="utf-8"))

        assert example["ingestion"]["chunk_planner_model"] == ""

    def test_the_chunk_planner_limits_default_to_the_indexers_own(self) -> None:
        settings = IngestionSettings()

        assert settings.chunk_planner_ceiling_tokens == DEFAULT_PLANNER_CEILING_TOKENS
        assert settings.chunk_planner_deadline_seconds == DEFAULT_PLANNER_DEADLINE_SECONDS
        assert settings.chunk_planner_concurrency == DEFAULT_PLANNER_CONCURRENCY

    def test_a_named_chunk_planner_deployment_is_kept(self) -> None:
        assert IngestionSettings(chunk_planner_model="example-deployment").chunk_planner_model == "example-deployment"

    @pytest.mark.parametrize("field", ["chunk_planner_ceiling_tokens", "chunk_planner_deadline_seconds", "chunk_planner_concurrency"])
    def test_a_chunk_planner_limit_that_is_not_positive_is_refused(self, field: str) -> None:
        with pytest.raises(ValidationError, match=field):
            IngestionSettings.model_validate({field: 0})
