"""`NoteWriter`: validate an authoring payload against one knowledge base and write it under its page_type's folder."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.authoring.contracts import AuthoringKnowledgeBase, NoteRefusedError, NoteWriter
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from pathlib import Path

TALK = {
    "frontmatter": {
        "id": "talk.agents-that-plan",
        "type": "talk",
        "title": "Agents that plan",
        "summary": "A talk on planning agents whose task list survives compaction.",
        "status": "active",
        "digest_of": "yt-abc123XYZ_-",
    },
    "sections": [
        {"heading": "TL;DR", "body": "Planning agents keep a task list the model owns."},
        {"heading": "Findings", "body": ""},
        {"heading": "Entities", "body": ""},
    ],
}


_REGISTRY_FACETS = """---
id: meta.vocabulary-facets
type: meta
title: "Vocabulary: facets"
summary: The claim-kind vocabulary the claim fixtures use.
status: verified
---

## `kind` — on claim blocks (single value)

| Value | Meaning |
|---|---|
| `observation` | Something observed |
"""


def _target(tmp_path: Path, *, folders: tuple[str, ...] = ()) -> AuthoringKnowledgeBase:
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True)
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text(_REGISTRY_FACETS, encoding="utf-8")
    if not folders:
        return AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=research_spec())
    base = research_spec()
    page_types = tuple(pt.model_copy(update={"folders": folders}) if pt.page_type == "talk" else pt for pt in base.page_types)
    spec = base.model_copy(update={"page_types": page_types})
    return AuthoringKnowledgeBase(name="research", collection="research", path=tmp_path, schema=spec)


def test_a_valid_payload_lands_under_the_kinds_folder(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(TALK)
    assert written.id == "talk.agents-that-plan"
    # The file name is the id minus its page_type prefix: the name a `[[agents-that-plan]]` link resolves.
    assert written.path == tmp_path / "Wiki" / "Talks" / "agents-that-plan.md"
    text = written.path.read_text(encoding="utf-8")
    assert "digest_of: yt-abc123XYZ_-" in text
    assert "## TL;DR" in text


def test_a_payload_that_breaks_the_grammar_is_refused_in_words(tmp_path: Path) -> None:
    bad = {**TALK, "frontmatter": {**TALK["frontmatter"], "id": "wrong.agents"}}
    with pytest.raises(NoteRefusedError, match=r"talk\."):
        NoteWriter(_target(tmp_path)).write(bad)
    assert not (tmp_path / "Wiki" / "Talks").exists()


def test_rewriting_the_same_id_replaces_the_file(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    again = {**TALK, "frontmatter": {**TALK["frontmatter"], "builds_on": ["[[paper.one]]"]}}
    written = writer.write(again)
    assert "builds_on" in written.path.read_text(encoding="utf-8")
    assert len(list((tmp_path / "Wiki" / "Talks").glob("*.md"))) == 1


def test_a_digest_of_another_source_is_never_replaced(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    first = writer.write(TALK)
    other = {**TALK, "frontmatter": {**TALK["frontmatter"], "digest_of": "yt-other"}}
    with pytest.raises(NoteRefusedError, match="yt-abc123XYZ_-"):
        writer.write(other)
    assert "digest_of: yt-abc123XYZ_-" in first.path.read_text(encoding="utf-8")


def test_digest_id_for_numbers_an_id_another_sources_digest_holds(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    assert writer.digest_id_for("talk", "talk.agents-that-plan", "yt-other") == "talk.agents-that-plan-2"
    assert writer.digest_id_for("talk", "talk.agents-that-plan", "yt-abc123XYZ_-") == "talk.agents-that-plan", "its own digest keeps its id"


def test_an_id_with_a_path_separator_is_refused(tmp_path: Path) -> None:
    bad = {**TALK, "frontmatter": {**TALK["frontmatter"], "id": "talk.foo/bar"}}
    with pytest.raises(NoteRefusedError, match="path separator"):
        NoteWriter(_target(tmp_path)).write(bad)
    assert not any(p for p in tmp_path.rglob("*.md") if "_meta" not in p.parts)


def test_an_id_with_a_dotdot_segment_is_refused(tmp_path: Path) -> None:
    bad = {**TALK, "frontmatter": {**TALK["frontmatter"], "id": "talk..plan"}}
    with pytest.raises(NoteRefusedError, match=r"\.\."):
        NoteWriter(_target(tmp_path)).write(bad)
    assert not any(p for p in tmp_path.rglob("*.md") if "_meta" not in p.parts)


def test_a_payload_missing_type_is_refused_in_words(tmp_path: Path) -> None:
    bad = {"frontmatter": {key: value for key, value in TALK["frontmatter"].items() if key != "type"}}
    with pytest.raises(NoteRefusedError, match="missing page type"):
        NoteWriter(_target(tmp_path)).write(bad)


def test_the_written_page_aliases_its_own_id(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(TALK)
    text = written.path.read_text(encoding="utf-8")
    # The id leads the aliases so a prefixed `[[talk.agents-that-plan]]` link resolves in Obsidian.
    assert "aliases:\n- talk.agents-that-plan" in text


def test_existing_aliases_keep_their_place_behind_the_id(tmp_path: Path) -> None:
    payload = {**TALK, "frontmatter": {**TALK["frontmatter"], "aliases": ["planning talk"]}}
    written = NoteWriter(_target(tmp_path)).write(payload)
    text = written.path.read_text(encoding="utf-8")
    assert "aliases:\n- talk.agents-that-plan\n- planning talk" in text


def test_an_id_already_aliased_is_not_doubled(tmp_path: Path) -> None:
    payload = {**TALK, "frontmatter": {**TALK["frontmatter"], "aliases": ["talk.agents-that-plan"]}}
    written = NoteWriter(_target(tmp_path)).write(payload)
    text = written.path.read_text(encoding="utf-8")
    assert text.count("talk.agents-that-plan") == 2  # the id line and the one alias line


def test_an_explicit_path_lands_under_a_declared_folder(tmp_path: Path) -> None:
    knowledge_base = _target(tmp_path, folders=("initiatives",))
    written = NoteWriter(knowledge_base).write(TALK, path="initiatives/2026-01-01-x/analysis.md")
    assert written.path == tmp_path / "initiatives" / "2026-01-01-x" / "analysis.md"
    assert written.path.is_file()


def test_an_explicit_path_outside_the_declared_folders_is_refused_naming_them(tmp_path: Path) -> None:
    knowledge_base = _target(tmp_path, folders=("initiatives",))
    with pytest.raises(NoteRefusedError, match="initiatives"):
        NoteWriter(knowledge_base).write(TALK, path="elsewhere/x.md")
    assert not (tmp_path / "elsewhere").exists()


def test_an_explicit_path_that_escapes_the_declared_folder_is_refused(tmp_path: Path) -> None:
    knowledge_base = _target(tmp_path, folders=("initiatives",))
    with pytest.raises(NoteRefusedError):
        NoteWriter(knowledge_base).write(TALK, path="../x.md")
    assert not (tmp_path.parent / "x.md").exists()


def test_an_explicit_path_not_ending_in_md_is_refused(tmp_path: Path) -> None:
    knowledge_base = _target(tmp_path, folders=("initiatives",))
    with pytest.raises(NoteRefusedError, match=r"\.md"):
        NoteWriter(knowledge_base).write(TALK, path="initiatives/x.txt")


def _follow_up(body: str) -> dict[str, object]:
    return {
        "frontmatter": {
            "id": "talk.follow-up",
            "type": "talk",
            "title": "Follow up",
            "summary": "A follow-up talk citing the first one.",
            "status": "active",
            "digest_of": "yt-def456ABC_-",
        },
        "sections": [{"heading": "TL;DR", "body": body}],
    }


def test_a_bare_page_id_that_resolves_becomes_a_wikilink(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("Builds on talk.agents-that-plan directly."))
    # The knowledge base is the file name (what stock Obsidian resolves), the display text keeps the id.
    assert "Builds on [[agents-that-plan|talk.agents-that-plan]] directly." in written.path.read_text(encoding="utf-8")
    assert any("talk.agents-that-plan" in warning for warning in written.warnings)


def test_a_bare_id_with_a_claim_anchor_is_wrapped_whole(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("See talk.agents-that-plan#^c2 for the claim."))
    assert "See [[agents-that-plan#^c2|talk.agents-that-plan#^c2]] for the claim." in written.path.read_text(encoding="utf-8")


def test_a_bare_id_that_resolves_nowhere_is_left_and_named(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(_follow_up("See talk.never-written here."))
    assert "See talk.never-written here." in written.path.read_text(encoding="utf-8")
    assert any("talk.never-written" in warning for warning in written.warnings)


def test_a_full_id_wikilink_gets_a_filename_target_keeping_the_id_on_display(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("See [[talk.agents-that-plan]] here."))
    assert "See [[agents-that-plan|talk.agents-that-plan]] here." in written.path.read_text(encoding="utf-8")


def test_a_full_id_wikilink_with_its_own_display_keeps_that_display(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("See [[talk.agents-that-plan#^c2|the claim]] here."))
    assert "See [[agents-that-plan#^c2|the claim]] here." in written.path.read_text(encoding="utf-8")


def test_a_filename_wikilink_is_untouched(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("See [[agents-that-plan]] here."))
    assert "See [[agents-that-plan]] here." in written.path.read_text(encoding="utf-8")


def test_ids_inside_code_stay_plain(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    body = "Run `studio fetch talk.agents-that-plan` first.\n\n```\nfetch(talk.agents-that-plan)\n```"
    written = writer.write(_follow_up(body))
    assert "[[" not in written.path.read_text(encoding="utf-8").split("## TL;DR")[1]


def test_a_heading_line_inside_a_body_is_never_rewritten(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    body = "Builds on talk.agents-that-plan directly.\n\n## After talk.agents-that-plan\n\nMore.\n\n### See [[talk.agents-that-plan]]\nStill more."
    written = writer.write(_follow_up(body))
    text = written.path.read_text(encoding="utf-8")
    assert "Builds on [[agents-that-plan|talk.agents-that-plan]] directly." in text
    assert "\n## After talk.agents-that-plan\n" in text
    assert "\n### See [[talk.agents-that-plan]]\n" in text


def test_mismatched_brackets_around_a_resolving_id_are_repaired(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("See [[talk.agents-that-plan] and [talk.agents-that-plan]] twice."))
    text = written.path.read_text(encoding="utf-8")
    assert "See [[agents-that-plan|talk.agents-that-plan]] and [[agents-that-plan|talk.agents-that-plan]] twice." in text


def test_a_markdown_link_with_an_id_as_text_is_untouched(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(TALK)
    written = writer.write(_follow_up("See [talk.agents-that-plan](https://example.com/x) upstream."))
    assert "See [talk.agents-that-plan](https://example.com/x) upstream." in written.path.read_text(encoding="utf-8")


def test_the_pages_own_id_counts_as_resolving(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(_follow_up("This page, talk.follow-up, cites itself."))
    assert "This page, [[follow-up|talk.follow-up]], cites itself." in written.path.read_text(encoding="utf-8")


FINDINGS_TALK = {
    "frontmatter": {
        "id": "talk.agents-that-plan",
        "type": "talk",
        "title": "Agents that plan",
        "summary": "A talk on planning agents whose task list survives compaction.",
        "status": "active",
        "digest_of": "yt-abc123XYZ_-",
    },
    "sections": [
        {"heading": "TL;DR", "body": "Planning agents keep a task list the model owns."},
        {
            "heading": "Findings",
            "body": (
                "- Task lists survive compaction. ^c1\n    - loc:: the talk\n    - kind:: observation\n"
                "- Plans transfer across models. ^c2\n    - loc:: the talk\n    - kind:: observation"
            ),
        },
        {"heading": "Entities", "body": ""},
    ],
}


def test_a_bare_anchor_after_a_claim_link_is_attributed_to_that_page(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(FINDINGS_TALK)
    written = writer.write(_follow_up("See [[agents-that-plan#^c1]] and also (^c2)."))
    assert "See [[agents-that-plan#^c1]] and also ([[agents-that-plan#^c2|^c2]])." in written.path.read_text(encoding="utf-8")


def test_a_bare_anchor_skips_line_links_that_define_nothing(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(FINDINGS_TALK)
    written = writer.write(_follow_up("Per [[agents-that-plan#^c1]], beats [[some-entity]] on the benchmark (^c2)."))
    text = written.path.read_text(encoding="utf-8")
    assert "beats [[some-entity]] on the benchmark ([[agents-that-plan#^c2|^c2]])." in text


def test_a_bare_anchor_the_named_page_never_defines_is_left_and_named(tmp_path: Path) -> None:
    writer = NoteWriter(_target(tmp_path))
    writer.write(FINDINGS_TALK)
    written = writer.write(_follow_up("See [[agents-that-plan#^c1]] and also (^c9)."))
    assert "and also (^c9)." in written.path.read_text(encoding="utf-8")
    assert any("^c9" in warning for warning in written.warnings)


def test_a_bare_anchor_with_no_page_on_the_line_is_left_and_named(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(_follow_up("The unsandboxing rate doubled (^c4)."))
    assert "The unsandboxing rate doubled (^c4)." in written.path.read_text(encoding="utf-8")
    assert any("^c4" in warning for warning in written.warnings)


def test_a_claim_definition_line_is_never_rewritten(tmp_path: Path) -> None:
    written = NoteWriter(_target(tmp_path)).write(FINDINGS_TALK)
    text = written.path.read_text(encoding="utf-8")
    assert "- Task lists survive compaction. ^c1" in text
    assert "- Plans transfer across models. ^c2" in text


def test_a_bare_anchor_the_page_itself_defines_links_to_its_own_stem(tmp_path: Path) -> None:
    payload = {
        **FINDINGS_TALK,
        "sections": [
            {"heading": "TL;DR", "body": "Plans transfer across models (^c2)."},
            *FINDINGS_TALK["sections"][1:],
        ],
    }
    written = NoteWriter(_target(tmp_path)).write(payload)
    assert "Plans transfer across models ([[agents-that-plan#^c2|^c2]])." in written.path.read_text(encoding="utf-8")


def test_a_declared_folder_above_the_knowledge_base_root_is_honored(tmp_path: Path) -> None:
    knowledge_base_root = tmp_path / "knowledge_base"
    knowledge_base = _target(knowledge_base_root, folders=("../sibling/adr",))
    written = NoteWriter(knowledge_base).write(TALK, path="../sibling/adr/0001-x.md")
    assert written.path == (knowledge_base_root / "../sibling/adr/0001-x.md").resolve()
    assert written.path.is_file()
