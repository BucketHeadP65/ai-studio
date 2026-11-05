"""The live repo-docs knowledge_base_schema: loads, declares the estate, and satisfies the knowledge-base-shape check."""

from pathlib import Path

from core.markdown import split_frontmatter

from engram.config import KnowledgeBaseSettings
from engram.kb.contracts import AuthoringPayload, PageTypeSpec, load_knowledge_base_schema, payload_of, render

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


_ADR = """\
---
id: core-adr-0099-a-sample-decision
aliases: ["core-adr-0099-a-sample-decision"]
type: adr
app: core
status: accepted
tags: []
title: "0099: A sample decision"
---

# 0099: A sample decision

**Status:** Accepted (2026-08-26).

## Context

Two processes ran the same job.

## Decision

One worker claims each job.

## Consequences

A crash repeats at most one delivery.
"""


def test_an_adr_that_opens_with_its_h1_and_a_status_line_renders_back_byte_for_byte() -> None:
    spec = load_knowledge_base_schema(_REPO / "docs")

    payload = payload_of(_ADR, source_path="../core/docs/adr/0099-a-sample-decision.md", spec=spec)

    sections = payload["sections"]
    assert isinstance(sections, list)
    assert sections[0] == {"heading": "0099: A sample decision", "body": "**Status:** Accepted (2026-08-26).", "level": 1}
    rendered = render(AuthoringPayload.model_validate({**payload, "knowledge_base_schema": spec}), spec=spec)
    assert split_frontmatter(rendered) == split_frontmatter(_ADR), "the same frontmatter values and the same body"
