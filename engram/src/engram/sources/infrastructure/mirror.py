"""`ObsidianMirrorStore`, the `MirrorStore` adapter over the knowledge base's `Sources/` folder.

State lives entirely in each note's YAML frontmatter. Writes are atomic (a temporary file in the
same directory, then a rename), so a half-written note is never seen by Obsidian or a concurrent
reconcile. The store guards atomicity and a single active note per key. It does not decide what
to write: `write` overwrites unconditionally, and the sync decides when to call it. Active mirrors
are named after the sanitised item title, and the permanent identity is the `source_key` in
frontmatter, so a title change renames the note and a title collision appends the key. Archived
notes are named by key. A note carrying no `source_key`, or whose frontmatter does not satisfy
`MirrorFile`, is skipped rather than guessed at.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import anyio

from core.files import atomic_write, atomic_write_bytes, safe_slug
from core.markdown import render_frontmatter, split_frontmatter
from core.observability import get_logger
from pydantic import ValidationError

from engram.sources.domain.mirror import MirrorFile, MirrorFrontmatter, MirrorStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pydantic import JsonValue

    from engram.config import KnowledgeBaseSettings

_logger = get_logger(__name__)

_FENCE = "---\n"
# Cap the title-derived stem well under the 255-byte filesystem limit, leaving room for a " (KEY).md" suffix.
_MAX_STEM = 150


class ObsidianMirrorStore:
    """`MirrorStore` backed by Markdown notes under the Obsidian knowledge base's `Sources/` folder.

    Active mirrors live at `sources_dir/<sanitised title>.md` and soft-deleted ones at
    `archive_dir/<source_key>.md`. Both carry a YAML frontmatter block that is the only
    persisted state. The `source_key` in that frontmatter, not the filename, is the join key.
    After `load_index` the store finds an active mirror by key from what that walk read, so a write
    reads no other mirror. Before it, every lookup scans `Sources/`.
    """

    def __init__(self, settings: KnowledgeBaseSettings) -> None:
        """Store the sources and archive folders.

        Args:
            settings: The knowledge base's settings, which name `sources_dir` and `archive_dir`.
        """
        self._sources_dir = anyio.Path(settings.sources_dir)
        self._archive_dir = anyio.Path(settings.archive_dir)
        self._active: dict[str, anyio.Path] | None = None

    async def _identified_mirrors(self) -> AsyncIterator[tuple[str, Mapping[str, JsonValue], anyio.Path]]:
        """Yield `(source_key, frontmatter, path)` for every note under `Sources/` and `_archive/` that carries a `source_key`."""
        for directory in (self._sources_dir, self._archive_dir):
            if not await directory.is_dir():
                continue
            async for note in directory.glob("*.md"):
                frontmatter = _parse_frontmatter(await note.read_text(encoding="utf-8"))
                if frontmatter is None:
                    continue
                key = _identity_key(frontmatter)
                if key is not None:
                    yield key, frontmatter, note

    async def load_index(self) -> dict[str, MirrorFile]:
        """Map `source_key` -> `MirrorFile` for every note under `Sources/` and `_archive/`.

        A note whose frontmatter does not satisfy `MirrorFile` is logged against its own path and
        skipped, never guessed at, so one malformed file never fails the walk over the rest.

        Returns:
            The current mirror state by key.
        """
        index: dict[str, MirrorFile] = {}
        active: dict[str, anyio.Path] = {}
        async for key, frontmatter, path in self._identified_mirrors():
            if path.parent == self._sources_dir:
                active[key] = path
            try:
                mirror_file = MirrorFile.model_validate({**frontmatter, "source_key": key})
            except ValidationError as error:
                _logger.warning("Skipping mirror {path}, its frontmatter is not a MirrorFile: {reason}", path=str(path), reason=str(error))
                continue
            index[mirror_file.source_key] = mirror_file
        self._active = active
        return index

    async def load_source_links(self) -> dict[str, str]:
        """Map `source_key` -> the deep link that mirror carries, over `Sources/` and `_archive/`.

        A mirror whose connector owns no deep link (an absent or blank `source_link`) is absent from
        the map rather than present with an empty value, so a caller reads a hit as "there is a
        link". Values are stripped of surrounding whitespace.

        Returns:
            The deep links by key.
        """
        links: dict[str, str] = {}
        async for key, frontmatter, _path in self._identified_mirrors():
            link = frontmatter.get("source_link")
            if isinstance(link, str) and link.strip():
                links[key] = link.strip()
        return links

    async def read_body(self, key: str) -> str | None:
        """The Markdown body of the active mirror whose frontmatter carries `key`.

        Args:
            key: The mirror's `source_key`.

        Returns:
            The body, or `None` if absent.
        """
        note = await self._path_for_key(key, self._sources_dir)
        if note is None:
            return None
        return _split_body(await note.read_text(encoding="utf-8"))

    async def write(self, *, frontmatter: MirrorFrontmatter, body: str, assets: Mapping[str, bytes] | None = None) -> None:
        """Atomically create or overwrite this item's mirror at its title-derived path.

        Removes any prior note for the same key under a different name (title-change rename) and any
        archived copy (restore-on-recreate), so a key always maps to exactly one active note. Asset
        files are written under `Sources/` at their given relative paths before the note, so a
        reader never sees a note whose images are still missing.

        Args:
            frontmatter: The note's frontmatter, whose `source_key` names the mirror.
            body: The Markdown body, written verbatim.
            assets: Figures the body references, keyed by Sources-relative path.

        Raises:
            ValueError: An asset path would escape `Sources/`.
        """
        await self._sources_dir.mkdir(parents=True, exist_ok=True)
        for relpath, data in (assets or {}).items():
            asset_target = self._asset_path(relpath)
            await asset_target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(Path(asset_target), data)

        existing = await self._path_for_key(frontmatter.source_key, self._sources_dir)
        target = await self._target_path(frontmatter, existing)
        atomic_write(Path(target), _render(frontmatter, body))

        if existing is not None and existing.name != target.name:
            await existing.unlink()
        if self._active is not None:
            self._active[frontmatter.source_key] = target

        archived = self._archive_dir / f"{frontmatter.source_key}.md"
        if await archived.is_file():
            await archived.unlink()

    async def soft_delete(self, key: str) -> bool:
        """Move this item's active note to `_archive/<key>.md` with `status: removed`.

        Args:
            key: The mirror's `source_key`.

        Returns:
            `True` if a note was moved.
        """
        note = await self._path_for_key(key, self._sources_dir)
        if note is None:
            return False

        text = await note.read_text(encoding="utf-8")
        frontmatter = _parse_frontmatter(text) or {}
        frontmatter["status"] = MirrorStatus.REMOVED.value
        body = _split_body(text) or ""

        await self._archive_dir.mkdir(parents=True, exist_ok=True)
        archived = self._archive_dir / f"{key}.md"
        atomic_write(Path(archived), render_frontmatter(frontmatter, body))
        await note.unlink()
        if self._active is not None:
            self._active.pop(key, None)
        return True

    async def _path_for_key(self, key: str, directory: anyio.Path) -> anyio.Path | None:
        """The note in `directory` whose frontmatter identity key is `key`, from the loaded index when there is one, else by a scan."""
        if self._active is not None and directory == self._sources_dir:
            known = self._active.get(key)
            return known if known is not None and await known.is_file() else None
        if not await directory.is_dir():
            return None
        async for note in directory.glob("*.md"):
            frontmatter = _parse_frontmatter(await note.read_text(encoding="utf-8"))
            if frontmatter is not None and _identity_key(frontmatter) == key:
                return note
        return None

    def _asset_path(self, relpath: str) -> anyio.Path:
        """Resolve an asset's Sources-relative path, rejecting anything that would escape `Sources/`."""
        pure = PurePosixPath(relpath)
        if pure.is_absolute() or ".." in pure.parts:
            msg = f"asset path escapes Sources/: {relpath!r}"
            raise ValueError(msg)
        return self._sources_dir / relpath

    async def _target_path(self, frontmatter: MirrorFrontmatter, existing: anyio.Path | None) -> anyio.Path:
        """The title-derived path to write to: the plain title note, or a `(key)`-disambiguated one on collision."""
        stem = safe_slug(frontmatter.title, max_len=_MAX_STEM)
        candidate = self._sources_dir / f"{stem}.md"
        if existing is not None and existing.name == candidate.name:
            return candidate
        if await candidate.is_file():
            other = _parse_frontmatter(await candidate.read_text(encoding="utf-8"))
            if other is not None and _identity_key(other) != frontmatter.source_key:
                return self._sources_dir / f"{stem} ({frontmatter.source_key}).md"
        return candidate


def _render(frontmatter: MirrorFrontmatter, body: str) -> str:
    """A mirror note's text: the frontmatter without an unset `channel` or `published` or an empty `meta`, then the body."""
    return render_frontmatter(frontmatter.to_disk_fields(), body)


def _identity_key(frontmatter: Mapping[str, JsonValue]) -> str | None:
    """The mirror's identity key, its `source_key`, from a raw frontmatter mapping, or `None` when absent."""
    value = frontmatter.get("source_key")
    return str(value) if value is not None else None


def _parse_frontmatter(text: str) -> dict[str, JsonValue] | None:
    """The YAML frontmatter mapping between the first two `---` fences, or `None` when there is no fence or no mapping."""
    if not text.startswith(_FENCE):
        return None
    data, _ = split_frontmatter(text)
    return data or None


def _split_body(text: str) -> str | None:
    """Everything after the closing frontmatter fence, or `None` if there is no frontmatter block."""
    if not text.startswith(_FENCE):
        return None
    _, body = split_frontmatter(text)
    return body
