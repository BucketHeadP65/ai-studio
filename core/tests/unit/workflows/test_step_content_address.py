"""A reusable step's content address: equal steps share it however they were written, and any change of content moves it."""

from __future__ import annotations

import hashlib
import json

import yaml

from core.ai.workflows.contracts import Briefing, Program, Shape, canonical_step_yaml, load_step_document, step_content_address
from core.ai.workflows.domain.vocabulary import SameAs

INSTRUCTIONS = "Read the source and write one digest note about it, quoting what it claims."


def _digest(instructions: str = INSTRUCTIONS) -> Briefing:
    return Briefing(
        name="digest",
        instructions=instructions,
        inputs={"source": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )


def _step_file(extra: str) -> str:
    """A step file carrying the digest briefing, with `extra` frontmatter lines inside the step."""
    return (
        "---\n"
        "title: Digest one source\n"
        "summary: Writes one digest note.\n"
        "version: 3\n"
        "step:\n"
        "  name: digest\n"
        f"  instructions: {INSTRUCTIONS}\n"
        "  inputs:\n"
        "    source: {json_schema: {type: string}}\n"
        "  output: {json_schema: {type: string}}\n"
        f"{extra}"
        "---\n"
        "\n"
        "Use it on one source.\n"
    )


def test_two_equal_briefings_share_a_content_address_and_one_changed_word_moves_it() -> None:
    assert step_content_address(_digest()) == step_content_address(_digest())
    assert step_content_address(_digest()) != step_content_address(_digest(INSTRUCTIONS.replace("one digest", "a digest")))


def test_a_field_written_at_its_default_keeps_the_step_content_address() -> None:
    bare = Briefing(name="digest", instructions=INSTRUCTIONS, output=Shape(json_schema={"type": "string"}))
    explicit = Briefing(
        name="digest", instructions=INSTRUCTIONS, output=Shape(json_schema={"type": "string"}), inputs={}, tools=(), skills=(), tier=None
    )
    assert step_content_address(bare) == step_content_address(explicit)


def test_a_nested_field_written_at_its_default_keeps_the_step_content_address() -> None:
    bare = Briefing(name="digest", instructions=INSTRUCTIONS, output=SameAs(like="find"))
    explicit = Briefing(name="digest", instructions=INSTRUCTIONS, output=SameAs(like="find", element=False, many=False))
    assert step_content_address(bare) == step_content_address(explicit)


def test_a_step_file_whose_only_change_writes_fields_at_their_defaults_keeps_its_content_address() -> None:
    bare = load_step_document(_step_file(""))
    explicit = load_step_document(_step_file("  tools: []\n  skills: []\n  tier: null\n  budget: null\n"))

    assert explicit.step == bare.step
    assert step_content_address(explicit.step) == step_content_address(bare.step)


def test_a_program_and_a_briefing_with_the_same_name_and_slots_have_different_content_addresses() -> None:
    briefing = _digest()
    program = Program(name="digest", source=INSTRUCTIONS, inputs=briefing.inputs, output=briefing.output)
    assert step_content_address(program) != step_content_address(briefing)


def test_the_content_address_is_the_sha256_of_the_canonical_json_with_defaults_left_out() -> None:
    canonical = {
        "inputs": {"source": {"json_schema": {"type": "string"}}},
        "instructions": INSTRUCTIONS,
        "name": "digest",
        "output": {"json_schema": {"type": "string"}},
    }
    expected = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert step_content_address(_digest()) == expected


def test_the_canonical_yaml_is_the_hashed_document_and_ignores_fields_written_at_their_defaults() -> None:
    explicit = Briefing(name="digest", instructions=INSTRUCTIONS, inputs=_digest().inputs, output=_digest().output, tools=(), skills=())

    text = canonical_step_yaml(explicit)

    assert text == canonical_step_yaml(_digest())
    assert yaml.safe_load(text) == _digest().model_dump(mode="json", exclude_defaults=True)
    assert [line.split(":")[0] for line in text.splitlines() if not line.startswith(" ")] == ["inputs", "instructions", "name", "output"]


def test_the_canonical_yaml_writes_multi_line_text_one_line_per_line() -> None:
    lines = [INSTRUCTIONS, "Quote every claim you keep.", "Name the source in the first line."]
    step = _digest("\n".join(lines))

    text = canonical_step_yaml(step)

    assert yaml.safe_load(text)["instructions"] == "\n".join(lines)
    assert all(any(row.strip() == line for row in text.splitlines()) for line in lines)
