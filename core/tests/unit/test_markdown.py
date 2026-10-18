"""Frontmatter split/render: round-trip, ordering, and the no-frontmatter case."""

from __future__ import annotations

from core.markdown import render_frontmatter, split_frontmatter


def test_split_then_render_round_trips():
    data = {"id": "paper.lightrag-2025", "type": "paper", "year": 2025}
    body = "\n# Body\n\nSome text.\n"
    rendered = render_frontmatter(data, body)
    parsed_data, parsed_body = split_frontmatter(rendered)
    assert parsed_data == data
    assert parsed_body == body


def test_render_preserves_key_order():
    rendered = render_frontmatter({"id": "x", "type": "paper", "title": "T"}, "body")
    fence = "---\n"
    block = rendered.split(fence, 2)[1]
    assert block.index("id:") < block.index("type:") < block.index("title:")


def test_render_is_idempotent_through_a_reparse_cycle():
    data = {"id": "x", "aliases": ["a", "b"]}
    once = render_frontmatter(data, "body\n")
    twice = render_frontmatter(*split_frontmatter(once))
    assert once == twice


def test_split_returns_empty_dict_when_there_is_no_frontmatter():
    text = "# Just a heading\n\nNo frontmatter here.\n"
    data, body = split_frontmatter(text)
    assert data == {}
    assert body == text


def test_split_returns_empty_dict_when_yaml_is_not_a_mapping():
    text = "---\n- just\n- a\n- list\n---\nbody\n"
    data, body = split_frontmatter(text)
    assert data == {}
    assert body == text


def test_render_handles_unicode_without_escaping():
    rendered = render_frontmatter({"title": "Köln — café"}, "body")
    assert "Köln — café" in rendered


def test_render_keeps_long_scalars_on_one_line():
    # PyYAML's default 80-col wrap folds a long scalar across indented continuation lines,
    # which breaks a line-wise reader (an ingest skill deduping on a stamped source_path);
    # width=inf keeps it on one line, and the value still round-trips.
    long_path = "notes/{OpenAIProduct} Weekly Planning - Review and Retrospective Session Notes (2024-08-26).md"
    rendered = render_frontmatter({"source_path": long_path}, "body\n")
    block = rendered.split("---\n", 2)[1]
    assert block.strip().count("\n") == 0  # not wrapped onto continuation lines
    data, _ = split_frontmatter(rendered)
    assert data["source_path"] == long_path
