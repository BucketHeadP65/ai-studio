"""`AuthoringTools.skeleton` and `knowledge_base_profile`'s `page_types` digest: a page type's own ceremony, served for authoring."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import ModelRetry

from engram.kb.contracts import CanonicalSection, KnowledgeBaseSchema, PageTypeSpec
from engram.testing.specs import research_spec
from tests.unit.engram.retrieval.test_authoring_tools import _doors

if TYPE_CHECKING:
    from pathlib import Path


def _fresh_research_spec() -> KnowledgeBaseSchema:
    """A `research_spec()`-equivalent `KnowledgeBaseSchema`, freshly constructed rather than the process-wide singleton.

    `research_spec()` is `lru_cache`d to one shared instance, and `KnowledgeBaseSchema`'s derived
    lookups (`prefix_by_page_type`, `page_types`, ...) are `cached_property`s that `model_copy` carries over
    verbatim in its `__dict__` copy: building on the singleton directly would risk a `model_copy`
    inheriting a lookup some earlier, unrelated test already computed against the singleton's own
    `page_types`, silently stale for a spec that adds one.
    """
    return KnowledgeBaseSchema.model_validate(research_spec().model_dump(mode="json"))


def _guided_spec() -> KnowledgeBaseSchema:
    """The research spec plus a `spec-doc` page_type whose sections and page_type carry authoring guidance."""
    base = _fresh_research_spec()
    spec_doc = PageTypeSpec(
        page_type="spec-doc",
        id_prefix="spec.",
        status_values=("draft", "approved"),
        canonical_sections=(
            CanonicalSection(slug="problem", guidance="State what breaks today."),
            CanonicalSection(slug="decisions", heading="Decisions", guidance="One row per decision."),
        ),
        guidance="Write before implementation starts.",
    )
    return base.model_copy(update={"page_types": (*base.page_types, spec_doc)})


async def test_skeleton_seeds_every_canonical_section_with_its_heading_and_guidance(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, spec=_guided_spec())

    skeleton = await doors.skeleton("research", "spec-doc")

    assert skeleton["frontmatter"] == {"id": "spec.", "type": "spec-doc", "title": "", "summary": "", "status": "draft"}
    assert skeleton["guidance"] == "Write before implementation starts."
    assert skeleton["sections"] == [
        {"heading": "Problem", "body": "", "guidance": "State what breaks today."},
        {"heading": "Decisions", "body": "", "guidance": "One row per decision."},
    ]
    rules = skeleton["rules"]
    assert isinstance(rules, list)
    assert any("slug of the title" in rule for rule in rules), "the agent is told to complete the id itself"


async def test_skeleton_status_falls_back_to_active_when_the_kind_declares_none(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)  # the research spec's "paper" page_type declares no status_values

    skeleton = await doors.skeleton("research", "paper")

    frontmatter = skeleton["frontmatter"]
    assert isinstance(frontmatter, dict)
    assert frontmatter["status"] == "active"
    assert frontmatter["id"] == "paper."


async def test_skeleton_of_an_unknown_kind_names_every_valid_kind(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path)

    with pytest.raises(ModelRetry) as excinfo:
        await doors.skeleton("research", "nope")

    message = str(excinfo.value)
    assert "no page_type 'nope' in knowledge base 'research'" in message
    assert "paper" in message
    assert "talk" in message


async def test_knowledge_base_profile_carries_every_kinds_guidance_and_sections(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, spec=_guided_spec())

    profile = await doors.knowledge_base_profile("research")

    page_types = profile["page_types"]
    assert isinstance(page_types, list)
    spec_doc = next(entry for entry in page_types if entry["page_type"] == "spec-doc")
    assert spec_doc["guidance"] == "Write before implementation starts."
    assert spec_doc["sections"] == [
        {"slug": "problem", "heading": "Problem", "guidance": "State what breaks today."},
        {"slug": "decisions", "heading": "Decisions", "guidance": "One row per decision."},
    ]
    talk = next(entry for entry in page_types if entry["page_type"] == "talk")
    assert talk["guidance"] == ""
    assert {"slug": "tl_dr", "heading": "TL;DR", "guidance": ""} in talk["sections"]


async def test_a_skeleton_answer_round_trips_into_write_note_with_guidance_ignored(tmp_path: Path) -> None:
    doors, _, _ = _doors(tmp_path, spec=_guided_spec())
    skeleton = await doors.skeleton("research", "spec-doc")
    base_frontmatter = skeleton["frontmatter"]
    assert isinstance(base_frontmatter, dict)
    frontmatter = {**base_frontmatter, "id": "spec.test-doc", "title": "Test doc"}
    payload = {**skeleton, "frontmatter": frontmatter}
    sections = payload["sections"]
    assert isinstance(sections, list)
    assert all("guidance" in section for section in sections), "the payload still carries guidance keys, unmodified"

    said = await doors.write_note("research", payload)

    assert said.startswith("spec.test-doc written to")
    written = (tmp_path / "Wiki" / "test-doc.md").read_text(encoding="utf-8")
    assert "State what breaks today." not in written
    assert "One row per decision." not in written
