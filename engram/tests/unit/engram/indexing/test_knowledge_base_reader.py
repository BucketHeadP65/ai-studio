r"""Unit tests for the filesystem knowledge base reader + the pure mirror heading-chunker.

Two surfaces in `src/engram/indexing/infrastructure/knowledge_base_reader.py` and `src/engram/indexing/domain/mirror_chunks.py`:

* :class:`FilesystemKnowledgeBaseReader` — globs the four read surfaces the Parse stage
  consumes (the declared note folders, `Sources/*.md`, `_meta/vocabulary/*.md`, named
  ``_meta/`` pages), yielding :class:`RawDocument`\s with **knowledge-base-relative** paths
  in **deterministic sorted** order (stable indexes), tolerating missing dirs.
* :func:`chunk_mirror` — splits a raw mirror body by its numbered headings into
  chunk drafts whose `section_path` is the numbered path indexed to an ltree
  label (`## 4 Experiments` → `s4`; `### 4.1` → `s4.s1`;
  `#### 7.3.1` → `s7.s3.s1`), matching `resolver.loc_to_ltree` so the L4
  verification join lines up. Unnumbered headings + the title/preamble carry
  `section_path = None`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engram.indexing.contracts import DEFAULT_CHUNK_SIZES, FilesystemKnowledgeBaseReader, RawDocument, token_count
from engram.indexing.domain.mirror_chunks import MIRROR_CHUNK_TOKEN_BUDGET, MirrorChunk, chunk_mirror

if TYPE_CHECKING:
    from collections.abc import Iterator

# A trimmed but faithful copy of the LightRAG mirror's heading skeleton (no-dot
# numbering, deep 3-level appendix numbering, an unnumbered References section).
_MIRROR = """\
---
source_key: K1
provider: example-library
source_hash: h1
---
# LightRAG: Simple and Fast Retrieval-Augmented Generation

Abstract-ish preamble before any numbered section.

## 1 Introduction

Intro body line one.
Intro body line two.

## 3 The LightRAG Architecture

Architecture overview.

### 3.1 Graph-based Text Indexing

Indexing body.

### 3.2 Dual-level Retrieval Paradigm

Retrieval body.

## 4 Evaluation

Eval overview.

### 4.1 Experimental Settings

Settings body.

## References

Bibliography body.

## 7 Appendix

Appendix overview.

### 7.3 Overview of the Prompts Used

Prompts overview.

#### 7.3.1 Prompts for Graph Generation

