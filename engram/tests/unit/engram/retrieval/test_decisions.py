import re

from pathlib import Path

import pytest

from engram.kb import contracts as kb
from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec
from engram.kb.domain.schema import DecisionSpec, Outcome
from engram.retrieval.decisions import _decision_reason_line, decide_page, list_decisions
from engram.testing.specs import research_spec

FLAG = """---
id: flag.try-planning-reminders
type: flag
title: Try planning reminders
summary: A live task list re-injected every turn.
status: pending
flag_kind: idea
about: ["[[talk.agents-that-plan]]"]
---

## Why

Because compaction drops the plan.

## Proposal

Mount the planning capability.

## Evidence

talk.agents-that-plan#findings
"""

MALFORMED = "# Not a note\n\nThis file has no frontmatter fence at all.\n"


def _knowledge_base(tmp_path: Path) -> Path:
    folder = tmp_path / "Wiki" / "Flags"
    folder.mkdir(parents=True)
    (folder / "flag.try-planning-reminders.md").write_text(FLAG, encoding="utf-8")
    return tmp_path


def _two_page_type_spec() -> KnowledgeBaseSchema:
    """A minimal spec with one decision page type (flag) and one plain-status page_type (note) with no decision block."""
    return KnowledgeBaseSchema(
        slug="test",
        page_types=(
            PageTypeSpec(
                page_type="flag",
                id_prefix="flag.",
                folder="Wiki/Flags",
                status_values=("pending", "accepted"),
                decision=DecisionSpec(open="pending", outcomes=(Outcome(status="accepted"),)),
            ),
            PageTypeSpec(
                page_type="note",
                id_prefix="note.",
                folder="Wiki/Notes",
                status_values=("draft", "final"),
            ),
        ),
    )


_MINIMAL_FLAG = """---
id: flag.minimal
type: flag
title: Minimal flag
status: pending
---

Plain body text.
"""

_MINIMAL_NOTE = """---
id: note.draft-idea
type: note
title: Draft idea
status: draft
---

Plain body text.
"""


