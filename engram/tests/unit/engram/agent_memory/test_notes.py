"""Path and markdown derivation for memory notes: deterministic, invertible, teaching errors."""

import pytest

from engram.agent_memory.domain import SOURCES, NotePieces, derive, is_index, page_id_of, path_of, render_index


def test_page_id_round_trips() -> None:
    path = "global/main/world/postgres-port.md"
    assert page_id_of(path) == "mem.global.main.world.postgres-port"
    assert path_of(page_id_of(path)) == path


def test_index_paths_are_recognized() -> None:
    assert is_index("global/main/MEMORY.md")
    assert not is_index("global/main/world/memory-md.md")


def test_derive_reads_heading_hook_and_body() -> None:
    content = "# Local Postgres port\nThe local Postgres answers on 5433.\n\nMore detail here.\n"
    pieces = derive("global/main/world/postgres-port.md", content)
    assert pieces == NotePieces(
        page_id="mem.global.main.world.postgres-port",
        category="world",
        title="Local Postgres port",
        summary="The local Postgres answers on 5433.",
        source=None,
        body=content,
    )


def test_derive_reads_optional_frontmatter() -> None:
    content = "---\nsource: owner-note\nsummary: Wraps at 140.\n---\n# Markdown width\nBody.\n"
    pieces = derive("global/main/project/markdown-width.md", content)
    assert pieces.source == "owner-note"
    assert pieces.summary == "Wraps at 140."
    assert pieces.category == "project"


def test_a_bad_source_teaches() -> None:
    content = "---\nsource: dream-journal\n---\n# x\nx\n"
    with pytest.raises(ValueError, match="owner-note, session, dream"):
        derive("global/main/world/x.md", content)


@pytest.mark.parametrize("source", SOURCES)
def test_each_valid_source_passes(source: str) -> None:
    content = f"---\nsource: {source}\n---\n# x\nx\n"
    pieces = derive("global/main/world/x.md", content)
    assert pieces.source == source


def test_derive_without_heading_falls_back_to_the_slug() -> None:
    pieces = derive("global/main/reference/jira-board.md", "https://example.atlassian.net/board\n")
    assert pieces.title == "jira-board"
    assert pieces.summary == "https://example.atlassian.net/board"


def test_a_bad_category_teaches() -> None:
    with pytest.raises(ValueError, match="world, project, reference"):
        derive("global/main/gossip/who-said-what.md", "# x\nx\n")


def test_a_dotted_slug_teaches() -> None:
    with pytest.raises(ValueError, match="dots"):
        derive("global/main/world/v1.2-notes.md", "# x\nx\n")


def test_a_dotted_scope_segment_teaches() -> None:
    with pytest.raises(ValueError, match="dots"):
        derive("glo.bal/main/world/x.md", "# x\nx\n")


def test_a_long_summary_is_truncated_to_the_limit() -> None:
    content = "# Title\n" + ("x" * 250) + "\n"
    pieces = derive("global/main/world/long-summary.md", content)
    assert len(pieces.summary) == 200


def test_render_index_is_one_line_per_note() -> None:
    text = render_index([("global/main/world/postgres-port.md", "Local Postgres port", "Answers on 5433.")])
    assert text == "- [Local Postgres port](global/main/world/postgres-port.md): Answers on 5433."
