"""Unit tests for the validator's injected `global_ids` duplicate-id check.

The incremental indexer validates only the *dirty* notes, so an in-batch-only
duplicate-id scan would miss a dirty note whose `id` collides with a **clean** page
already in `kb.pages` (a row not in this batch). The union
`global_ids = DB ids ∪ dirty ids` is threaded into `validate(...)` so the `duplicate-id`
rule spans the whole knowledge base, not just the validated batch.

These pin that seam:

* a dirty note whose id is in `global_ids` (a clean DB id outside the batch) → `duplicate-id`;
* the same note with no `global_ids` collision (default empty, or a non-colliding set) → none;
* two in-batch notes sharing an id still collide via the union (the real caller always
  folds the dirty ids into `global_ids`).

`global_ids` defaults to empty, which is safe for the full-rebuild caller: it validates every
note, so the in-batch ids it folds into the union already cover the knowledge base.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from engram.indexing.contracts import resolve
from engram.indexing.domain.validator import validate
from engram.kb.contracts import KnowledgeBaseNote, VocabularyIndex, parse_frontmatter, parse_vocabulary
from engram.testing.knowledge_base_builder import GOLDEN_MINI_VOCABULARIES
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engram.indexing.contracts import ValidationReport

_MINI = GOLDEN_MINI_VOCABULARIES


@pytest.fixture(scope="module")
def vocabulary() -> VocabularyIndex:
    docs = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(_MINI.glob("*.md"))]
    return parse_vocabulary(docs).index


_RESEARCH_SPEC = research_spec()


def _paper(*, id_: str, source_path: str = "Memory/Papers/note.md") -> KnowledgeBaseNote:
    frontmatter = parse_frontmatter(
        {
            "id": id_,
            "type": "paper",
            "title": "LightRAG",
            "summary": "A short summary.",
            "status": "draft",
            "paper_type": "method",
            "year": 2025,
            "zotero_key": "K",
            "source_hash": "h",
        },
        _RESEARCH_SPEC,
    )
    return KnowledgeBaseNote(frontmatter=frontmatter, sections=(), claims=(), source_path=source_path)


def _report(notes: tuple[KnowledgeBaseNote, ...], vocabulary: VocabularyIndex, *, global_ids: Iterable[str] | None = None) -> ValidationReport:
    resolved = resolve(notes)
    if global_ids is None:
        return validate(resolved.notes, vocabulary, resolve_result=resolved, spec=research_spec())
    return validate(resolved.notes, vocabulary, resolve_result=resolved, global_ids=frozenset(global_ids), spec=research_spec())


def _dup_errors(report: ValidationReport) -> list[str]:
    return [issue.page_id for issue in report.errors if issue.rule == "duplicate-id"]


class TestGlobalIdsDuplicateId:
    """`validate(..., global_ids=...)` flags a dirty note colliding with a clean DB id."""

    def test_dirty_note_colliding_with_a_clean_db_id_is_a_duplicate_id_error(self, vocabulary: VocabularyIndex):
        # The note is the only one in this (dirty) batch, but its id already exists on a clean
        # page in kb.pages — present in global_ids, absent from the batch. The union check fires.
        note = _paper(id_="paper.lightrag-2025")
        report = _report((note,), vocabulary, global_ids={"paper.lightrag-2025"})

        assert _dup_errors(report) == ["paper.lightrag-2025"]

    def test_no_collision_with_a_non_overlapping_global_ids_set_is_clean(self, vocabulary: VocabularyIndex):
        note = _paper(id_="paper.lightrag-2025")
        report = _report((note,), vocabulary, global_ids={"paper.graphrag-2024", "paper.hipporag-2024"})

        assert _dup_errors(report) == []

    def test_default_empty_global_ids_raises_no_duplicate_id_error(self, vocabulary: VocabularyIndex):
        # No global_ids passed at all (the full-rebuild caller's signature) → no duplicate-id error
        # from validate for a single, unique-id note.
        note = _paper(id_="paper.lightrag-2025")
        report = _report((note,), vocabulary)

        assert _dup_errors(report) == []

    def test_two_in_batch_notes_sharing_an_id_still_collide_via_the_union(self, vocabulary: VocabularyIndex):
        # The real incremental caller folds the dirty ids INTO global_ids (DB ids ∪ dirty ids),
        # so two batch notes on the same id collide through the union exactly as the prior
        # whole-knowledge-base scan caught them — one error for the shared id.
        notes = (_paper(id_="paper.dup", source_path="Memory/Papers/a.md"), _paper(id_="paper.dup", source_path="Memory/Papers/b.md"))
        report = _report(notes, vocabulary, global_ids={"paper.dup"})

        assert _dup_errors(report) == ["paper.dup"]
