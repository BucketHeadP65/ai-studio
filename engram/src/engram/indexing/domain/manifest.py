"""Pure index-manifest change detection (no IO, no database imports).

The index manifest maps each indexed file `path` to its `content_hash` (raw
file text) and `identity_hash` (its normalized resolution-key set). Re-hashing
every file and diffing against the stored manifest yields the raw
`{added, removed, content_changed, identity_changed, unchanged}` partition
without parsing. Incremental indexing starts from it.

- `content_hash` answers "did anything in this file change?" (the re-parse gate).
- `identity_hash` answers "did this file's effect on how other files resolve
  change?" (the inbound-closure gate). It is `card.content_hash` of the
  lower-cased, de-duplicated, sorted key set `{stem, id, title, *aliases}`.

`compute_closure` then turns that raw diff into the `DirtySet`, the minimal set
of files whose indexed output can differ. It asks the stored link-reference graph
for the inbound closure (pages that reference an appeared or disappeared
resolution key) and the mirror citers (pages that cite a changed source). It is
single-level: a closure page's own identity is unchanged, so re-resolving it
widens nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from engram.indexing.domain.card import content_hash

if TYPE_CHECKING:
    from collections.abc import Callable


class FileType(StrEnum):
    """Whether an indexed file is a memory note or a source mirror."""

    NOTE = "note"
    SOURCE = "source"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One stored manifest row (the prior index's view of a file)."""

    path: str
    kind: FileType
    content_hash: str
    identity_hash: str
    page_id: str | None
    source_key: str | None


@dataclass(frozen=True, slots=True)
class HashedFile:
    """A file as seen this run: its path, kind, raw text, and the two hashes."""

    path: str
    kind: FileType
    text: str
    content_hash: str
    identity_hash: str


