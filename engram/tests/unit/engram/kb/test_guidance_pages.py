"""Guidance pages: policy prose in markdown, merged into the spec at load, the JSON strings the fallback."""

import json

from pathlib import Path

import pytest

from engram.kb.contracts import KnowledgeBaseSchema, KnowledgeBaseSchemaMissingError, load_knowledge_base_schema
from engram.kb.infrastructure.guidance_pages import (
    apply_guidance,
    read_compounding_guidance,
    read_documentation_guidance,
    render_compounding_policy,
    render_documentation_policy,
)

_SPEC = {
    "slug": "alpha",
    "page_types": [
        {"page_type": "spec-doc", "guidance": "json page_type guidance", "status_values": ["draft"], "canonical_sections": [
            {"slug": "problem", "guidance": "json problem"},
            {"slug": "out_of_scope", "heading": "Out of Scope", "guidance": "json scope"},
        ]},
        {"page_type": "flag", "status_values": ["pending"]},
    ],
    "compounding": {"entries": [
        {"name": "flag-ideas", "from": {"plane": "pages", "page_types": ["spec-doc"]}, "to": ["flag"], "guidance": "json entry"},
    ]},
}

_DOC_PAGE = """---
id: meta.documentation-policy
type: meta
title: Documentation policy
---
# Documentation policy

## spec-doc
Page page_type guidance,
two lines.

### Problem
Page problem guidance.

### Out of Scope
Page scope guidance.

### Ghost Section
Ignored, no such section.

## ghost-page_type
Ignored, no such page_type.
"""

_COMPOUNDING_PAGE = """---
id: meta.compounding-policy
type: meta
title: Compounding policy
---
# Compounding policy

## flag-ideas
Page entry guidance.

## ghost-entry
Ignored.
"""


def _knowledge_base(tmp_path: Path, *, doc: str | None = _DOC_PAGE, compounding: str | None = _COMPOUNDING_PAGE) -> Path:
    (tmp_path / "_meta").mkdir()
    (tmp_path / "knowledge-base-schema.json").write_text(json.dumps(_SPEC), encoding="utf-8")
    if doc is not None:
        (tmp_path / "_meta" / "documentation-policy.md").write_text(doc, encoding="utf-8")
    if compounding is not None:
        (tmp_path / "_meta" / "compounding-policy.md").write_text(compounding, encoding="utf-8")
    return tmp_path


def test_documentation_page_reads_kinds_and_sections_by_heading(tmp_path: Path) -> None:
    knowledge_base_path = _knowledge_base(tmp_path)
    spec = KnowledgeBaseSchema.from_path(knowledge_base_path / "knowledge-base-schema.json")

    read = read_documentation_guidance(knowledge_base_path, spec)

    assert set(read) == {"spec-doc"}
    assert read["spec-doc"].guidance == "Page page_type guidance,\ntwo lines."
    assert read["spec-doc"].sections["problem"] == "Page problem guidance."
    assert read["spec-doc"].sections["out_of_scope"] == "Page scope guidance."


def test_compounding_page_reads_entries(tmp_path: Path) -> None:
    knowledge_base_path = _knowledge_base(tmp_path)
    spec = KnowledgeBaseSchema.from_path(knowledge_base_path / "knowledge-base-schema.json")

    assert read_compounding_guidance(knowledge_base_path, spec)["flag-ideas"] == "Page entry guidance."


def test_page_prose_wins_and_json_stays_the_fallback(tmp_path: Path) -> None:
    spec = load_knowledge_base_schema(_knowledge_base(tmp_path))

    page_type = spec.page_type("spec-doc")
    assert page_type is not None
    assert page_type.guidance == "Page page_type guidance,\ntwo lines."
    assert {s.slug: s.guidance for s in page_type.canonical_sections} == {"problem": "Page problem guidance.", "out_of_scope": "Page scope guidance."}
    assert spec.compounding.entries[0].guidance == "Page entry guidance."
    flag = spec.page_type("flag")
    assert flag is not None
    assert flag.guidance == ""


def test_from_path_merges_the_pages_beside_the_spec_file(tmp_path: Path) -> None:
    spec = KnowledgeBaseSchema.from_path(_knowledge_base(tmp_path) / "knowledge-base-schema.json")

    page_type = spec.page_type("spec-doc")
    assert page_type is not None
    assert page_type.guidance == "Page page_type guidance,\ntwo lines."
    assert spec.compounding.entries[0].guidance == "Page entry guidance."


def test_a_guidance_page_alone_does_not_stand_in_for_a_spec(tmp_path: Path) -> None:
    """A `_meta/compounding-policy.md` page with no `knowledge-base-schema.json` beside it is still refused."""
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "compounding-policy.md").write_text(
        "---\nid: meta.compounding-policy\ntype: meta\ntitle: Compounding policy\n---\n## digest-sources\nPage digest guidance.\n",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeBaseSchemaMissingError, match="studio init --template"):
        load_knowledge_base_schema(tmp_path)


def test_missing_pages_leave_the_json_guidance(tmp_path: Path) -> None:
    spec = load_knowledge_base_schema(_knowledge_base(tmp_path, doc=None, compounding=None))

    page_type = spec.page_type("spec-doc")
    assert page_type is not None
    assert page_type.guidance == "json page_type guidance"
    assert spec.compounding.entries[0].guidance == "json entry"


def test_malformed_page_leaves_the_spec_as_loaded(tmp_path: Path) -> None:
    spec = load_knowledge_base_schema(_knowledge_base(tmp_path, doc="---\n: not yaml [\n---\n## spec-doc\nx\n"))

    page_type = spec.page_type("spec-doc")
    assert page_type is not None
    assert page_type.guidance == "json page_type guidance"


def test_pages_can_only_fill_declared_structure(tmp_path: Path) -> None:
    spec = load_knowledge_base_schema(_knowledge_base(tmp_path))

    assert spec.page_type("ghost-page_type") is None
    page_type = spec.page_type("spec-doc")
    assert page_type is not None
    assert [s.slug for s in page_type.canonical_sections] == ["problem", "out_of_scope"]


def test_fenced_code_inside_guidance_is_prose_not_headings(tmp_path: Path) -> None:
    fenced = "## spec-doc\nWrite it like this:\n\n```markdown\n### Problem\nnot a section\n## flag\nnot a page_type\n```\n\n### Problem\nReal problem guidance.\n"
    knowledge_base_path = _knowledge_base(tmp_path, doc=f"---\nid: meta.documentation-policy\ntype: meta\ntitle: Documentation policy\n---\n{fenced}")
    spec = KnowledgeBaseSchema.from_path(knowledge_base_path / "knowledge-base-schema.json")

    read = read_documentation_guidance(knowledge_base_path, spec)

    assert set(read) == {"spec-doc"}
    assert read["spec-doc"].guidance == "Write it like this:\n\n```markdown\n### Problem\nnot a section\n## flag\nnot a page_type\n```"
    assert read["spec-doc"].sections == {"problem": "Real problem guidance."}


def test_render_round_trips_through_read(tmp_path: Path) -> None:
    spec = KnowledgeBaseSchema.model_validate(_SPEC)
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "documentation-policy.md").write_text(render_documentation_policy(spec), encoding="utf-8")
    (tmp_path / "_meta" / "compounding-policy.md").write_text(render_compounding_policy(spec), encoding="utf-8")

    assert read_documentation_guidance(tmp_path, spec)["spec-doc"].sections["problem"] == "json problem"
    assert read_compounding_guidance(tmp_path, spec)["flag-ideas"] == "json entry"
    assert apply_guidance(spec, tmp_path) == spec