def test_list_decisions_reads_the_kind_folder_at_its_open_status(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    listing = list_decisions(knowledge_base, research_spec())
    assert [row.id for row in listing.rows] == ["flag.try-planning-reminders"]
    row = listing.rows[0]
    assert row.page_type == "flag"
    assert row.status == "pending"
    assert row.links["about"] == ("talk.agents-that-plan",)
    assert row.links["resolved_by"] == ()
    assert listing.skipped == ()


def test_list_decisions_excludes_a_page_already_past_its_open_status(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    path.write_text(FLAG.replace("status: pending", "status: accepted"), encoding="utf-8")

    listing = list_decisions(knowledge_base, research_spec())

    assert listing.rows == ()


def test_list_decisions_skips_a_malformed_file_and_reports_it(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    (knowledge_base / "Wiki" / "Flags" / "flag.broken.md").write_text(MALFORMED, encoding="utf-8")

    listing = list_decisions(knowledge_base, research_spec())

    assert [row.id for row in listing.rows] == ["flag.try-planning-reminders"]
    assert listing.skipped == ("flag.broken.md",)


def test_list_decisions_skips_a_file_that_is_not_utf8_and_reports_it(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    (knowledge_base / "Wiki" / "Flags" / "flag.binary.md").write_bytes(b"---\nid: flag.binary\n\xff\xfe not text at all\n---\n")

    listing = list_decisions(knowledge_base, research_spec())

    assert [row.id for row in listing.rows] == ["flag.try-planning-reminders"]
    assert listing.skipped == ("flag.binary.md",)


def test_list_decisions_only_lists_declared_decision_page_types(tmp_path: Path) -> None:
    spec = _two_page_type_spec()
    knowledge_base = tmp_path
    flags = knowledge_base / "Wiki" / "Flags"
    flags.mkdir(parents=True)
    (flags / "flag.minimal.md").write_text(_MINIMAL_FLAG, encoding="utf-8")
    notes = knowledge_base / "Wiki" / "Notes"
    notes.mkdir(parents=True)
    (notes / "note.draft-idea.md").write_text(_MINIMAL_NOTE, encoding="utf-8")

    listing = list_decisions(knowledge_base, spec)

    assert [row.id for row in listing.rows] == ["flag.minimal"]


def test_decide_page_moves_a_page_of_a_kind_with_no_decision_block(tmp_path: Path) -> None:
    spec = _two_page_type_spec()
    notes = tmp_path / "Wiki" / "Notes"
    notes.mkdir(parents=True)
    (notes / "note.draft-idea.md").write_text(_MINIMAL_NOTE, encoding="utf-8")

    row = decide_page(tmp_path, spec, "note.draft-idea", "final")

    assert row.status == "final"
    assert row.page_type == "note"


def test_decide_page_rewrites_only_the_status_line(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted")
    assert row.status == "accepted"

    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")
    assert text == FLAG.replace("status: pending", "status: accepted", 1)

    reparsed = kb.parse(text, source_path=path.name, spec=research_spec())
    assert reparsed.frontmatter.status == "accepted"


def test_decide_page_refuses_when_the_status_line_cannot_be_found(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    # A trailing comment still parses as `status: pending` through the note grammar's YAML
    # frontmatter (so the row lists normally), but the raw-text rewrite's line pattern requires
    # the whole line to be `status: <value>` with nothing after it, so it finds nothing to rewrite.
    text = path.read_text(encoding="utf-8").replace("status: pending", "status: pending  # queued for review", 1)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(LookupError, match="status"):
        decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted")

    assert path.read_text(encoding="utf-8") == text, "a refused rewrite must never touch the file"


def test_an_unknown_page_is_refused_by_id(tmp_path: Path) -> None:
    with pytest.raises(LookupError, match=re.escape("flag.missing")):
        decide_page(_knowledge_base(tmp_path), research_spec(), "flag.missing", "dismissed")


def test_a_malformed_page_is_refused_the_same_way_as_a_missing_one(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    (knowledge_base / "Wiki" / "Flags" / "flag.broken.md").write_text(MALFORMED, encoding="utf-8")

    with pytest.raises(LookupError, match=re.escape("flag.broken")):
        decide_page(knowledge_base, research_spec(), "flag.broken", "accepted")


def test_decide_page_refuses_a_status_outside_the_kind_status_set(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    with pytest.raises(ValueError, match="not-a-status"):
        decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "not-a-status")


def test_decide_page_refuses_a_link_field_outside_the_kind_edge_fields(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    with pytest.raises(ValueError, match="not_a_field"):
        decide_page(
            knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", links={"not_a_field": ("q.x",)}
        )


def test_decide_page_accepts_completed(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "completed")
    assert row.status == "completed"


def test_decide_page_with_a_reason_inserts_a_decision_reason_line_after_status(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="landed in the sprint")

    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    status_idx = next(i for i, line in enumerate(lines) if line == "status: accepted")
    assert lines[status_idx + 1] == "decision_reason: landed in the sprint"

    reparsed = kb.parse(text, source_path=path.name, spec=research_spec())
    assert reparsed.frontmatter.model_extra is not None
    assert reparsed.frontmatter.model_extra["decision_reason"] == "landed in the sprint"


def test_decide_page_with_a_reason_replaces_an_existing_decision_reason_line_in_place(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="first reason")
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "dismissed", reason="second reason")

    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")
    assert text.count("decision_reason:") == 1
    assert "decision_reason: second reason" in text
    assert "first reason" not in text


def test_decide_page_with_no_reason_leaves_an_existing_decision_reason_line_untouched(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="keep me")

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "dismissed")

    assert row.status == "dismissed"
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    assert "decision_reason: keep me" in path.read_text(encoding="utf-8")


def test_decide_page_quotes_a_reason_whose_plain_form_would_change_meaning(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="landed in PR: fix bug")

    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")

    reparsed = kb.parse(text, source_path=path.name, spec=research_spec())
    assert reparsed.frontmatter.model_extra is not None
    assert reparsed.frontmatter.model_extra["decision_reason"] == "landed in PR: fix bug"


def test_decide_page_replacing_a_reason_tolerates_a_backslash_digit_sequence(tmp_path: Path) -> None:
    r"""A rendered reason containing e.g. `\1` must not be read as a regex backreference on the replace-in-place branch."""
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="first reason")

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "dismissed", reason="see \\1 in the diff")

    assert row.status == "dismissed"
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")
    assert text.count("decision_reason:") == 1
    assert "decision_reason: see \\1 in the diff" in text

    reparsed = kb.parse(text, source_path=path.name, spec=research_spec())
    assert reparsed.frontmatter.model_extra is not None
    assert reparsed.frontmatter.model_extra["decision_reason"] == "see \\1 in the diff"


def test_decide_page_normalizes_newlines_in_a_reason_to_single_spaces(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(
        knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="landed in the sprint\nsecond line"
    )

    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    status_idx = next(i for i, line in enumerate(lines) if line == "status: accepted")
    assert lines[status_idx + 1] == "decision_reason: landed in the sprint second line"

    reparsed = kb.parse(text, source_path=path.name, spec=research_spec())
    assert reparsed.frontmatter.model_extra is not None
    assert reparsed.frontmatter.model_extra["decision_reason"] == "landed in the sprint second line"


def test_decide_page_replacing_a_reason_swallows_a_folded_continuation_line(tmp_path: Path) -> None:
    """A reason folded across an indented continuation line must not survive a later decide.

    Whether from our own long-reason fold before this fix, or another renderer's plain-scalar wrap,
    the whole entry is replaced, not just its own first physical line, and the following
    frontmatter key is left standing.
    """
    knowledge_base = _knowledge_base(tmp_path)
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    folded = "status: pending\ndecision_reason: first part\n  second part\nflag_kind: idea"
    text = path.read_text(encoding="utf-8").replace("status: pending\nflag_kind: idea", folded)
    path.write_text(text, encoding="utf-8")

    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason="new reason")

    head, _, _ = path.read_text(encoding="utf-8").partition("\n---\n")
    reason_lines = [line for line in head.splitlines() if line.startswith("decision_reason:")]
    assert reason_lines == [_decision_reason_line("new reason")]
    assert "second part" not in head
    assert "flag_kind: idea" in head


def test_decide_page_with_a_reason_over_eighty_characters_stays_on_one_physical_line(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    long_reason = "a reason written past PyYAML's own eighty-column default fold width for a plain scalar value"
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"

    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", reason=long_reason)

    head, _, _ = path.read_text(encoding="utf-8").partition("\n---\n")
    reason_lines = [line for line in head.splitlines() if line.startswith("decision_reason:")]
    assert reason_lines == [f"decision_reason: {long_reason}"]

    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "dismissed", reason="short again")

    head, _, _ = path.read_text(encoding="utf-8").partition("\n---\n")
    assert head.count("decision_reason:") == 1
    assert "decision_reason: short again" in head
    assert long_reason not in head


def test_decide_page_writes_a_link_field_and_reads_it_back(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    row = decide_page(
        knowledge_base,
        research_spec(),
        "flag.try-planning-reminders",
        "completed",
        links={"resolved_by": ("q.planning", "[[pb.reminders]]")},
    )
    assert row.status == "completed"
    assert row.links["resolved_by"] == ("q.planning", "pb.reminders")

    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    text = path.read_text(encoding="utf-8")
    assert 'resolved_by: ["[[q.planning]]", "[[pb.reminders]]"]' in text
    assert text.count("resolved_by:") == 1
    # completed is past the page_type's open status, so it no longer lists as an open decision
    assert list_decisions(knowledge_base, research_spec()).rows == ()


def test_decide_page_replaces_an_existing_link_block_list(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    path = knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md"
    block = "status: accepted\nresolved_by:\n  - '[[q.old]]'\n  - '[[q.older]]'\n"
    path.write_text(FLAG.replace("status: pending\n", block), encoding="utf-8")

    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "completed", links={"resolved_by": ("q.new",)})

    text = path.read_text(encoding="utf-8")
    assert 'resolved_by: ["[[q.new]]"]' in text
    assert "q.old" not in text
    assert text.count("resolved_by:") == 1
    assert 'about: ["[[talk.agents-that-plan]]"]' in text


def test_decide_page_with_an_empty_status_keeps_the_status_and_the_reason_and_replaces_only_the_named_links(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", links={"resolved_by": ("q.old",)}, reason="keep me")

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "", links={"resolved_by": ("q.new",)})

    assert row.status == "accepted"
    assert row.links["resolved_by"] == ("q.new",)
    text = (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")
    assert "\nstatus: accepted\n" in text
    assert "decision_reason: keep me" in text
    assert 'resolved_by: ["[[q.new]]"]' in text
    assert "q.old" not in text
    assert 'about: ["[[talk.agents-that-plan]]"]' in text


def test_decide_page_with_an_empty_status_and_a_reason_inserts_the_reason_after_the_unchanged_status_line(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "", reason="linked by a step")

    assert row.status == "pending"
    text = (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")
    assert "\nstatus: pending\ndecision_reason: linked by a step\n" in text


def test_decide_page_with_add_merges_the_ids_into_the_field_existing_first_without_repeats(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", links={"resolved_by": ("q.old", "q.keep")})

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "", links={"resolved_by": ("q.keep", "[[q.new]]")}, add=True)

    assert row.links["resolved_by"] == ("q.old", "q.keep", "q.new")
    text = (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")
    assert 'resolved_by: ["[[q.old]]", "[[q.keep]]", "[[q.new]]"]' in text
    assert text.count("resolved_by:") == 1


def test_decide_page_with_add_leaves_the_fields_it_does_not_name_alone(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "", links={"resolved_by": ("q.new",)}, add=True)

    assert row.links["about"] == ("talk.agents-that-plan",)
    assert row.links["resolved_by"] == ("q.new",)
    text = (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")
    assert 'about: ["[[talk.agents-that-plan]]"]' in text
    assert 'resolved_by: ["[[q.new]]"]' in text


def test_decide_page_without_add_replaces_the_named_field(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", links={"resolved_by": ("q.old",)})

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "", links={"resolved_by": ("q.new",)}, add=False)

    assert row.links["resolved_by"] == ("q.new",)
    assert "q.old" not in (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")


def test_decide_page_without_links_keeps_the_existing_links(tmp_path: Path) -> None:
    knowledge_base = _knowledge_base(tmp_path)
    decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "accepted", links={"resolved_by": ("q.planning",)})

    row = decide_page(knowledge_base, research_spec(), "flag.try-planning-reminders", "completed")

    assert row.links["resolved_by"] == ("q.planning",)
    text = (knowledge_base / "Wiki" / "Flags" / "flag.try-planning-reminders.md").read_text(encoding="utf-8")
    assert 'resolved_by: ["[[q.planning]]"]' in text
