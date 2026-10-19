r"""The indexing slice's filesystem read adapter.

`FilesystemKnowledgeBaseReader` is the concrete `KnowledgeBaseReader`, the Parse stage's input. It yields
`RawDocument`\s with knowledge-base-relative, forward-slashed paths in sorted order. A missing directory yields
nothing rather than raising.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from engram.indexing.domain.models import RawDocument

if TYPE_CHECKING:
    from collections.abc import Iterator

# The split-doc generated-output banner. A file opening with it is skipped wherever the reader yields notes.
_GENERATED_BANNER = "<!-- GENERATED"


def _is_generated(text: str) -> bool:
    """Whether `text` opens with the split-doc generated-output banner."""
    return text.lstrip().startswith(_GENERATED_BANNER)


class FilesystemKnowledgeBaseReader:
    """Reads the knowledge base's markdown surfaces off disk (a `KnowledgeBaseReader`).

    You must pass the folders and files the schema declares for its page types. The reader reads no note folder of
    its own. `sources_subdir` must match the `knowledge_base.sources_subdir` the mirrors are written under.
    """

    def __init__(
        self,
        knowledge_base_path: Path,
        sources_subdir: str = "Sources",
        *,
        note_folders: tuple[str, ...],
        note_files: tuple[str, ...] = (),
    ) -> None:
        """Read the knowledge base at `knowledge_base_path`.

        Args:
            knowledge_base_path: The knowledge base root.
            sources_subdir: The mirrors folder under the root.
            note_folders: Root-relative folders read recursively for notes.
            note_files: Root-relative markdown files read as notes, `..` segments allowed.
        """
        self._root = Path(knowledge_base_path)
        self._sources_subdir = sources_subdir
        # Root-relative folders the schema's page types declare.
        self._note_folders = note_folders
        # Root-relative markdown files for pages outside any folder a glob reaches.
        self._note_files = note_files


    @property
    def root(self) -> Path:
        """The knowledge base root this reader walks, the identity an index claims its collection with."""
        return self._root

    def unit_documents(self) -> Iterator[RawDocument]:
        """The unit documents under the declared note folders, and the declared note files.

        Folders are read recursively. A file reached more than once is yielded once, and a file opening
        with the generated banner is never yielded.

        Returns:
            The documents, sorted by knowledge-base-relative path.
        """
        seen: dict[str, RawDocument] = {}
        for folder in self._note_folders:
            for document in self._glob(folder, recursive=True):
                seen.setdefault(document.path, document)
        for rel in self._note_files:
            document = self._read_note_file(rel)
            if document is not None:
                seen.setdefault(document.path, document)
        return iter(sorted(seen.values(), key=lambda document: document.path))

    def source_mirrors(self) -> Iterator[RawDocument]:
        """Every mirror under the sources folder.

        Returns:
            The mirrors, sorted by knowledge-base-relative path.
        """
        return self._glob(self._sources_subdir, recursive=False)

    def vocabulary_documents(self) -> Iterator[RawDocument]:
        """Every `_meta/vocabulary/*.md` vocabulary page.

        Returns:
            The vocabulary pages, sorted by knowledge-base-relative path.
        """
        return self._glob("_meta/vocabulary", recursive=False)

    def meta_document(self, name: str) -> RawDocument:
        """Read one `_meta/<name>.md` page.

        Args:
            name: The page name without its extension, such as `purpose`.

        Returns:
            The page.

        Raises:
            FileNotFoundError: The page does not exist.
        """
        path = self._root / "_meta" / f"{name}.md"
        return self._read(path)

    def _glob(self, subdir: str, *, recursive: bool) -> Iterator[RawDocument]:
        base = self._root / subdir
        if not base.is_dir():
            return iter(())
        pattern = "**/*.md" if recursive else "*.md"
        files = sorted(path for path in base.glob(pattern) if path.is_file())
        documents = (self._read(path) for path in files)
        return iter([document for document in documents if not _is_generated(document.text)])

    def _read(self, path: Path) -> RawDocument:
        relative = path.relative_to(self._root).as_posix()
        return RawDocument(path=relative, text=path.read_text(encoding="utf-8"))

    def _read_note_file(self, rel: str) -> RawDocument | None:
        """Read one `note_files` entry, or `None` when it is missing or opens with the generated banner."""
        path = self._root / rel
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        if _is_generated(text):
            return None
        return RawDocument(path=Path(rel).as_posix(), text=text)
