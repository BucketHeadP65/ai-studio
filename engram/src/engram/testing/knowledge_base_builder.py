"""Builds a mini fixture knowledge base on disk for index tests.

A :class:`KnowledgeBaseBuilder` rooted at a ``tmp_path`` writes page notes into the folders the schema declares,
`Sources/` raw mirrors, `_meta/vocabulary/*.md` vocabularies copied from the golden
mini-vocabularies, and the three `_meta/` orientation stubs (`schema`, `purpose`,
`retrieval-policy`) the `api.about` content row is built from. `KnowledgeBaseSettings` accepts the tree once
`add_knowledge_base_schema` has written its schema file.

`add_unit_document` accepts raw markdown text or an :class:`~engram.kb.contracts.AuthoringPayload`,
which it renders with the shared note renderer.
"""

from __future__ import annotations

import shutil

from pathlib import Path
from typing import TYPE_CHECKING

from engram.kb.contracts import AuthoringPayload, render
from engram.testing.specs import research_spec

if TYPE_CHECKING:
    from collections.abc import Iterable

    from engram.kb.contracts import KnowledgeBaseSchema

# The golden fixtures shipped as package data beside this module: the mini vocabulary pages, the
# LightRAG benchmark paper note, and the mirror excerpt its claims quote.
GOLDEN = Path(__file__).resolve().parent / "golden"
assert GOLDEN.is_dir(), f"no golden fixtures at {GOLDEN} -- package data missing"
GOLDEN_MINI_VOCABULARIES = GOLDEN / "mini-vocabularies"
GOLDEN_LIGHTRAG_PAPER = GOLDEN / "lightrag-2025.md"
GOLDEN_MIRROR_EXCERPT = GOLDEN / "mirror-excerpt.md"
_GOLDEN_MINI_VOCABULARIES = GOLDEN_MINI_VOCABULARIES

_META_STUBS: dict[str, str] = {
    "schema": "# Schema\n\nThe binding note-grammar contract (stub for tests).\n",
    "purpose": "# Purpose\n\nWhy the knowledge base exists (stub for tests).\n",
    "retrieval-policy": "# Retrieval policy\n\nQuery-time doctrine (stub for tests).\n",
}


class KnowledgeBaseBuilder:
    """Writes a minimal but well-formed knowledge base under `root` for index-slice tests."""

    def __init__(self, root: Path) -> None:
        """Create `Sources/` and `_meta/` under `root`.

        Args:
            root: The knowledge base folder.
        """
        self.root = root
        (root / "Sources").mkdir(parents=True, exist_ok=True)
        (root / "_meta").mkdir(parents=True, exist_ok=True)

    def add_unit_document(self, folder: str, slug: str, content: str | AuthoringPayload) -> Path:
        """Write a `<folder>/<slug>.md` page from raw text or a rendered payload.

        `folder` must be one the schema declares for the page's type, because the index reads no other folder.

        Args:
            folder: The page's folder relative to the knowledge base root, such as `Wiki/Papers`.
            slug: The file name without `.md`.
            content: The raw markdown, or a payload to render.

        Returns:
            The written file.
        """
        text = render(content) if isinstance(content, AuthoringPayload) else content
        folder_path = self.root / folder
        folder_path.mkdir(parents=True, exist_ok=True)
        path = folder_path / f"{slug}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def add_source_mirror(self, slug: str, content: str) -> Path:
        """Write a `Sources/<slug>.md` raw mirror.

        Args:
            slug: The file name without `.md`.
            content: The mirror text.

        Returns:
            The written file.
        """
        path = self.root / "Sources" / f"{slug}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def add_vocabulary(self, names: Iterable[str] = ("entity-types", "relation-types", "facets")) -> None:
        """Copy the named golden mini-vocabulary pages into `_meta/vocabulary/`.

        Args:
            names: The vocabulary page names, all three by default.
        """
        vocabulary_dir = self.root / "_meta" / "vocabulary"
        vocabulary_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copyfile(_GOLDEN_MINI_VOCABULARIES / f"{name}.md", vocabulary_dir / f"{name}.md")

    def add_meta_stubs(self) -> None:
        """Write the three `_meta/` orientation pages (schema, purpose, retrieval-policy)."""
        for name, body in _META_STUBS.items():
            (self.root / "_meta" / f"{name}.md").write_text(body, encoding="utf-8")

    def add_knowledge_base_schema(self, spec: KnowledgeBaseSchema | None = None) -> Path:
        """Write `knowledge-base-schema.json` at the knowledge base root, `research_spec()` by default.

        You must write one before indexing or loading the schema, because there is no fallback schema.
        The default suits the research-domain golden fixtures. Pass `minimal_spec()` or your own
        `KnowledgeBaseSchema` for anything else.

        Args:
            spec: The schema to write, `research_spec()` when omitted.

        Returns:
            The written file.
        """
        resolved = spec if spec is not None else research_spec()
        path = self.root / "knowledge-base-schema.json"
        path.write_text(resolved.model_dump_json(), encoding="utf-8")
        return path

    def build_minimal(self) -> Path:
        """Write the vocabularies and the meta stubs.

        Returns:
            The knowledge base root.
        """
        self.add_vocabulary()
        self.add_meta_stubs()
        return self.root