Deep prompt body.
"""


_NOTE_FOLDERS = ("Wiki/Papers", "Wiki/Entities")
"""The folders the knowledge base below declares for its page types."""


def _build_knowledge_base(tmp_path: Path) -> Path:
    """A tiny on-disk knowledge base with one page, one mirror, two vocabularies + meta stubs."""
    (tmp_path / "Wiki" / "Papers").mkdir(parents=True)
    (tmp_path / "Wiki" / "Entities").mkdir(parents=True)
    (tmp_path / "Sources").mkdir(parents=True)
    (tmp_path / "_meta" / "vocabulary").mkdir(parents=True)

    (tmp_path / "Wiki" / "Papers" / "lightrag-2025.md").write_text("# paper", encoding="utf-8")
    (tmp_path / "Wiki" / "Entities" / "lightrag.md").write_text("# entity", encoding="utf-8")
    # A non-markdown sibling and an image must be ignored by the globs.
    (tmp_path / "Wiki" / "Papers" / "notes.txt").write_text("ignore me", encoding="utf-8")
    (tmp_path / "Sources" / "lightrag.md").write_text(_MIRROR, encoding="utf-8")
    (tmp_path / "Sources" / "diagram.png").write_bytes(b"\x89PNG")
    (tmp_path / "_meta" / "vocabulary" / "facets.md").write_text("# facets", encoding="utf-8")
    (tmp_path / "_meta" / "vocabulary" / "entity-types.md").write_text("# entity-types", encoding="utf-8")
    (tmp_path / "_meta" / "schema.md").write_text("# schema", encoding="utf-8")
    (tmp_path / "_meta" / "purpose.md").write_text("# purpose", encoding="utf-8")
    (tmp_path / "_meta" / "retrieval-policy.md").write_text("# retrieval-policy", encoding="utf-8")
    return tmp_path


class TestFilesystemKnowledgeBaseReaderGlobs:
    """The four read surfaces: only `.md`, knowledge-base-relative paths, sorted, fault-tolerant."""

    def test_memory_notes_globs_only_markdown_recursively(self, tmp_path: Path):
        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        docs = list(reader.unit_documents())

        paths = [doc.path for doc in docs]
        assert paths == ["Wiki/Entities/lightrag.md", "Wiki/Papers/lightrag-2025.md"]
        assert all(isinstance(doc, RawDocument) for doc in docs)

    def test_memory_notes_paths_are_knowledge_base_relative_with_forward_slashes(self, tmp_path: Path):
        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        paths = [doc.path for doc in reader.unit_documents()]

        assert all(not Path(path).is_absolute() for path in paths)
        assert all("\\" not in path for path in paths)

    def test_memory_notes_returns_each_file_text(self, tmp_path: Path):
        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        by_path = {doc.path: doc.text for doc in reader.unit_documents()}

        assert by_path["Wiki/Papers/lightrag-2025.md"] == "# paper"

    def test_unit_documents_are_read_from_the_declared_folders_alone(self, tmp_path: Path):
        # A folder no page type declares is not read, whatever it is called.
        (tmp_path / "notes").mkdir()
        (tmp_path / "MOC").mkdir()
        (tmp_path / "Units").mkdir()
        (tmp_path / "notes" / "alpha.md").write_text("# alpha", encoding="utf-8")
        (tmp_path / "MOC" / "index.md").write_text("# index", encoding="utf-8")
        (tmp_path / "Units" / "stray.md").write_text("# stray", encoding="utf-8")

        reader = FilesystemKnowledgeBaseReader(tmp_path, note_folders=("notes", "MOC"))

        paths = [doc.path for doc in reader.unit_documents()]

        assert paths == ["MOC/index.md", "notes/alpha.md"]

    def test_a_file_under_two_declared_folders_is_yielded_once(self, tmp_path: Path):
        (tmp_path / "Wiki" / "Papers").mkdir(parents=True)
        (tmp_path / "Wiki" / "Papers" / "p.md").write_text("# p", encoding="utf-8")

        reader = FilesystemKnowledgeBaseReader(tmp_path, note_folders=("Wiki", "Wiki/Papers"))

        paths = [doc.path for doc in reader.unit_documents()]

        assert paths == ["Wiki/Papers/p.md"]

    def test_source_mirrors_globs_only_markdown(self, tmp_path: Path):
        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        paths = [doc.path for doc in reader.source_mirrors()]

        assert paths == ["Sources/lightrag.md"]

    def test_source_mirrors_honors_a_custom_sources_subdir(self, tmp_path: Path):
        # The read side must honor KNOWLEDGE_BASE_SOURCES_SUBDIR so it agrees with the mirror
        # writer (KnowledgeBaseSettings.sources_subdir); a renamed mirrors folder written to
        # Refs/ must be read from Refs/, not silently missed under a hardcoded Sources/.
        (tmp_path / "Refs").mkdir()
        (tmp_path / "Refs" / "paper.md").write_text("# mirror", encoding="utf-8")
        (tmp_path / "Sources").mkdir()
        (tmp_path / "Sources" / "stale.md").write_text("# decoy", encoding="utf-8")

        reader = FilesystemKnowledgeBaseReader(tmp_path, sources_subdir="Refs")

        paths = [doc.path for doc in reader.source_mirrors()]

        assert paths == ["Refs/paper.md"]

    def test_registry_documents_sorted(self, tmp_path: Path):
        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        paths = [doc.path for doc in reader.vocabulary_documents()]

        assert paths == ["_meta/vocabulary/entity-types.md", "_meta/vocabulary/facets.md"]

    def test_meta_document_by_name(self, tmp_path: Path):
        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        doc = reader.meta_document("schema")

        assert doc.path == "_meta/schema.md"
        assert doc.text == "# schema"

    def test_missing_directories_yield_empty(self, tmp_path: Path):
        # tmp_path has none of the declared folders, Sources or _meta, so every iterator must be empty and none may raise.
        reader = FilesystemKnowledgeBaseReader(tmp_path, note_folders=_NOTE_FOLDERS)

        assert list(reader.unit_documents()) == []
        assert list(reader.source_mirrors()) == []
        assert list(reader.vocabulary_documents()) == []

    def test_iterators_are_lazy(self, tmp_path: Path):
        from collections.abc import Iterator as _Iterator

        reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

        assert isinstance(reader.unit_documents(), _Iterator)


class TestChunkMirror:
    """Numbered headings → ltree `section_path`; preamble/unnumbered → `None`."""

    def _chunks(self) -> list[MirrorChunk]:
        return list(chunk_mirror(_MIRROR, DEFAULT_CHUNK_SIZES))

    def test_preamble_is_the_first_chunk_with_no_section_path(self):
        chunks = self._chunks()

        first = chunks[0]
        assert first.section_path is None
        assert first.ord == 0
        assert "preamble before any numbered section" in first.body

    def test_numbered_h2_compiles_to_single_ltree_label(self):
        by_heading = {chunk.heading: chunk for chunk in self._chunks()}

        assert by_heading["1 Introduction"].section_path == "s1"
        assert by_heading["4 Evaluation"].section_path == "s4"

    def test_numbered_h3_compiles_to_dotted_ltree_label(self):
        by_heading = {chunk.heading: chunk for chunk in self._chunks()}

        assert by_heading["3.1 Graph-based Text Indexing"].section_path == "s3.s1"
        assert by_heading["4.1 Experimental Settings"].section_path == "s4.s1"

    def test_deep_h4_numbering_compiles_to_three_segments(self):
        by_heading = {chunk.heading: chunk for chunk in self._chunks()}

        assert by_heading["7.3.1 Prompts for Graph Generation"].section_path == "s7.s3.s1"

    def test_unnumbered_heading_has_no_section_path(self):
        by_heading = {chunk.heading: chunk for chunk in self._chunks()}

        assert by_heading["References"].section_path is None

    def test_section_path_matches_resolver_loc_to_ltree(self):
        # The L4 join is raw_chunks.section_path @> claims.loc_path, so the chunker
        # and the resolver MUST agree on the label format.
        from engram.indexing.domain.resolver import loc_to_ltree

        by_heading = {chunk.heading: chunk for chunk in self._chunks()}
        assert by_heading["3.1 Graph-based Text Indexing"].section_path == loc_to_ltree("§3.1")

    def test_body_holds_only_its_own_text(self):
        by_heading = {chunk.heading: chunk for chunk in self._chunks()}

        intro = by_heading["1 Introduction"]
        assert "Intro body line one." in intro.body
        assert "Intro body line two." in intro.body
        assert "Architecture overview" not in intro.body

    def test_ord_is_dense_and_increasing_in_document_order(self):
        ords = [chunk.ord for chunk in self._chunks()]

        assert ords == list(range(len(ords)))

    def test_headings_in_document_order(self):
        headings = [chunk.heading for chunk in self._chunks()][1:]  # drop preamble

        assert headings == [
            "1 Introduction",
            "3 The LightRAG Architecture",
            "3.1 Graph-based Text Indexing",
            "3.2 Dual-level Retrieval Paradigm",
            "4 Evaluation",
            "4.1 Experimental Settings",
            "References",
            "7 Appendix",
            "7.3 Overview of the Prompts Used",
            "7.3.1 Prompts for Graph Generation",
        ]

    def test_empty_body_before_first_heading_yields_no_preamble_chunk(self):
        text = "## 1 Intro\n\nBody.\n"

        chunks = list(chunk_mirror(text, DEFAULT_CHUNK_SIZES))

        assert [chunk.section_path for chunk in chunks] == ["s1"]

    def test_a_section_over_the_token_budget_splits_at_paragraph_breaks(self):
        paragraphs = [f"Paragraph {index} " + " ".join(["word"] * 300) for index in range(60)]
        text = "## 4 Experiments\n\n" + "\n\n".join(paragraphs) + "\n"

        chunks = list(chunk_mirror(text, DEFAULT_CHUNK_SIZES))

        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= MIRROR_CHUNK_TOKEN_BUDGET for chunk in chunks)
        assert {(chunk.heading, chunk.section_path) for chunk in chunks} == {("4 Experiments", "s4")}
        assert [chunk.ord for chunk in chunks] == list(range(len(chunks)))
        assert "\n\n".join(chunk.body for chunk in chunks) == "\n\n".join(paragraphs)

    def test_a_paragraph_over_the_token_budget_splits_at_word_breaks(self):
        words = [f"w{index}" for index in range(20_000)]
        text = "## Transcript\n\n" + " ".join(words) + "\n"

        chunks = list(chunk_mirror(text, DEFAULT_CHUNK_SIZES))

        assert len(chunks) > 1
        assert all(token_count(chunk.body) <= MIRROR_CHUNK_TOKEN_BUDGET for chunk in chunks)
        assert " ".join(chunk.body for chunk in chunks).split() == words


def _exhaust(iterator: Iterator[RawDocument]) -> int:
    return sum(1 for _ in iterator)


def test_memory_glob_does_not_leak_meta_or_sources(tmp_path: Path):
    # Only the two pages, never the mirror, vocabularies, or _meta pages.
    reader = FilesystemKnowledgeBaseReader(_build_knowledge_base(tmp_path), note_folders=_NOTE_FOLDERS)

    assert _exhaust(reader.unit_documents()) == 2
