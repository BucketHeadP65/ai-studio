"""The sync use case: converge a knowledge base's mirrors to each connector's library on the command bus.

The connector is the source of truth. Mirrors are disposable copies whose only mutable state is
each note's frontmatter. A handler drives the connector, the converter and the mirror store
through their ports, collects per-item failures into the `SyncReport` without aborting the run,
and plans the same decisions with no writes or conversions on a dry run. It exposes an `events`
list the bus drains after it returns, so `SyncStarted` and `SyncCompleted` subscribers observe
the run's already-committed state.
"""

from __future__ import annotations

import hashlib

from collections import Counter
from typing import TYPE_CHECKING

from core.cqrs.contracts import narrow_command, utcnow
from core.observability import get_logger

from engram.sources.domain.mirror import MirrorStatus
from engram.sources.domain.source_queue import source_identities
from engram.sources.domain.sync import (
    PlannedAction,
    SyncAction,
    SyncCompleted,
    SyncError,
    SyncMirrors,
    SyncReport,
    SyncStarted,
    build_frontmatter,
    decide,
    full_run_archives,
    holder_of,
    mirrors_by_identity,
    should_archive,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from pathlib import Path

    from core.cqrs.contracts import Command, Event

    from engram.sources.domain.mirror import MirrorFile
    from engram.sources.domain.source_item import SourceItem
    from engram.sources.internal_contracts import Connector, DocumentConverter, MirrorStore, SyncHandler

_logger = get_logger(__name__)

# The dry-run title rendered for an orphaned mirror (its item no longer exists at the connector).
_ORPHAN_TITLE = "(removed at its source)"

# Per-item actions (every action except DELETE, which the orphan pass tallies separately)
# -> the report counter they bump. RECONVERT and REFRESH both count as an update.
_COUNTER_FIELD: dict[SyncAction, str] = {
    SyncAction.CREATE: "created",
    SyncAction.RECONVERT: "updated",
    SyncAction.REFRESH: "updated",
    SyncAction.SKIP: "skipped",
}


def pdf_sha256(path: Path) -> str:
    """Hex `sha256` digest of the bytes at `path` (the mirror's source hash).

    Args:
        path: The PDF the connector handed over.

    Returns:
        The hex digest.

    Raises:
        OSError: If the file is missing or unreadable, the caller collects the
            item into the report's `unavailable` list.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hold(held: dict[str, str], item: SourceItem) -> None:
    """Record that `item`'s own mirror now holds every identifier its source carries, keeping an earlier holder."""
    for identity in source_identities(url=item.url, doi=item.doi):
        held.setdefault(identity, item.provider_key)


class SyncMirrorsHandler:
    """Handles `SyncMirrors`: converge the knowledge base's mirrors to one `Connector` library."""

    def __init__(
        self,
        connector: Connector,
        converter: DocumentConverter,
        store: MirrorStore,
        *,
        clock: Callable[[], datetime] = utcnow,
        max_age_years: int | None = None,
    ) -> None:
        """Hold the ports and the knowledge base ingestion policy's age rule.

        Args:
            connector: The connector (every non-trashed item bearing a PDF).
            converter: The PDF to Markdown converter, invoked only for CREATE and RECONVERT (and a
                body-less REFRESH), never on a dry run.
            store: The mirror store (read index and body, write, soft-delete).
            clock: Timezone-aware "now" source stamped into written frontmatter.
            max_age_years: Refuses at admission an item whose year is older than the current year
                minus the value and that has no live mirror yet, reporting it under `too_old`. An
                unknown year passes, an item already mirrored keeps converging, and `None` applies
                no age rule.
        """
        self._connector = connector
        self._converter = converter
        self._store = store
        self._clock = clock
        self._max_age_years = max_age_years
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> SyncReport:
        """Bus entry point, narrowing `command` to the `SyncMirrors` this handler is registered for, so a misregistration fails loudly.

        Args:
            command: The command to handle, which must be a `SyncMirrors`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, SyncMirrors, handler="SyncMirrorsHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: SyncMirrors) -> SyncReport:
        """Run one sync pass (dry or applying) and return its `SyncReport`.

        A `limit` slices the item list to a partial backfill and disables orphan archiving. The dry
        run mirrors the applying run exactly, so its plan is a faithful preview of what an applying
        run would do. An item with no live mirror of its own whose source another active mirror
        already holds (any identifier `source_identities` names) is never converted or written and lands in
        `already_mirrored`, and so does a second item of one source within the run. In an applying run,
        an item with a missing or unreadable PDF lands in
        `unavailable`, any other per-item failure lands in `errors`, and neither aborts the run. A
        failing orphan soft-delete is appended to `errors` and never aborts the pass. A REFRESH
        that reuses the stored body keeps the mirror's existing converter label and writes no
        assets.

        Args:
            command: The sync command.

        Returns:
            The run's report.
        """
        self._emit(SyncStarted())
        items = await self._connector.list_items_with_pdf()
        items = items[: command.limit]
        archives_orphans = full_run_archives(command)
        if command.dry_run:
            report = await self._plan(items, include_orphans=archives_orphans)
        else:
            report = await self._apply(items, archive_orphans=archives_orphans)
        self._emit(SyncCompleted(report=report))
        return report

    def _refused_by_age(self, item: SourceItem, *, existing: MirrorFile | None, now: datetime) -> bool:
        """True when the age rule refuses `item` at admission: a known year older than the rule allows and no live mirror."""
        if self._max_age_years is None or item.year is None:
            return False
        if existing is not None and existing.status is not MirrorStatus.REMOVED:
            return False
        refused = item.year < now.year - self._max_age_years
        if refused:
            _logger.info(
                "Item {item} ({year}) is older than max_age_years={max_age_years}, not admitted",
                item=item.provider_key,
                year=item.year,
                max_age_years=self._max_age_years,
            )
        return refused

    def _mine(self, index: Mapping[str, MirrorFile]) -> dict[str, MirrorFile]:
        """The mirrors this handler's connector owns. Another connector's mirrors are never its orphans."""
        return {key: mirror for key, mirror in index.items() if mirror.provider == self._connector.name}

    # -- applying run ----------------------------------------------------------

    async def _apply(self, items: list[SourceItem], *, archive_orphans: bool) -> SyncReport:
        """Converge mirrors to `items`, idempotent and per-item fault-tolerant, then archive orphans when asked."""
        index = await self._store.load_index()
        held = mirrors_by_identity(index)
        now = self._clock()
        actions: list[SyncAction] = []
        errors: list[SyncError] = []
        unavailable: list[str] = []
        too_old: list[str] = []
        already_mirrored: dict[str, str] = {}
        for item in items:
            if self._refused_by_age(item, existing=index.get(item.provider_key), now=now):
                too_old.append(item.provider_key)
                continue
            if (holder := holder_of(item, index.get(item.provider_key), held)) is not None:
                already_mirrored[item.provider_key] = holder
                continue
            try:
                pdf_hash = pdf_sha256(item.pdf_path)
            except OSError:
                _logger.warning("No readable PDF for item {item} at {path}", item=item.provider_key, path=str(item.pdf_path))
                unavailable.append(item.provider_key)
                continue
            try:
                actions.append(await self._apply_one(item, existing=index.get(item.provider_key), pdf_hash=pdf_hash, now=now))
                _hold(held, item)
            except Exception as error:
                _logger.exception("Sync failed for item {item}", item=item.provider_key)
                errors.append(SyncError(key=item.provider_key, message=str(error)))
        archived = 0
        if archive_orphans:
            archived = await self._archive_orphans(index, present_keys={item.provider_key for item in items}, errors=errors)
        counts = Counter(_COUNTER_FIELD[action] for action in actions)
        return SyncReport(
            too_old=tuple(too_old),
            already_mirrored=already_mirrored,
            created=counts["created"],
            updated=counts["updated"],
            archived=archived,
            skipped=counts["skipped"],
            errors=tuple(errors),
            unavailable=tuple(unavailable),
        )

    async def _apply_one(self, item: SourceItem, *, existing: MirrorFile | None, pdf_hash: str, now: datetime) -> SyncAction:
        """Apply the decided action for a single item and return the action taken."""
        action = decide(item, existing, pdf_hash)
        if action is SyncAction.SKIP:
            return action
        body, converter_label, assets = await self._resolve_body(item, action, existing)
        frontmatter = build_frontmatter(
            item,
            provider=self._connector.name,
            source_hash=pdf_hash,
            status=MirrorStatus.ACTIVE,
            synced=now,
            converter=converter_label,
        )
        await self._store.write(frontmatter=frontmatter, body=body, assets=assets)
        return action

    async def _resolve_body(self, item: SourceItem, action: SyncAction, existing: MirrorFile | None) -> tuple[str, str, dict[str, bytes]]:
        """The `(body, converter_label, assets)` for a write: the stored body on a REFRESH that has one, else a fresh conversion."""
        if action is SyncAction.REFRESH:
            body = await self._store.read_body(item.provider_key)
            if body is not None:
                return body, existing.converter if existing is not None else "unknown", {}
        result = await self._converter.to_markdown(item.pdf_path, source_url=item.url)
        return result.markdown, result.converter, result.assets

    async def _archive_orphans(self, index: dict[str, MirrorFile], *, present_keys: set[str], errors: list[SyncError]) -> int:
        """Soft-delete every active mirror this handler's connector owns whose item left its library, and count them."""
        archived = 0
        for key, mirror in self._mine(index).items():
            if not should_archive(mirror, present_keys=present_keys):
                continue
            try:
                archived += int(await self._store.soft_delete(key))
            except Exception as error:
                _logger.exception("Soft-delete failed for mirror {mirror}", mirror=key)
                errors.append(SyncError(key=key, message=str(error)))
        return archived

    # -- dry run ---------------------------------------------------------------

    async def _plan(self, items: list[SourceItem], *, include_orphans: bool) -> SyncReport:
        """Compute the per-item plan with zero writes or conversions (the dry run)."""
        index = await self._store.load_index()
        held = mirrors_by_identity(index)
        now = self._clock()
        planned: list[PlannedAction] = []
        unavailable: list[str] = []
        too_old: list[str] = []
        already_mirrored: dict[str, str] = {}
        for item in items:
            if self._refused_by_age(item, existing=index.get(item.provider_key), now=now):
                too_old.append(item.provider_key)
                continue
            if (holder := holder_of(item, index.get(item.provider_key), held)) is not None:
                already_mirrored[item.provider_key] = holder
                continue
            try:
                pdf_hash = pdf_sha256(item.pdf_path)
            except OSError:
                _logger.warning("No readable PDF for item {item} at {path}", item=item.provider_key, path=str(item.pdf_path))
                unavailable.append(item.provider_key)
                continue
            planned.append(
                PlannedAction(key=item.provider_key, title=item.title, action=decide(item, index.get(item.provider_key), pdf_hash))
            )
            _hold(held, item)
        if include_orphans:
            present_keys = {item.provider_key for item in items}
            planned.extend(
                PlannedAction(key=key, title=_ORPHAN_TITLE, action=SyncAction.DELETE)
                for key, mirror in self._mine(index).items()
                if should_archive(mirror, present_keys=present_keys)
            )
        return SyncReport(
            dry_run=True, planned=tuple(planned), unavailable=tuple(unavailable), too_old=tuple(too_old), already_mirrored=already_mirrored
        )

    def _emit(self, event: Event) -> None:
        """Queue `event` for the bus to dispatch after this handler returns."""
        self.events.append(event)


class MultiConnectorSync:
    """Runs the declared connectors' sync handlers in declaration order and merges their reports.

    The bus drains events off the handler it dispatched, which is this one, so each connector's
    events are moved here as it finishes and every connector's `SyncStarted` and `SyncCompleted`
    reaches the subscribers.
    """

    def __init__(self, handlers: Mapping[str, SyncHandler]) -> None:
        """Store the handlers by connector name, in declaration order.

        Args:
            handlers: One sync handler per declared connector, by connector name.
        """
        self._handlers = handlers
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> SyncReport:
        """Bus entry point, narrowing `command` to the `SyncMirrors` this handler is registered for, so a misregistration fails loudly.

        Args:
            command: The command to handle, which must be a `SyncMirrors`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, SyncMirrors, handler="MultiConnectorSync")
        return await self.handle(command)

    async def handle(self, command: SyncMirrors) -> SyncReport:
        """Run every handler with the same command and fold the reports, carrying their events along.

        A handler that raises contributes one `SyncError` naming its connector and the
        exception, instead of aborting the run, so every other connector still runs and
        its own report still lands in the merge.

        Args:
            command: The sync command every handler runs.

        Returns:
            The merged report.
        """
        reports = [await self._run_one(name, handler, command) for name, handler in self._handlers.items()]
        return SyncReport(
            created=sum(r.created for r in reports),
            updated=sum(r.updated for r in reports),
            archived=sum(r.archived for r in reports),
            skipped=sum(r.skipped for r in reports),
            errors=tuple(e for r in reports for e in r.errors),
            unavailable=tuple(u for r in reports for u in r.unavailable),
            too_old=tuple(k for r in reports for k in r.too_old),
            already_mirrored={key: holder for r in reports for key, holder in r.already_mirrored.items()},
            dry_run=command.dry_run,
            planned=tuple(p for r in reports for p in r.planned),
            created_by_connector={name: report.created for name, report in zip(self._handlers, reports, strict=True) if report.created},
        )

    async def _run_one(self, name: str, handler: SyncHandler, command: SyncMirrors) -> SyncReport:
        """Run one connector's handler, folding a raised exception into a `SyncError` and moving its events here either way."""
        try:
            return await handler.handle(command)
        except Exception as error:
            _logger.exception("Sync failed for connector {connector}", connector=name)
            return SyncReport(errors=(SyncError(key=name, message=f"connector {name} failed: {error}"),))
        finally:
            self.events.extend(handler.events)
            handler.events.clear()
