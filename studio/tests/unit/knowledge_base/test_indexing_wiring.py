r"""Unit specs for the index slice's `path_root` wiring (`studio/knowledge_base/wiring/indexing.py`).

`KnowledgeBaseSchema.path_root` (`"parent"`, the default, or `"knowledge_base"`) governs the filesystem base
`index_handler_provider` threads into `IndexKnowledgeBaseHandler` — pinned here by calling
`_path_root` directly, without wiring the provider's heavier dependencies (reader, embedder, UoW
factory, dashboard, summary stage), since only the base computation is under test. The handler
itself threads whatever base it receives to both the full-rebuild and incremental validate calls
from one stored value (`engram/tests/unit/engram/index/test_incremental_path_fields.py`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from engram.indexing.contracts import PARAGRAPH_NUMBERING_VERSION, ChunkSizes, indexer_version
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec
from sqlalchemy.ext.asyncio import create_async_engine

from studio.knowledge_base.settings import AzureOpenAiSettings, IngestionSettings, KnowledgeBaseSettings
from studio.knowledge_base.wiring.indexing import (
    _path_root,
    indexer_version_changed,
    ingestion_chunk_planner,
    ingestion_chunk_sizes,
    ingestion_cut_plan_stage,
    ingestion_indexer_version,
)

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base(root: Path) -> KnowledgeBaseSettings:
    """A `KnowledgeBaseSettings` over a minimal knowledge base directory (`_meta/` and a schema file)."""
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return KnowledgeBaseSettings(path=root)


def _spec(path_root: Literal["parent", "knowledge_base"] = "parent") -> KnowledgeBaseSchema:
    return KnowledgeBaseSchema(slug="estate", page_types=(PageTypeSpec(page_type="module", id_prefix="mod."),), path_root=path_root)


class TestPathRootWiring:
    """`_path_root` resolves each `KnowledgeBaseSchema.path_root` setting to its declared filesystem base."""

    def test_the_default_parent_setting_resolves_to_the_knowledge_base_roots_parent(self, tmp_path: Path) -> None:
        # Mirrors repo-docs: a knowledge base rooted at `docs/`, whose module `path` fields point into
        # the repo root (the knowledge base root's parent) it sits under.
        knowledge_base_root = tmp_path / "estate" / "docs"
        knowledge_base_root.mkdir(parents=True)

        assert _path_root(_knowledge_base(knowledge_base_root), _spec("parent")) == knowledge_base_root.parent

    def test_the_knowledge_base_setting_resolves_to_the_knowledge_base_root_itself(self, tmp_path: Path) -> None:
        # A standalone vault: its `path_fields` values are knowledge base-relative, not repo-relative.
        knowledge_base_root = tmp_path / "vault"
        knowledge_base_root.mkdir(parents=True)

        assert _path_root(_knowledge_base(knowledge_base_root), _spec("knowledge_base")) == knowledge_base_root

    def test_an_unspecified_path_root_defaults_to_parent(self, tmp_path: Path) -> None:
        # A spec that never declares `path_root` gets today's behavior, unchanged.
        knowledge_base_root = tmp_path / "estate" / "docs"
        knowledge_base_root.mkdir(parents=True)
        default_spec = KnowledgeBaseSchema(slug="estate", page_types=(PageTypeSpec(page_type="module", id_prefix="mod."),))

        assert default_spec.path_root == "parent"
        assert _path_root(_knowledge_base(knowledge_base_root), default_spec) == knowledge_base_root.parent


class TestChunkSizesWiring:
    def test_the_ingestion_settings_become_the_index_handlers_chunk_sizes(self) -> None:
        ingestion = IngestionSettings(chunk_target_tokens=300, chunk_max_tokens=600)

        assert ingestion_chunk_sizes(ingestion) == ChunkSizes(target_tokens=300, max_tokens=600)


_AZURE = AzureOpenAiSettings(endpoint="https://example.openai.azure.com")

_ENGINE_URL = "postgresql+asyncpg://example:example@localhost/example"


class TestChunkPlannerWiring:
    def test_the_ingestion_settings_name_the_chunk_planners_deployment(self) -> None:
        planner = ingestion_chunk_planner(_AZURE, IngestionSettings(chunk_planner_model="example-deployment"))

        assert planner is not None
        assert planner.identity == "example-deployment/prompt-1/thinking-low"

    def test_an_empty_chunk_planner_model_indexes_with_no_chunk_planner(self) -> None:
        assert ingestion_chunk_planner(_AZURE, IngestionSettings(chunk_planner_model="")) is None

    def test_no_azure_endpoint_indexes_with_no_chunk_planner(self) -> None:
        assert ingestion_chunk_planner(AzureOpenAiSettings(endpoint=""), IngestionSettings(chunk_planner_model="example-deployment")) is None

    def test_the_cut_plan_stage_carries_the_settings_planner_and_ceiling(self) -> None:
        ingestion = IngestionSettings(chunk_planner_model="example-deployment", chunk_planner_ceiling_tokens=9_000)

        stage = ingestion_cut_plan_stage(create_async_engine(_ENGINE_URL), _AZURE, ingestion)

        assert stage is not None
        assert (stage.planner, stage.ceiling_tokens) == (f"example-deployment/prompt-1/thinking-low/paragraphs-{PARAGRAPH_NUMBERING_VERSION}", 9_000)

    def test_no_chunk_planner_means_no_cut_plan_stage(self) -> None:
        assert ingestion_cut_plan_stage(create_async_engine(_ENGINE_URL), AzureOpenAiSettings(endpoint=""), IngestionSettings()) is None

    def test_the_indexer_version_names_the_settings_chunk_planner_as_the_handler_stores_it(self) -> None:
        ingestion = IngestionSettings(chunk_target_tokens=300, chunk_max_tokens=600, chunk_planner_model="example-deployment", chunk_planner_ceiling_tokens=9_000)
        stage = ingestion_cut_plan_stage(create_async_engine(_ENGINE_URL), _AZURE, ingestion)

        assert stage is not None
        assert ingestion_indexer_version(_AZURE, ingestion) == indexer_version(
            ChunkSizes(target_tokens=300, max_tokens=600), planner=stage.planner, planner_ceiling_tokens=stage.ceiling_tokens
        )
        assert ingestion_indexer_version(_AZURE, ingestion).endswith(f"+9000+example-deployment/prompt-1/thinking-low/paragraphs-{PARAGRAPH_NUMBERING_VERSION}")

    def test_the_indexer_version_with_no_chunk_planner_names_the_chunk_sizes_only(self) -> None:
        ingestion = IngestionSettings(chunk_target_tokens=300, chunk_max_tokens=600)

        assert ingestion_indexer_version(AzureOpenAiSettings(endpoint=""), ingestion) == indexer_version(ChunkSizes(target_tokens=300, max_tokens=600))

    def test_a_stored_indexer_version_other_than_the_settings_one_has_changed(self) -> None:
        ingestion = IngestionSettings(chunk_target_tokens=300, chunk_max_tokens=600)
        current = ingestion_indexer_version(AzureOpenAiSettings(endpoint=""), ingestion)

        assert indexer_version_changed(current, AzureOpenAiSettings(endpoint=""), ingestion) is False
        assert indexer_version_changed(indexer_version(ChunkSizes(target_tokens=400, max_tokens=800)), AzureOpenAiSettings(endpoint=""), ingestion) is True

    def test_no_stored_indexer_version_is_no_change(self) -> None:
        assert indexer_version_changed(None, AzureOpenAiSettings(endpoint=""), IngestionSettings()) is False
