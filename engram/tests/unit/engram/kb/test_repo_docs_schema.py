"""The live repo-docs knowledge_base_schema: loads, declares the estate, and satisfies the knowledge-base-shape check."""

from pathlib import Path

from engram.config import KnowledgeBaseSettings
from engram.kb.contracts import PageTypeSpec, load_knowledge_base_schema

_REPO = Path(__file__).resolve().parents[5]

_EXPECTED_KINDS = {
    "adr", "architecture", "reference", "explanation", "how-to", "glossary",
    "spec-doc", "plan-doc", "brainstorming-doc", "architecture-doc", "memory-unit", "hub", "module",
}


class TestRepoDocsSchema:
    def test_live_schema_loads_with_the_estate_page_types(self) -> None:
        spec = load_knowledge_base_schema(_REPO / "docs")
        assert spec.slug == "repo-docs"
        assert spec.retrieval.collection == "repo-docs"
        assert spec.page_type_values == frozenset(_EXPECTED_KINDS)

    def test_knowledge_base_root_passes_the_shape_check(self) -> None:
        settings = KnowledgeBaseSettings(path=_REPO / "docs")
        assert settings.path.name == "docs"


def test_prefixless_page_type_disclosure_summary_omits_the_prefix_clause() -> None:
    pt = PageTypeSpec(page_type="adr", id_prefix="")

    assert "prefix" not in pt.disclosure_summary
    assert pt.disclosure_summary.startswith("sections [")
