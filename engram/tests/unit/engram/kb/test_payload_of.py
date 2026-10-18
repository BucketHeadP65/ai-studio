"""`payload_of`: a page's markdown as the write_note payload that renders it back whole."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.kb.contracts import AuthoringPayload, payload_of, render
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Mapping

_PAYLOAD: dict[str, object] = {
    "frontmatter": {
        "id": "talk.agents-that-plan",
        "type": "talk",
        "title": "Agents that plan",
        "summary": "A talk on planning agents.",
        "status": "active",
        "digest_of": "yt-abc",
        "channel": "Example Channel",
        "published": "2024-05-01",
        "alternative_to": ["[[talk.other-talk]]"],
        "created": "2026-10-14",
    },
    "sections": [
        {"heading": "TL;DR", "body": "Planning agents keep a task list the model owns."},
        {"heading": "Findings", "body": ""},
        {"heading": "Entities", "body": "Kestrel and Wren."},
        {"heading": "Open questions", "body": "Whether it scales."},
        {"heading": "Later", "level": 3, "body": "Revisit next year."},
    ],
    "claims": [
        {
            "text": "Planning agents keep a task list.",
            "loc": "§1",
            "quote": "agents keep a task list",
            "kind": "observation",
            "subject": "[[ent.kestrel]]",
            "condition": "long sessions",
        },
        {
            "text": "The list survives compaction.",
            "loc": "§2",
            "quote": "survives compaction",
            "kind": "observation",
            "on_task": "[[task.planning]]",
            "on_dataset": ["[[dataset.alpha]]", "[[dataset.beta]]"],
        },
        {
            "text": "Wren beats the baseline.",
            "loc": "§3",
            "quote": "beats the baseline",
            "kind": "observation",
            "edges": [["outperforms", "[[ent.baseline]]"]],
            "magnitude": "12 points",
        },
    ],
}


def _rendered(payload: Mapping[str, object]) -> str:
    spec = research_spec()
    return render(AuthoringPayload.model_validate({**payload, "knowledge_base_schema": spec}), spec=spec)


def test_a_page_s_payload_renders_back_to_the_same_markdown() -> None:
    spec = research_spec()
    text = _rendered(_PAYLOAD)

    payload = payload_of(text, source_path="Wiki/Talks/agents-that-plan.md", spec=spec)

    assert _rendered(payload) == text


def test_the_payload_names_each_declared_claim_field_as_a_key_of_its_own_and_leaves_the_claims_body_empty() -> None:
    spec = research_spec()

    payload = payload_of(_rendered(_PAYLOAD), source_path="note.md", spec=spec)

    claims = payload["claims"]
    sections = payload["sections"]
    assert isinstance(claims, list)
    assert isinstance(sections, list)
    first, second, third = claims
    assert isinstance(second, dict)
    assert isinstance(third, dict)
    assert first == {
        "anchor": "^c1",
        "text": "Planning agents keep a task list.",
        "loc": "§1",
        "quote": "agents keep a task list",
        "kind": "observation",
        "subject": "[[ent.kestrel]]",
        "condition": "long sessions",
    }
    assert second["on_task"] == "[[task.planning]]", "a link scalar field holds one link string"
    assert second["on_dataset"] == ["[[dataset.alpha]]", "[[dataset.beta]]"]
    assert third["edges"] == [["outperforms", "[[ent.baseline]]"]]
    assert {"heading": "Findings", "body": "", "level": 2} in sections, "the renderer writes the claims, never a body"
    assert {"heading": "Later", "body": "Revisit next year.", "level": 3} in sections


def test_a_frontmatter_list_written_in_flow_style_is_carried_with_the_same_values() -> None:
    spec = research_spec()
    text = _rendered(_PAYLOAD).replace("alternative_to:\n- '[[talk.other-talk]]'\n", 'alternative_to: ["[[talk.other-talk]]"]\n', 1)
    assert 'alternative_to: ["[[talk.other-talk]]"]' in text

    payload = payload_of(text, source_path="note.md", spec=spec)

    frontmatter = payload["frontmatter"]
    assert isinstance(frontmatter, dict)
    assert frontmatter["alternative_to"] == ["[[talk.other-talk]]"]


def _with_tl_dr_body(body: str) -> dict[str, object]:
    """`_PAYLOAD` with its TL;DR section's body replaced, the way a writer puts extra headings inside a body."""
    sections = _PAYLOAD["sections"]
    assert isinstance(sections, list)
    return {**_PAYLOAD, "sections": [{"heading": "TL;DR", "body": body}, *sections[1:]]}


