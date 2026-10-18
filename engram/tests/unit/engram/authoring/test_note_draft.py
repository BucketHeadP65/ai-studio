"""`NoteDraft`: a proposal that cannot assign what its destination assigns."""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from engram.authoring.contracts import ASSIGNED_FRONTMATTER, NoteDraft, NoteSection

DRAFT = {
    "page_type": "note",
    "title": "What the run concluded",
    "summary": "One reading of what the evidence supports.",
    "frontmatter": {"status": "synthesized"},
    "sections": [{"heading": "TL;DR", "body": "It holds under the stated conditions."}],
}


def test_a_draft_keeps_its_own_fields_and_its_schema_declared_frontmatter() -> None:
    draft = NoteDraft.model_validate(DRAFT)
    assert draft.page_type == "note"
    assert draft.frontmatter == {"status": "synthesized"}
    assert draft.sections == (NoteSection(heading="TL;DR", body="It holds under the stated conditions."),)


@pytest.mark.parametrize("assigned", sorted(ASSIGNED_FRONTMATTER))
def test_frontmatter_may_not_carry_a_field_the_destination_assigns(assigned: str) -> None:
    with pytest.raises(ValidationError, match=assigned):
        NoteDraft.model_validate({**DRAFT, "frontmatter": {"status": "synthesized", assigned: "mine"}})


def test_a_field_nobody_declared_is_refused_rather_than_carried() -> None:
    with pytest.raises(ValidationError, match="path"):
        NoteDraft.model_validate({**DRAFT, "path": "Wiki/Notes/mine.md"})


def test_a_blank_title_or_page_type_is_not_a_draft() -> None:
    with pytest.raises(ValidationError):
        NoteDraft.model_validate({**DRAFT, "title": "   "})
    with pytest.raises(ValidationError):
        NoteDraft.model_validate({**DRAFT, "page_type": ""})


def test_the_payload_carries_the_minted_id_and_the_drafts_own_fields() -> None:
    payload = NoteDraft.model_validate(DRAFT).payload("note.0123456789abcdef01234567")
    assert payload["frontmatter"] == {
        "id": "note.0123456789abcdef01234567",
        "type": "note",
        "title": "What the run concluded",
        "summary": "One reading of what the evidence supports.",
        "status": "synthesized",
    }
    assert payload["sections"] == [{"heading": "TL;DR", "body": "It holds under the stated conditions."}]
