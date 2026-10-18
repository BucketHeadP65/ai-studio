"""`repo_docs_knowledge_base`: the checkout's own docs knowledge base as an authoring knowledge base, or nothing when it cannot be."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.kb.domain.schema import RetrievalSpec
from engram.testing.specs import research_spec

from harness.adapters import knowledge_base as knowledge_base_adapter

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_the_repo_docs_target_is_named_by_its_spec_and_rooted_beside_it(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    spec = research_spec().model_copy(update={"slug": "repo-docs", "retrieval": RetrievalSpec(collection="repo-docs")})
    spec_path = docs / "knowledge-base-schema.json"
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")

    knowledge_base = knowledge_base_adapter.repo_docs_knowledge_base(spec_path)

    assert knowledge_base is not None
    assert knowledge_base.name == "repo-docs"
    assert knowledge_base.collection == "repo-docs"
    assert knowledge_base.path == docs
    assert knowledge_base.schema.slug == "repo-docs"


def test_a_missing_spec_file_leaves_the_repo_docs_knowledge_base_read_only(tmp_path: Path) -> None:
    assert knowledge_base_adapter.repo_docs_knowledge_base(tmp_path / "docs" / "knowledge-base-schema.json") is None


def test_a_spec_that_fails_to_load_is_logged_and_leaves_the_knowledge_base_read_only(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    spec_path = tmp_path / "knowledge-base-schema.json"
    spec_path.write_text("{not json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        knowledge_base = knowledge_base_adapter.repo_docs_knowledge_base(spec_path)

    assert knowledge_base is None
    assert "repo-docs knowledge base stays read-only" in caplog.text


def test_this_checkout_offers_its_own_docs_knowledge_base_for_writing() -> None:
    knowledge_base = knowledge_base_adapter.repo_docs_knowledge_base()

    assert knowledge_base is not None
    assert knowledge_base.name == "repo-docs"
    assert knowledge_base.path == knowledge_base_adapter.REPO_DOCS_SCHEMA.parent
