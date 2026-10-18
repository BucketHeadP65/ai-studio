"""`parse` does NOT infer missing frontmatter from a note's location.

`index` ingests only notes carrying explicit engram frontmatter (an authored note); turning a raw
note into an authored one is the job of the consented `/ingest` workflow, never a parse-time guess.
Inferring the page_type from the containing spec folder and slugifying the path into an id would sweep
every legacy/junk file in a declared folder into index and collapse every non-ASCII filename onto
the same `*.untitled` id (the slug regex is ASCII-only).

A note without an explicit, spec-declared `type` therefore raises `FrontmatterError` — index skips
it rather than inventing an id — while an explicitly-authored generic note parses unchanged.
"""

from __future__ import annotations

import pytest

from engram.kb.contracts import KnowledgeBaseSchema, PageTypeSpec, parse
from engram.kb.exceptions import FrontmatterError


def _personal_spec() -> KnowledgeBaseSchema:
    """A tiny domain-general spec: a `note` folder and a `moc` folder, no claims."""
    return KnowledgeBaseSchema(
        slug="personal",
        page_types=(PageTypeSpec(page_type="note", id_prefix="n.", folder="notes"), PageTypeSpec(page_type="moc", id_prefix="moc.", folder="MOC")),
    )


class TestNoFolderInference:
    """A note without explicit engram frontmatter is rejected, never inferred from its folder."""

    def test_note_in_declared_folder_without_type_is_not_inferred(self):
        # Living under the spec's `notes/` folder must NOT make parse guess `type: note`.
        with pytest.raises(FrontmatterError, match="unknown or missing page type"):
            parse("# Federated Learning\n\nBody text.", source_path="notes/Federated Learning.md", spec=_personal_spec())

    def test_non_ascii_filename_in_declared_folder_is_not_inferred(self):
        # The exact shape that caused the mass collision: a Greek-named file under a declared
        # folder. It must raise — never be assigned a slugified ("" → "untitled") id.
        with pytest.raises(FrontmatterError, match="unknown or missing page type"):
            parse("body", source_path="notes/Μπάλος.md", spec=_personal_spec())

    def test_note_under_no_declared_folder_is_not_inferred(self):
        with pytest.raises(FrontmatterError, match="unknown or missing page type"):
            parse("body", source_path="random/orphan.md", spec=_personal_spec())


class TestExplicitFrontmatterStillParses:
    """An explicitly-authored generic note (what `/ingest` produces) parses via the generic path."""

    def test_explicit_generic_note_parses_unchanged(self):
        text = "---\nid: n.custom\ntype: note\ntitle: Custom Title\nstatus: draft\n---\n\nBody."
        note = parse(text, source_path="notes/whatever.md", spec=_personal_spec())

        assert note.frontmatter.id == "n.custom"
        assert note.frontmatter.type == "note"
        assert note.frontmatter.title == "Custom Title"
