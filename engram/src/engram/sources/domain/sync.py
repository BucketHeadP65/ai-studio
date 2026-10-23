"""The sync command, its events, the run report, and the pure decisions a sync run makes.

`SyncMirrors` is the one sync command, and `SyncStarted` and `SyncCompleted` are the events a run
emits around itself. `SyncReport` is the run summary. `decide` picks what to do with one item,
`holder_of` names the mirror that already holds an item's source, `build_frontmatter` maps an item
onto a mirror's frontmatter, and the orphan-archival rule decides which mirrors a run archives.
Nothing here does I/O.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from core.cqrs.contracts import Command, Event
from pydantic import BaseModel, ConfigDict, Field

from engram.sources.domain.mirror import MirrorFrontmatter, MirrorStatus
from engram.sources.domain.source_queue import source_identities

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from engram.sources.domain.mirror import MirrorFile
    from engram.sources.domain.source_item import SourceItem


class SyncAction(StrEnum):
    """What sync should do with a single connector item this run."""

    CREATE = "create"  # no mirror yet (or it was removed) -> convert + write
    RECONVERT = "reconvert"  # PDF bytes changed (source_hash differs) -> convert + write
    REFRESH = "refresh"  # only metadata changed (version differs) -> rewrite frontmatter, keep body
    SKIP = "skip"  # version + hash unchanged
    DELETE = "delete"  # mirror exists but the item is gone from the connector -> soft-delete


class SyncMirrors(Command):
    """Converge the knowledge base's `Sources/` mirrors to the connector's library.

    `dry_run` plans without writing or converting. `limit` processes at most N items and disables
    orphan archiving, because a partial view must never archive the rest of the library.
    """

    dry_run: bool = False
    limit: int | None = None


class SyncError(BaseModel):
    """One item that failed during a sync run, captured so the run can continue."""

    model_config = ConfigDict(frozen=True)

    key: str
    message: str


class PlannedAction(BaseModel):
    """One dry-run row: the action an applying run would take for an item (or orphaned mirror)."""

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    action: SyncAction


class SyncReport(BaseModel):
    """Tally of a sync run: per-action counters, per-item failures, and the dry-run plan."""

    model_config = ConfigDict(frozen=True)

    created: int = 0
    updated: int = 0  # reconverted (PDF bytes changed) + refreshed (metadata only)
    archived: int = 0
    skipped: int = 0
    errors: tuple[SyncError, ...] = ()
    unavailable: tuple[str, ...] = ()  # items whose attachment path could not resolve
    too_old: tuple[str, ...] = ()  # items refused at admission by the knowledge base policy's max_age_years
    already_mirrored: dict[str, str] = Field(default_factory=dict)
    """The items refused at admission because another active mirror already holds their source, each item's key mapped to that mirror's key."""
    dry_run: bool = False
    planned: tuple[PlannedAction, ...] = ()  # populated only when dry_run
    created_by_connector: dict[str, int] = Field(default_factory=dict)
    """The mirrors each connector created, by connector name. Only the fold over several connectors fills it, and a connector that created none is absent."""


class SyncStarted(Event):
    """Emitted when a sync run begins."""


class SyncCompleted(Event):
    """Emitted after the run finishes, carrying the full report."""

    report: SyncReport


def decide(item: SourceItem, existing: MirrorFile | None, pdf_hash: str) -> SyncAction:
    """Pure decision for one item given its current mirror state and PDF hash.

    Args:
        item: The connector's item under consideration.
        existing: The item's current mirror state, or None if it has no mirror.
        pdf_hash: The freshly computed `sha256` of the item's PDF bytes.

    Returns:
        The action to take: `CREATE` when there is no live mirror, `RECONVERT`
        when the PDF bytes changed, `REFRESH` when only the metadata version
        changed, otherwise `SKIP`.
    """
    if existing is None or existing.status is MirrorStatus.REMOVED:
        return SyncAction.CREATE
    if pdf_hash != existing.source_hash:
        return SyncAction.RECONVERT
    if item.version != existing.source_version:
        return SyncAction.REFRESH
    return SyncAction.SKIP


def mirrors_by_identity(index: Mapping[str, MirrorFile]) -> dict[str, str]:
    """Each identifier an active mirror's source carries, mapped to that mirror's key. A removed mirror holds nothing.

    Args:
        index: The mirrors by key.

    Returns:
        The holding mirror's key by identifier.
    """
    held: dict[str, str] = {}
    for key, mirror in index.items():
        if mirror.status is MirrorStatus.ACTIVE:
            held.update(dict.fromkeys(source_identities(url=mirror.url, doi=mirror.doi), key))
    return held


def holder_of(item: SourceItem, existing: MirrorFile | None, held: Mapping[str, str]) -> str | None:
    """The key of another active mirror that already holds the source `item` names, or `None`.

    An item with a live mirror of its own is never refused, so it keeps converging.

    Args:
        item: The connector's item under consideration.
        existing: The item's current mirror state, or `None` if it has no mirror.
        held: The holding mirror's key by identifier, as `mirrors_by_identity` answers it.

    Returns:
        The holding mirror's key, or `None` when the item may be admitted.
    """
    if existing is not None and existing.status is MirrorStatus.ACTIVE:
        return None
    for identity in sorted(source_identities(url=item.url, doi=item.doi)):
        if (holder := held.get(identity)) is not None:
            return holder
    return None


def build_frontmatter(
    item: SourceItem, *, provider: str, source_hash: str, status: MirrorStatus, synced: datetime, converter: str
) -> MirrorFrontmatter:
    """Map a `SourceItem` onto the generic mirror frontmatter.

    Args:
        item: The connector's item.
        provider: The adapter's `Connector.name` value, the name a knowledge base declared.
        source_hash: The PDF's `sha256` digest to record.
        status: The lifecycle status to stamp (typically `ACTIVE`).
        synced: The run timestamp (the handler's injected clock, never `now()` here).
        converter: The provenance label of the converter that produced the body
            (a fresh conversion's label, or the preserved one on a body-reusing REFRESH).

    Returns:
        The frontmatter to write atop the mirror note. Authors render as
        `"Last, First"`, the PDF path is stringified for YAML. `source_key` and `source_version`
        carry the item's `provider_key` and `version` whatever the connector (the mirror store's
        own join key). `source_link` carries whatever deep link the item's own connector filled in
        (`SourceItem.source_link`), empty when its connector owns none. `channel` and `published`
        pass through whatever the item carries (`None` for a connector that has neither), and so
        does `meta` (empty for a connector that carries none).
    """
    return MirrorFrontmatter(
        title=item.title,
        authors=[f"{creator.last_name}, {creator.first_name}" for creator in item.creators],
        year=item.year,
        item_type=item.item_type,
        doi=item.doi,
        url=item.url,
        channel=item.channel,
        published=item.published,
        meta=item.meta,
        source_id=item.source_id,
        provider=provider,
        provider_key=item.provider_key,
        source_key=item.provider_key,
        source_version=item.version,
        source_link=item.source_link,
        collections=item.collections,
        keywords=item.tags,
        date_added=item.date_added,
        date_modified=item.date_modified,
        pdf_path=str(item.pdf_path),
        source_hash=source_hash,
        converter=converter,
        status=status,
        synced=synced,
    )


# -- the orphan-archival rule -------------------------------------------------
#
# A mirror is archived only when ALL FOUR conditions hold, co-located here so the
# rule reads at a glance rather than being stitched from several call sites:
#
# 1. The run is unlimited (`command.limit is None`). A `limit` slices the item list
#    to a partial backfill, so most of the library is intentionally absent this run
#    and archiving against it would soft-delete the whole knowledge base. Only a full
#    run holds the complete set of present keys needed to conclude a mirror is an orphan.
# 2. The mirror's `provider` field matches the handler's own connector name. Another
#    connector's mirrors are never this run's orphans.
# 3. The mirror is `ACTIVE`. An already-`REMOVED` mirror is not re-archived.
# 4. Its key is absent from `present_keys`. The item left the connector's library.
#
# `full_run_archives` is condition 1 (the run-level gate). `SyncMirrorsHandler._mine` is
# condition 2, it narrows the index to the handler's own connector before either pass
# iterates it. `should_archive` is conditions 3 + 4, the per-mirror test the applying pass
# and the dry-run plan share, so both decide identically. It is only consulted on a full
# run and only against mirrors the handler's own connector owns.


def full_run_archives(command: SyncMirrors) -> bool:
    """Whether this run archives orphans at all: only an unlimited (full) run does (condition 1).

    Args:
        command: The sync command.

    Returns:
        True when the command has no limit.
    """
    return command.limit is None


def should_archive(mirror: MirrorFile, *, present_keys: set[str]) -> bool:
    """Whether one mirror is an orphan to soft-delete: ACTIVE and gone from the connector's library (conditions 3 + 4).

    Args:
        mirror: The mirror to test.
        present_keys: The keys of the items the connector's library holds now.

    Returns:
        True when the mirror is active and its key is not in `present_keys`.
    """
    return mirror.status is MirrorStatus.ACTIVE and mirror.source_key not in present_keys
