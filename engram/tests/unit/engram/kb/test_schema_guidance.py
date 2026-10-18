"""Guidance on the spec models: declared per page_type and per section, round-tripping through knowledge_base_schema JSON."""

from engram.kb.contracts import CanonicalSection, PageTypeSpec


def test_bare_slug_still_serializes_bare() -> None:
    section = CanonicalSection.model_validate("findings")
    assert section.guidance == ""
    assert section.model_dump() == "findings"


def test_guidance_round_trips() -> None:
    section = CanonicalSection.model_validate(
        {"slug": "acceptance_criteria", "heading": "Acceptance Criteria", "guidance": "Checkboxes, each verifiable."}
    )
    dumped = section.model_dump()
    assert dumped == {
        "slug": "acceptance_criteria",
        "heading": "Acceptance Criteria",
        "guidance": "Checkboxes, each verifiable.",
    }
    assert CanonicalSection.model_validate(dumped) == section


def test_guidance_without_heading_serializes_as_object() -> None:
    section = CanonicalSection.model_validate({"slug": "problem", "guidance": "The gap, no solution language."})
    assert section.model_dump() == {"slug": "problem", "guidance": "The gap, no solution language."}


def test_page_type_guidance_defaults_empty() -> None:
    pt = PageTypeSpec(page_type="spec-doc")
    assert pt.guidance == ""
