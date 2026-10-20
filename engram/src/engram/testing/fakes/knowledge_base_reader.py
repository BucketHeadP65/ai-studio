r"""An in-memory `KnowledgeBaseReader` for unit tests that never touches the filesystem.

A test scripts the four reads of :class:`FakeKnowledgeBaseReader` as `(path, text)` pairs and
the fake yields :class:`~engram.indexing.domain.models.RawDocument`\s. The scripted state is public.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engram.indexing.contracts import RawDocument

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


class FakeKnowledgeBaseReader:
    """Yields scripted knowledge base documents from memory."""

    def __init__(
        self,
        units: Iterable[tuple[str, str]] = (),
        *,
        sources: Iterable[tuple[str, str]] = (),
        vocabularies: Iterable[tuple[str, str]] = (),
        meta: dict[str, str] | None = None,
        root: str = "/fake/knowledge_base",
    ) -> None:
        """Script the documents.

        Args:
            units: `(path relative to the knowledge base, text)` pairs for the pages in the declared note folders and files.
            sources: Pairs for the `Sources/` mirrors.
            vocabularies: Pairs for the `_meta/vocabulary/*.md` pages.
            meta: Each name mapped to the text of its `_meta/<name>.md` orientation page.
            root: The value `root` reports.
        """
        self.units = list(units)
        self.sources = list(sources)
        self.vocabularies = list(vocabularies)
        self.meta = dict(meta or {})
        self.root = Path(root)

    def unit_documents(self) -> Iterator[RawDocument]:
        """Iterate the scripted pages of the declared note folders and files.

        Returns:
            An iterator over the scripted documents, in order.
        """
        return iter([RawDocument(path=path, text=text) for path, text in self.units])

    def source_mirrors(self) -> Iterator[RawDocument]:
        """Iterate the scripted `Sources/` mirror documents.

        Returns:
            An iterator over the scripted documents, in order.
        """
        return iter([RawDocument(path=path, text=text) for path, text in self.sources])

    def vocabulary_documents(self) -> Iterator[RawDocument]:
        """Iterate the scripted `_meta/vocabulary/*.md` documents.

        Returns:
            An iterator over the scripted documents, in order.
        """
        return iter([RawDocument(path=path, text=text) for path, text in self.vocabularies])

    def meta_document(self, name: str) -> RawDocument:
        """Answer the scripted `_meta/<name>.md` document.

        Args:
            name: The orientation page name without its extension.

        Returns:
            The document, with empty text when `name` was not scripted.
        """
        return RawDocument(path=f"_meta/{name}.md", text=self.meta.get(name, ""))