def test_a_section_the_page_type_does_not_declare_between_declared_ones_renders_back_in_place() -> None:
    spec = research_spec()
    text = _rendered(_with_tl_dr_body("Planning agents keep a task list.\n\n## Background\n\nRecorded at a meetup."))
    assert "## TL;DR\n\nPlanning agents keep a task list.\n\n## Background\n\nRecorded at a meetup.\n\n## Findings\n" in text

    payload = payload_of(text, source_path="note.md", spec=spec)

    assert _rendered(payload) == text
    sections = payload["sections"]
    assert isinstance(sections, list)
    assert sections[0] == {
        "heading": "TL;DR",
        "body": "Planning agents keep a task list.\n\n## Background\n\nRecorded at a meetup.",
        "level": 2,
    }


def test_only_the_section_that_would_move_is_carried_and_every_other_section_stays_its_own() -> None:
    spec = research_spec()
    text = _rendered(_with_tl_dr_body("Planning agents keep a task list.\n\n## Background\n\nRecorded at a meetup."))

    payload = payload_of(text, source_path="note.md", spec=spec)

    assert _rendered(payload) == text
    assert payload["sections"] == [
        {"heading": "TL;DR", "body": "Planning agents keep a task list.\n\n## Background\n\nRecorded at a meetup.", "level": 2},
        {"heading": "Findings", "body": "", "level": 2},
        {"heading": "Entities", "body": "Kestrel and Wren.", "level": 2},
        {"heading": "Open questions", "body": "Whether it scales.", "level": 2},
        {"heading": "Later", "body": "Revisit next year.", "level": 3},
    ]


def test_a_heading_with_no_blank_line_under_it_renders_back_as_written() -> None:
    spec = research_spec()
    text = _rendered(_with_tl_dr_body("Planning agents keep a task list.\n\n### Setting\nRecorded at a meetup."))
    assert "\n### Setting\nRecorded at a meetup.\n" in text

    payload = payload_of(text, source_path="note.md", spec=spec)

    assert _rendered(payload) == text


def test_a_page_with_a_section_in_the_middle_and_text_before_its_first_heading_is_refused() -> None:
    spec = research_spec()
    text = _rendered(_with_tl_dr_body("Planning agents keep a task list.\n\n## Background\n\nRecorded at a meetup."))
    text = text.replace("\n## TL;DR", "\nAn opening line no section holds.\n\n## TL;DR", 1)

    with pytest.raises(ValueError, match=r"holds text a payload cannot carry whole, first at 'An opening line no section holds\.'"):
        payload_of(text, source_path="note.md", spec=spec)


def test_a_section_the_page_type_does_not_declare_before_the_first_declared_one_is_refused() -> None:
    spec = research_spec()
    text = _rendered(_PAYLOAD).replace("\n## TL;DR", "\n## Background\n\nRecorded at a meetup.\n\n## TL;DR", 1)

    with pytest.raises(ValueError, match=r"holds text a payload cannot carry whole, first at '## Background'"):
        payload_of(text, source_path="note.md", spec=spec)


def test_a_section_the_page_type_does_not_declare_after_a_claims_section_that_holds_claims_is_refused() -> None:
    spec = research_spec()
    text = _rendered(_PAYLOAD).replace("\n## Entities\n", "\n## Background\n\nRecorded at a meetup.\n\n## Entities\n", 1)
    assert "^c3\n" in text.split("## Background")[0]

    with pytest.raises(ValueError, match=r"holds text a payload cannot carry whole, first at '## Background'"):
        payload_of(text, source_path="note.md", spec=spec)


def test_a_page_with_text_before_its_first_heading_is_refused() -> None:
    spec = research_spec()
    text = _rendered(_PAYLOAD).replace("\n## TL;DR", "\nAn opening line no section holds.\n\n## TL;DR", 1)

    with pytest.raises(ValueError, match=r"holds text a payload cannot carry whole, first at 'An opening line no section holds\.'"):
        payload_of(text, source_path="note.md", spec=spec)


def test_a_claim_the_grammar_does_not_read_back_is_refused_rather_than_dropped() -> None:
    """A claim whose text holds a blank line renders, but the parser does not read it back as a claim."""
    spec = research_spec()
    two_paragraphs = {"text": "Wren beats the baseline.\n\nThe margin holds.", "loc": "§4", "quote": "margin holds", "kind": "observation"}
    claims = _PAYLOAD["claims"]
    assert isinstance(claims, list)
    text = _rendered({**_PAYLOAD, "claims": [*claims, two_paragraphs]})

    with pytest.raises(ValueError, match="holds text a payload cannot carry whole"):
        payload_of(text, source_path="note.md", spec=spec)
