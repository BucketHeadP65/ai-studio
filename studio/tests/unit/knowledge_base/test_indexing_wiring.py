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

from engram.indexing.contracts import ChunkSizes
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec

from studio.knowledge_base.settings import IngestionSettings, KnowledgeBaseSettings
from studio.knowledge_base.wiring.indexing import _path_root, ingestion_chunk_sizes

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base(root: Path) -> KnowledgeBaseSettings:
    """A `KnowledgeBaseSettings` over a minimal legacy-shaped knowledge base directory (`Units/` + `_meta/`)."""
    (root / "Units").mkdir(parents=True)
    (root / "_meta").mkdir(parents=True)
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