@dataclass(frozen=True, slots=True)
class ManifestDiff:
    """The raw partition from re-hashing all files vs the stored manifest."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    content_changed: tuple[str, ...]
    identity_changed: tuple[str, ...]
    unchanged: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IdentitySurface:
    """The resolution keys a file exposes, paired with the row id it produced.

    For a note `page_id` is the `kb.pages` id and `keys` is the normalized
    (lower-cased) set `{stem, id, title, *aliases}`. For a source mirror
    `page_id` is empty (sources have no `kb.pages` row, and `index_manifest`
    stores `page_id` NULL) and `keys` is its single normalized `source_key`,
    which is what `mirror_citers` is queried by.
    """

    page_id: str
    keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class DirtySet:
    """The files to recompute plus the row ids/keys to delete this run.

    `dirty_paths` are parsed, resolved, validated and loaded. `deleted_page_ids` and
    `deleted_source_keys` are targeted deletes (vanished or renamed-away rows).
    """

    dirty_paths: frozenset[str]
    deleted_page_ids: frozenset[str]
    deleted_source_keys: frozenset[str]


def identity_hash(*, stem: str, id_: str, title: str, aliases: tuple[str, ...]) -> str:
    """The stable digest of a note's normalized resolution-key set.

    The keys `{stem, id, title, *aliases}` are stripped, lower-cased,
    de-duplicated, and sorted before hashing, so the digest is invariant to
    alias order, surrounding whitespace, and case, and blank keys are dropped.

    Args:
        stem: The note's filename stem (no extension).
        id_: The note's frontmatter `id`.
        title: The note's frontmatter `title`.
        aliases: The note's frontmatter `aliases`.

    Returns:
        The sha256 hex digest of the newline-joined normalized key set.
    """
    keys = {key.strip().lower() for key in (stem, id_, title, *aliases) if key.strip()}
    return content_hash("\n".join(sorted(keys)))


def compute_diff(stored: dict[str, ManifestEntry], current: dict[str, HashedFile]) -> ManifestDiff:
    """Partition the current files against the stored manifest by path and hash.

    A path present only in `current` is `added`, and one present only in `stored` is
    `removed`. For paths in both, a differing `content_hash` is `content_changed`
    (otherwise `unchanged`), and `identity_changed` is the subset of
    `content_changed` whose `identity_hash` also differs. Every output tuple is sorted.

    Args:
        stored: The prior index's manifest, keyed by path.
        current: This run's re-hashed files, keyed by path.

    Returns:
        The five-way `ManifestDiff`, with `identity_changed ⊆ content_changed`.
    """
    added = sorted(current.keys() - stored.keys())
    removed = sorted(stored.keys() - current.keys())

    content_changed: list[str] = []
    identity_changed: list[str] = []
    unchanged: list[str] = []
    for path in stored.keys() & current.keys():
        prior = stored[path]
        now = current[path]
        if prior.content_hash != now.content_hash:
            content_changed.append(path)
            if prior.identity_hash != now.identity_hash:
                identity_changed.append(path)
        else:
            unchanged.append(path)

    return ManifestDiff(
        added=tuple(added),
        removed=tuple(removed),
        content_changed=tuple(sorted(content_changed)),
        identity_changed=tuple(sorted(identity_changed)),
        unchanged=tuple(sorted(unchanged)),
    )


def _affected_names(
    diff: ManifestDiff, *, old_surfaces: dict[str, IdentitySurface], new_surfaces: dict[str, IdentitySurface]
) -> frozenset[str]:
    """The resolution keys that appeared or disappeared across added, removed and identity-changed paths."""
    empty: frozenset[str] = frozenset()
    affected: set[str] = set()
    for path in {*diff.identity_changed, *diff.added, *diff.removed}:
        old_keys = old_surfaces[path].keys if path in old_surfaces else empty
        new_keys = new_surfaces[path].keys if path in new_surfaces else empty
        affected |= old_keys ^ new_keys
    return frozenset(affected)


def compute_closure(
    diff: ManifestDiff,
    *,
    old_surfaces: dict[str, IdentitySurface],
    new_surfaces: dict[str, IdentitySurface],
    inbound_by_name: Callable[[frozenset[str]], frozenset[str]],
    mirror_citers: Callable[[frozenset[str]], frozenset[str]],
    path_of_page: Callable[[str], str | None],
) -> DirtySet:
    """Expand the raw diff into the minimal dirty set + targeted deletes.

    The dirty set is `reparse ∪ inbound-closure ∪ mirror-citers`:

    - `reparse` is `added ∪ content_changed` (files whose text changed).
    - the **inbound closure** is `inbound_by_name(affected_names)`: every page
      that references (resolved or dangling) a key that appeared or disappeared
      across the structural changes. It catches an add that now satisfies a
      dangling link and a delete or rename that strands a resolved one.
    - the **mirror citers** are `mirror_citers(changed_source_keys)`: every page
      that cites a source mirror whose text changed or vanished, so its quotes
      re-verify. Note keys passed in this query match no source.

    Deletes are targeted: a removed or renamed-away page id is dropped, as is a
    removed source's key. A page id that a re-parsed file produces again is kept.

    Args:
        diff: The raw `compute_diff` partition.
        old_surfaces: The prior index's identity surfaces, keyed by path.
        new_surfaces: This run's re-parsed identity surfaces (`added ∪ content_changed`).
        inbound_by_name: Maps `link_refs` names to the ids of the pages that reference them (the inbound closure).
        mirror_citers: Maps mirror source keys to the ids of the pages that cite them (quote re-verification).
        path_of_page: Maps a page id to its path this run, or `None` if it no longer exists.

    Returns:
        The `DirtySet` to recompute and the row ids/keys to delete.
    """
    reparse = frozenset({*diff.added, *diff.content_changed})

    affected = _affected_names(diff, old_surfaces=old_surfaces, new_surfaces=new_surfaces)
    closure_paths = _paths_for(inbound_by_name(affected), path_of_page=path_of_page)

    changed_or_removed = {*diff.content_changed, *diff.removed}
    changed_keys: set[str] = set()
    for path in changed_or_removed:
        surface = new_surfaces.get(path) or old_surfaces.get(path)
        if surface is not None:
            changed_keys |= surface.keys
    citer_paths = _paths_for(mirror_citers(frozenset(changed_keys)), path_of_page=path_of_page)

    dirty_paths = reparse | closure_paths | citer_paths

    deleted_page_ids = {old_surfaces[path].page_id for path in diff.removed if _is_note(old_surfaces.get(path))}
    deleted_page_ids |= {
        old_surfaces[path].page_id
        for path in diff.identity_changed
        if path in old_surfaces and path in new_surfaces and old_surfaces[path].page_id != new_surfaces[path].page_id
    }
    # A page id a re-parsed file produces again is an update, not a delete. A rename that keeps the
    # frontmatter `id` removes the old path and adds the new one, and the new path upserts the same
    # row. Only ids that truly vanished are deleted.
    reupserted_ids = {surface.page_id for surface in new_surfaces.values() if surface.page_id}
    deleted_page_ids -= reupserted_ids

    deleted_source_keys: set[str] = set()
    for path in diff.removed:
        surface = old_surfaces.get(path)
        if surface is not None and not _is_note(surface):
            deleted_source_keys |= surface.keys

    return DirtySet(
        dirty_paths=dirty_paths,
        deleted_page_ids=frozenset(deleted_page_ids),
        deleted_source_keys=frozenset(deleted_source_keys),
    )


def _is_note(surface: IdentitySurface | None) -> bool:
    """Whether a surface is a note with a `kb.pages` id rather than a source mirror."""
    return surface is not None and bool(surface.page_id)


def _paths_for(page_ids: frozenset[str], *, path_of_page: Callable[[str], str | None]) -> frozenset[str]:
    """Map page ids to their current paths, dropping ids with no path this run."""
    return frozenset(path for page_id in page_ids if (path := path_of_page(page_id)) is not None)
