"""`find_knowledge_base_root` and `knowledge_base_slug`, the knowledge base-identity primitives `resolve_knowledge_base_root` builds on."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from studio.knowledge_base.settings import find_knowledge_base_root, knowledge_base_slug

if TYPE_CHECKING:
    from pathlib import Path


def _knowledge_base(root: Path, slug: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text(json.dumps({"slug": slug, "page_types": [{"page_type": "note"}]}), encoding="utf-8")
    return root


class TestFindKnowledgeBaseRoot:
    def test_finds_the_spec_in_the_start_directory(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")

        assert find_knowledge_base_root(root) == root

    def test_walks_up_to_a_parent_that_holds_a_spec(self, tmp_path: Path):
        root = _knowledge_base(tmp_path / "alpha", "alpha")
        deep = root / "Units" / "topic"
        deep.mkdir(parents=True)

        assert find_knowledge_base_root(deep) == root

    def test_never_walks_down(self, tmp_path: Path):
        _knowledge_base(tmp_path / "outside" / "alpha", "alpha")

        assert find_knowledge_base_root(tmp_path / "outside") is None

    def test_stops_at_the_filesystem_root(self, tmp_path: Path):
        assert find_knowledge_base_root(tmp_path) is None


class TestKnowledgeBaseSlug:
    def test_reads_the_slug_from_the_spec(self, tmp_path: Path):
        assert knowledge_base_slug(_knowledge_base(tmp_path / "alpha", "alpha-vault")) == "alpha-vault"

    def test_falls_back_to_the_directory_name_without_a_spec(self, tmp_path: Path):
        (tmp_path / "legacy").mkdir()

        assert knowledge_base_slug(tmp_path / "legacy") == "legacy"

    def test_falls_back_to_the_directory_name_when_the_spec_will_not_parse(self, tmp_path: Path):
        root = tmp_path / "broken"
        root.mkdir()
        (root / "knowledge-base-schema.json").write_text("{not json", encoding="utf-8")

        assert knowledge_base_slug(root) == "broken"
