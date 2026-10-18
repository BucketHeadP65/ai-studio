"""The note sidecar: roundtrip, the anchor ladder, and the agent's rendered fence."""

from __future__ import annotations

import os

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engram.kb.contracts import Note, NoteAnchor, NoteSheet, Reply, add_reply, set_status

from harness.features.projects import service
from harness.features.projects.domain import add_note, annotate_read, remove_note, replace_note, replace_reply, resolve_anchor
from harness.features.projects.infrastructure.note_sidecars import annotated_files, has_sidecar, load_sheet, save_sheet, sidecar_path


def _anchor(exact: str, prefix: str = "", suffix: str = "") -> NoteAnchor:
    return NoteAnchor(exact=exact, prefix=prefix, suffix=suffix)


def _note(anchor: NoteAnchor, text: str = "a note", *, archived: bool = False) -> Note:
    return Note(id="00000000-0000-0000-0000-000000000000", anchor=anchor, text=text, created_at=datetime.now(UTC), archived=archived)


class TestSidecarRoundtrip:
    def test_an_absent_sidecar_is_an_empty_sheet(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("body", encoding="utf-8")

        sheet = load_sheet(tmp_path, target)

        assert sheet == NoteSheet()

    def test_save_then_load_roundtrips_every_field(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("body", encoding="utf-8")
        note = _note(_anchor("body", prefix="pre", suffix="suf"), text="hello", archived=True)
        sheet = NoteSheet(notes=(note,))

        save_sheet(tmp_path, target, sheet)
        loaded = load_sheet(tmp_path, target)

        assert loaded == sheet
        assert sidecar_path(tmp_path, target) == tmp_path / ".agent-threads" / "note.md.threads.json"

    def test_an_unreadable_sidecar_answers_an_empty_sheet(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("body", encoding="utf-8")
        sidecar = sidecar_path(tmp_path, target)
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text("not json at all {{{", encoding="utf-8")

        sheet = load_sheet(tmp_path, target)

        assert sheet == NoteSheet()

    def test_created_at_serializes_with_a_timezone_offset(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        note = _note(_anchor("x"))
        sheet = NoteSheet(notes=(note,))

        save_sheet(tmp_path, target, sheet)

        raw = sidecar_path(tmp_path, target).read_text(encoding="utf-8")
        assert "+00:00" in raw or "Z" in raw

    def test_a_sidecar_without_replies_or_status_fields_still_loads(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        raw = (
            '{"version": 1, "notes": [{"id": "n1", "anchor": {"exact": "x", "prefix": "", "suffix": ""}, '
            '"text": "hi", "created_at": "2026-01-01T00:00:00+00:00", "archived": false}]}'
        )
        sidecar = sidecar_path(tmp_path, target)
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(raw, encoding="utf-8")

        sheet = load_sheet(tmp_path, target)

        [note] = sheet.notes
        assert note.replies == ()
        assert note.status == "open"


class TestSidecarHome:
    """The `.agent-threads/` home: path mapping, reads, writes."""

    def test_sidecar_path_for_a_root_level_file(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"

        assert sidecar_path(tmp_path, target) == tmp_path / ".agent-threads" / "note.md.threads.json"

    def test_sidecar_path_for_a_nested_file(self, tmp_path: Path) -> None:
        target = tmp_path / "docs" / "adr" / "x.md"

        assert sidecar_path(tmp_path, target) == tmp_path / ".agent-threads" / "docs" / "adr" / "x.md.threads.json"

    def test_sidecar_path_resolves_a_symlinked_root_before_comparing(self, tmp_path: Path) -> None:
        """A symlinked project root must not make an actual descendant look like it falls outside `root`.

        `root` arrives from the registry unresolved; `file` arrives realpath'd.
        """
        real_root = tmp_path / "real"
        real_root.mkdir()
        link_root = tmp_path / "link"
        link_root.symlink_to(real_root)
        target = Path(os.path.realpath(link_root / "note.md"))

        sidecar = sidecar_path(link_root, target)

        assert sidecar == real_root / ".agent-threads" / "note.md.threads.json"

    def test_load_and_save_sheet_work_through_a_symlinked_project_root(self, tmp_path: Path) -> None:
        real_root = tmp_path / "real"
        real_root.mkdir()
        link_root = tmp_path / "link"
        link_root.symlink_to(real_root)
        target = Path(os.path.realpath(link_root / "note.md"))
        target.write_text("body", encoding="utf-8")
        note = _note(_anchor("body"))

        save_sheet(link_root, target, NoteSheet(notes=(note,)))
        loaded = load_sheet(link_root, target)

        assert loaded.notes == (note,)

    def test_has_sidecar_is_true_once_the_centralized_sidecar_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("body", encoding="utf-8")

        assert has_sidecar(tmp_path, target) is False

        save_sheet(tmp_path, target, NoteSheet(notes=(_note(_anchor("body")),)))
        assert has_sidecar(tmp_path, target) is True

    def test_annotated_files_and_summarize_see_every_sidecar_under_the_home(self, tmp_path: Path) -> None:
        first = tmp_path / "a-first.md"
        first.write_text("one", encoding="utf-8")
        save_sheet(tmp_path, first, NoteSheet(notes=(_note(_anchor("one")),)))

        second = tmp_path / "b-second.md"
        second.write_text("two", encoding="utf-8")
        save_sheet(tmp_path, second, NoteSheet(notes=(_note(_anchor("two")),)))

        found = annotated_files(tmp_path)
        assert [rel for _file, rel in found] == ["a-first.md", "b-second.md"]

        summaries = service.summarize(tmp_path)
        assert [summary.path for summary in summaries] == ["a-first.md", "b-second.md"]
        assert [summary.open_notes for summary in summaries] == [1, 1]

    def test_a_sidecar_beside_the_file_is_not_read(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("body", encoding="utf-8")
        beside = target.with_name("note.md.threads.json")
        beside.write_text(NoteSheet(notes=(_note(_anchor("body")),)).model_dump_json(), encoding="utf-8")

        assert has_sidecar(tmp_path, target) is False
        assert load_sheet(tmp_path, target) == NoteSheet()
        assert annotated_files(tmp_path) == []
        assert beside.is_file()


class TestResolveAnchorLadder:
    def test_a_unique_exact_match_resolves(self) -> None:
        text = "one two three\nfour five six"

        hit = resolve_anchor(text, _anchor("four five"))

        assert hit is not None
        assert hit.line == 2
        assert text[hit.offset : hit.offset + len("four five")] == "four five"
        assert hit.end == hit.offset + len("four five")
        assert text[hit.offset : hit.end] == "four five"

    def test_repeated_exact_with_distinct_prefixes_picks_the_right_one(self) -> None:
        text = "alpha target beta\nomega target gamma"
        first = _anchor("target", prefix="alpha ", suffix=" beta")
        second = _anchor("target", prefix="omega ", suffix=" gamma")

        first_hit = resolve_anchor(text, first)
        second_hit = resolve_anchor(text, second)

        assert first_hit is not None
        assert second_hit is not None
        assert first_hit.line == 1
        assert second_hit.line == 2
        assert first_hit.offset != second_hit.offset
        assert text[first_hit.offset : first_hit.end] == "target"
        assert text[second_hit.offset : second_hit.end] == "target"

    def test_a_rewrapped_paragraph_survives_the_normalized_pass(self) -> None:
        anchor = _anchor("the quick brown fox jumps over the lazy dog")
        # The paragraph got rewrapped onto two lines -- the exact string no longer appears
        # verbatim, but the same words in the same order do.
        text = "intro line\nthe quick brown fox\njumps over the lazy dog\noutro line"

        hit = resolve_anchor(text, anchor)

        assert hit is not None
        assert hit.line == 2

    def test_an_anchor_with_no_surviving_text_is_orphaned(self) -> None:
        text = "completely different content"

        hit = resolve_anchor(text, _anchor("text that is nowhere here"))

        assert hit is None

    def test_ambiguous_repeats_with_no_disambiguating_context_are_orphaned(self) -> None:
        text = "repeat repeat repeat"

        hit = resolve_anchor(text, _anchor("repeat"))

        assert hit is None

    def test_a_repeat_narrows_by_prefix_alone_when_the_suffix_changed(self) -> None:
        text = "alpha repeat beta and gamma repeat delta"

        hit = resolve_anchor(text, _anchor("repeat", prefix="gamma ", suffix=" rewritten"))

        assert hit is not None
        assert hit.offset == text.rindex("repeat")

    def test_a_repeat_narrows_by_suffix_alone_when_the_prefix_changed(self) -> None:
        text = "alpha repeat beta and gamma repeat delta"

        hit = resolve_anchor(text, _anchor("repeat", prefix="rewritten ", suffix=" delta"))

        assert hit is not None
        assert hit.offset == text.rindex("repeat")

    def test_a_heading_note_survives_an_edit_that_rewrote_its_prefix(self) -> None:
        # The TOC repeats the heading text twice (link target and label); the note was taken on
        # the heading itself, then a paragraph landed at the end of the section above it.
        text = (
            "- [[#R3. Weekly summary|R3. Weekly summary]]\n\n"
            "Old tail.\nAn answer was inserted here.\n\n## R3. Weekly summary\n\nSource: the weekly log."
        )
        anchor = _anchor("R3. Weekly summary", prefix="Old tail.\n\n## ", suffix="\n\nSource: the weekly log.")

        hit = resolve_anchor(text, anchor)

        assert hit is not None
        assert hit.offset == text.rindex("R3. Weekly summary")

    def test_a_heading_note_survives_both_edges_changing(self) -> None:
        text = "- [[#R3. Weekly summary|R3. Weekly summary]]\n\nNew tail.\n\n## R3. Weekly summary\n\nA rewritten body."
        anchor = _anchor("R3. Weekly summary", prefix="Old tail.\n\n## ", suffix="\n\nSource: the weekly log.")

        hit = resolve_anchor(text, anchor)

        assert hit is not None
        assert hit.offset == text.rindex("R3. Weekly summary")

    def test_a_body_note_never_jumps_to_a_heading(self) -> None:
        text = "## target\n\nbody target here and body target there"

        hit = resolve_anchor(text, _anchor("target", prefix="gone ", suffix=" gone"))

        assert hit is None

    def test_two_headings_repeating_the_noted_text_stay_orphaned(self) -> None:
        text = "intro\n\n## Notes\n\nfirst\n\n## Notes\n\nsecond"

        hit = resolve_anchor(text, _anchor("Notes", prefix="gone\n\n## ", suffix="\n\ngone"))

        assert hit is None


class TestAnnotateRead:
    def test_a_resolved_note_renders_its_line_and_text(self) -> None:
        text = "one\ntwo\nthree"
        sheet = NoteSheet(notes=(_note(_anchor("two"), text="watch this"),))

        rendered = annotate_read("RESULT", text, sheet)

        assert rendered.startswith("RESULT\n\n--- owner notes (annotations, not part of the file) ---\n")
        assert '[n1] line 2, status open, on "two": watch this' in rendered

    def test_an_orphaned_note_still_renders_flagged(self) -> None:
        text = "nothing matches"
        sheet = NoteSheet(notes=(_note(_anchor("gone"), text="lost"),))

        rendered = annotate_read("RESULT", text, sheet)

        assert '[n1] orphaned, status open, on "gone": lost' in rendered

    def test_all_archived_notes_render_the_input_byte_identical(self) -> None:
        text = "one\ntwo\nthree"
        sheet = NoteSheet(notes=(_note(_anchor("two"), archived=True), _note(_anchor("one"), archived=True)))

        rendered = annotate_read("RESULT", text, sheet)

        assert rendered == "RESULT"

    def test_archived_notes_are_skipped_while_active_ones_still_render(self) -> None:
        text = "one\ntwo\nthree"
        sheet = NoteSheet(
            notes=(_note(_anchor("one"), text="hidden", archived=True), _note(_anchor("three"), text="shown", archived=False))
        )

        rendered = annotate_read("RESULT", text, sheet)

        assert "hidden" not in rendered
        assert '[n1] line 3, status open, on "three": shown' in rendered

    def test_a_resolved_thread_renders_the_input_byte_identical(self) -> None:
        text = "one\ntwo\nthree"
        note = _note(_anchor("two"), text="watch this").model_copy(update={"status": "resolved"})
        sheet = NoteSheet(notes=(note,))

        rendered = annotate_read("RESULT", text, sheet)

        assert rendered == "RESULT"

    def test_a_threads_replies_render_indented_beneath_it_in_order(self) -> None:
        text = "one\ntwo\nthree"
        note = _note(_anchor("two"), text="watch this").model_copy(
            update={
                "replies": (
                    Reply(author="owner", text="any update?", at=datetime.now(UTC)),
                    Reply(author="agent", text="fixed it", at=datetime.now(UTC)),
                )
            }
        )
        sheet = NoteSheet(notes=(note,))

        rendered = annotate_read("RESULT", text, sheet)

        lines = rendered.splitlines()
        note_index = lines.index('[n1] line 2, status open, on "two": watch this')
        assert lines[note_index + 1] == "    reply (owner): any update?"
        assert lines[note_index + 2] == "    reply (agent): fixed it"


class TestSheetMutations:
    def test_add_note_mints_id_and_timestamp(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="hi")

        assert sheet.notes == (note,)
        assert note.text == "hi"
        assert note.created_at.tzinfo is not None

    def test_replace_note_updates_only_the_given_fields(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")

        updated_sheet, updated_note = replace_note(sheet, note.id, text="changed", archived=None)

        assert updated_note.text == "changed"
        assert updated_note.archived is False
        assert updated_sheet.notes == (updated_note,)

    def test_replace_note_can_archive_without_touching_text(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")

        _updated_sheet, updated_note = replace_note(sheet, note.id, text=None, archived=True)

        assert updated_note.text == "original"
        assert updated_note.archived is True

    def test_replace_note_raises_for_an_unknown_id(self) -> None:
        with pytest.raises(KeyError):
            replace_note(NoteSheet(), "no-such-id", text="x", archived=None)

    def test_remove_note_drops_the_matching_note(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")

        remaining = remove_note(sheet, note.id)

        assert remaining.notes == ()

    def test_remove_note_raises_for_an_unknown_id(self) -> None:
        with pytest.raises(KeyError):
            remove_note(NoteSheet(), "no-such-id")

    def test_add_reply_appends_to_the_thread_and_mints_the_timestamp(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")

        updated_sheet, updated_note = add_reply(sheet, note.id, author="owner", text="a reply")

        [reply] = updated_note.replies
        assert reply.author == "owner"
        assert reply.text == "a reply"
        assert reply.at.tzinfo is not None
        assert updated_sheet.notes == (updated_note,)

    def test_add_reply_preserves_reply_order_across_authors(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")
        sheet, note = add_reply(sheet, note.id, author="owner", text="first")
        _sheet, note = add_reply(sheet, note.id, author="agent", text="second")

        assert [reply.text for reply in note.replies] == ["first", "second"]

    def test_add_reply_raises_for_an_unknown_id(self) -> None:
        with pytest.raises(KeyError):
            add_reply(NoteSheet(), "no-such-id", author="owner", text="x")

    def test_set_status_resolves_and_reopens_a_thread(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")

        resolved_sheet, resolved_note = set_status(sheet, note.id, "resolved")
        assert resolved_note.status == "resolved"
        assert resolved_sheet.notes == (resolved_note,)

        reopened_sheet, reopened_note = set_status(resolved_sheet, note.id, "open")
        assert reopened_note.status == "open"
        assert reopened_sheet.notes == (reopened_note,)

    def test_set_status_raises_for_an_unknown_id(self) -> None:
        with pytest.raises(KeyError):
            set_status(NoteSheet(), "no-such-id", "resolved")

    def test_replace_note_text_is_always_allowed_while_the_note_exists(self) -> None:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")
        resolved_sheet, resolved_note = set_status(sheet, note.id, "resolved")

        _updated_sheet, updated_note = replace_note(resolved_sheet, resolved_note.id, text="revised", archived=None)

        assert updated_note.text == "revised"
        assert updated_note.status == "resolved"


class TestReplaceReply:
    def _sheet_with_replies(self) -> tuple[NoteSheet, Note]:
        sheet, note = add_note(NoteSheet(), anchor=_anchor("x"), text="original")
        sheet, note = add_reply(sheet, note.id, author="owner", text="first")
        sheet, note = add_reply(sheet, note.id, author="agent", text="second")
        return sheet, note

    def test_edits_an_owner_replys_text(self) -> None:
        sheet, note = self._sheet_with_replies()

        _updated_sheet, updated_note = replace_reply(sheet, note.id, 0, text="revised")

        assert [reply.text for reply in updated_note.replies] == ["revised", "second"]
        assert updated_note.replies[0].author == "owner"

    def test_refuses_to_edit_an_agent_reply(self) -> None:
        sheet, note = self._sheet_with_replies()

        with pytest.raises(PermissionError, match="agent reply cannot be edited"):
            replace_reply(sheet, note.id, 1, text="revised")

    def test_raises_for_an_unknown_note_id(self) -> None:
        with pytest.raises(KeyError):
            replace_reply(NoteSheet(), "no-such-id", 0, text="x")

    def test_raises_for_an_out_of_range_reply_index(self) -> None:
        sheet, note = self._sheet_with_replies()

        with pytest.raises(KeyError):
            replace_reply(sheet, note.id, 5, text="x")

    def test_the_agent_refusal_sentence_is_punctuation_clean(self) -> None:
        sheet, note = self._sheet_with_replies()

        with pytest.raises(PermissionError) as excinfo:
            replace_reply(sheet, note.id, 1, text="revised")

        message = str(excinfo.value)
        assert all(char.isalnum() or char in " ,.?()" for char in message)


class TestSummarize:
    def test_a_file_with_no_sidecar_is_absent_from_the_summary(self, tmp_path: Path) -> None:
        (tmp_path / "plain.md").write_text("body", encoding="utf-8")

        summaries = service.summarize(tmp_path)

        assert summaries == []

    def test_counts_split_by_status_and_no_agent_reply_is_none(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("one two", encoding="utf-8")
        open_note = _note(_anchor("one"))
        resolved_note = _note(_anchor("two")).model_copy(update={"status": "resolved"})
        save_sheet(tmp_path, target, NoteSheet(notes=(open_note, resolved_note)))

        [summary] = service.summarize(tmp_path)

        assert summary.path == "note.md"
        assert summary.open_notes == 1
        assert summary.resolved_notes == 1
        assert summary.latest_agent_reply_at is None

    def test_latest_agent_reply_ignores_owner_replies_and_picks_the_most_recent(self, tmp_path: Path) -> None:
        target = tmp_path / "note.md"
        target.write_text("one", encoding="utf-8")
        earlier = datetime(2026, 1, 1, tzinfo=UTC)
        later = datetime(2026, 6, 1, tzinfo=UTC)
        note_ = _note(_anchor("one")).model_copy(
            update={
                "replies": (
                    Reply(author="agent", text="first", at=earlier),
                    Reply(author="owner", text="owner reply", at=datetime(2026, 12, 1, tzinfo=UTC)),
                    Reply(author="agent", text="second", at=later),
                )
            }
        )
        save_sheet(tmp_path, target, NoteSheet(notes=(note_,)))

        [summary] = service.summarize(tmp_path)

        assert summary.latest_agent_reply_at == later

    def test_multiple_annotated_files_are_sorted_by_path(self, tmp_path: Path) -> None:
        for name in ("b.md", "a.md"):
            target = tmp_path / name
            target.write_text("x", encoding="utf-8")
            save_sheet(tmp_path, target, NoteSheet(notes=(_note(_anchor("x")),)))

        summaries = service.summarize(tmp_path)

        assert [summary.path for summary in summaries] == ["a.md", "b.md"]


class TestAnnotatedFiles:
    def test_finds_every_markdown_file_with_a_sidecar_and_skips_the_rest(self, tmp_path: Path) -> None:
        annotated = tmp_path / "annotated.md"
        annotated.write_text("body", encoding="utf-8")
        save_sheet(tmp_path, annotated, NoteSheet())
        (tmp_path / "plain.md").write_text("body", encoding="utf-8")

        found = annotated_files(tmp_path)

        assert found == [(annotated, "annotated.md")]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
