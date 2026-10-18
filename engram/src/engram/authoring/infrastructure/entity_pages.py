"""The entity pages of one knowledge base on disk: read by name for entity resolution, created when a name has none."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

import yaml

from core.files import atomic_write, exclusive_lock
from core.markdown import render_frontmatter, split_frontmatter

from engram.authoring.domain import AliasMerge, KnownEntities, aliases_with
from engram.authoring.exceptions import NoteRefusedError
from engram.authoring.infrastructure.note_writer import NoteWriter
from engram.retrieval import contracts as retrieval

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import JsonValue

    from engram.authoring.domain import AuthoringKnowledgeBase, CheckedNote


class _PageNames(NamedTuple):
    """One entity page's id with the names entity resolution matches it by."""

    id: str
    title: str
    aliases: list[str]


def _split(path: Path) -> tuple[dict[str, JsonValue], str] | None:
    """The frontmatter and body of the file at `path`, or `None` when it cannot be read as a page."""
    try:
        return split_frontmatter(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def _names(frontmatter: dict[str, JsonValue]) -> _PageNames | None:
    """The id, title and aliases `frontmatter` carries, or `None` when it has no string `id`."""
    page_id, title, aliases = frontmatter.get("id"), frontmatter.get("title"), frontmatter.get("aliases")
    if not isinstance(page_id, str) or not page_id:
        return None
    return _PageNames(
        id=page_id, title=title if isinstance(title, str) else "", aliases=[str(alias) for alias in aliases] if isinstance(aliases, list) else []
    )


class EntityPages:
    """The entity pages of one knowledge base, kept in the page type its schema's `entities` block names."""

    def __init__(self, knowledge_base: AuthoringKnowledgeBase) -> None:
        """Hold the knowledge base whose entity pages are read and created.

        Args:
            knowledge_base: The knowledge base. With no `entities` block in its schema, it has no entity pages.
        """
        self._target = knowledge_base
        self._paths: dict[str, Path] = {}

    def known(self) -> KnownEntities:
        """Every entity page on disk by its title and its aliases, a title winning over another page's alias.

        A file with no readable frontmatter or no string `id` is left out. Each page's file is kept for `path_of`.

        Returns:
            The pages entity resolution can match a name to.
        """
        known = KnownEntities()
        entities = self._target.schema.entities
        page_type = self._target.schema.page_type(entities.page_type) if entities is not None else None
        if page_type is None:
            return known
        pages: list[_PageNames] = []
        for path in retrieval.page_type_paths(self._target.path, page_type):
            split = _split(path)
            names = _names(split[0]) if split is not None else None
            if names is not None:
                pages.append(names)
                self._paths.setdefault(names.id, path)
        for page in pages:
            known.add(page.id, [page.title])
        for page in pages:
            known.add(page.id, page.aliases)
        return known

    def path_of(self, page_id: str) -> Path:
        """The file of an entity page `known()` read.

        Args:
            page_id: The id of a page `known()` read.

        Returns:
            The page's file, the first one read when two files carry the id.

        Raises:
            LookupError: `known()` read no page with that id.
        """
        found = self._paths.get(page_id)
        if found is None:
            msg = f"no entity page {page_id!r} was read"
            raise LookupError(msg)
        return found

    def create(self, checked: CheckedNote) -> bool:
        """Publish a checked entity page at a path nothing holds yet.

        Args:
            checked: The page `NoteWriter.check` passed.

        Returns:
            True when this call published it. False when a page already sat at its path, which is left as it is.
        """
        try:
            NoteWriter(self._target).create(checked)
        except FileExistsError:
            return False
        return True

    def merge_aliases(self, path: Path, names: Sequence[str]) -> AliasMerge:
        """Add each of `names` that the entity page at `path` does not already match to its aliases, holding the page's lock.

        The lock is the hidden file `.<page>.lock` beside the page. The call blocks while another writer holds it, so run
        it off the event loop. The page is written only when a name was added.

        Args:
            path: The entity page the names belong to.
            names: The names this call resolved to the page.

        Returns:
            The page's id and the names added to its aliases.

        Raises:
            NoteRefusedError: The file at `path` has no readable frontmatter or no string `id`. It is left as it is.
        """
        with exclusive_lock(path.with_name(f".{path.name}.lock")):
            split = _split(path)
            page = _names(split[0]) if split is not None else None
            if split is None or page is None:
                msg = f"{path.name} already holds a file entity resolution cannot read as an entity page. Fix or remove it, then resolve again"
                raise NoteRefusedError(msg)
            frontmatter, body = split
            merged = aliases_with(page.title, page.aliases, names)
            if merged != page.aliases:
                aliases: list[JsonValue] = list(merged)
                atomic_write(path, render_frontmatter({**frontmatter, "aliases": aliases}, body))
        return AliasMerge(page_id=page.id, added=tuple(merged[len(page.aliases) :]))
