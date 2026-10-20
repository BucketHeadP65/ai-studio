"""The refresh use cases: the handler a worker runs for one refresh job, and the ticker that queues the due ones.

A refresh pass preflights the schema, scans the watch list, syncs the mirrors, places new mirrors and indexes, through the knowledge base's own bus.
A clean pass answers a one-line detail. Any failure raises, and the worker turns that into a retry or an abandonment.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from core.cqrs.contracts import MessageBus
from core.observability import get_logger
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport
from engram.init.contracts import assert_schema_at_head
from engram.kb import contracts as kb
from engram.sources.contracts import ScanReport, ScanWatchList, SyncError, SyncMirrors, SyncReport

from studio.knowledge_base.refresh.domain import (
    REFRESH_KIND,
    REFRESH_POLICY,
    RefreshAbandoned,
    RefreshCompleted,
    RefreshCounts,
    RefreshResult,
    RefreshStarted,
    is_due,
)
from studio.knowledge_base.refresh.infrastructure.librarian import LibrarianPlacer, librarian_agent, new_mirrors_at
from studio.knowledge_base.settings import AzureOpenAiSettings, DatabaseSettings, IngestionSettings, KnowledgeBaseSettings, read_registry
from studio.knowledge_base.watch_list import WATCH_LIST_PATH

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from core.cqrs.contracts import JobContext, JobQueries, Outbox
    from core.wiring.contracts import Container

    from studio.knowledge_base.refresh.domain import RefreshJob
    from studio.knowledge_base.settings import KnowledgeBaseEntry

_logger = get_logger(__name__)

TICK_INTERVAL_SECONDS = 60.0
"""How often the worker runs a tick."""


def _top_level_mirror_names(sources_dir: Path) -> frozenset[str]:
    """Every `.md` file name at the top level of `sources_dir` right now."""
    return frozenset(path.name for path in sources_dir.glob("*.md")) if sources_dir.is_dir() else frozenset()


async def _run_librarian(container: Container, sources_dir: Path, before: frozenset[str]) -> None:
    """Place every mirror the sync just wrote at the top of `sources_dir`, logging and never raising any failure."""
    new_names = sorted(name for name in _top_level_mirror_names(sources_dir) if name not in before)
    if not new_names:
        return
    new_mirrors = new_mirrors_at(sources_dir, new_names)
    if not new_mirrors:
        return
    try:
        azure_openai = await container.get(AzureOpenAiSettings)
        ingestion = await container.get(IngestionSettings)
        placer = LibrarianPlacer(lambda: librarian_agent(azure_openai, ingestion))
        report = await placer.place(sources_dir, new_mirrors)
    except Exception as error:  # noqa: BLE001 (the librarian pass is best-effort, never fails the refresh)
        _logger.warning("librarian pass failed for knowledge base at {sources_dir}", error=error, sources_dir=str(sources_dir))
        return
    if report.left:
        _logger.info("librarian left {count} mirror(s) at the Sources/ top level for knowledge base at {sources_dir}", count=len(report.left), sources_dir=str(sources_dir))


def _sync_summary(report: SyncReport) -> str:
    """The sync part of a refresh's detail: the counters, then the too-old, already-mirrored and errored counts when there are any."""
    summary = f"synced {report.created} created/{report.updated} updated/{report.archived} archived"
    if report.too_old:
        summary += f"/{len(report.too_old)} too old"
    if report.already_mirrored:
        summary += f"/{len(report.already_mirrored)} already mirrored"
    return summary + (f"/{len(report.errors)} errored, " if report.errors else ", ")


def _counts_of(scan: ScanReport | None, sync: SyncReport) -> RefreshCounts:
    """What one attempt's scan and sync brought in."""
    return RefreshCounts(
        sources_created=sync.created,
        requested_by_entry=dict(scan.requested_by_entry) if scan is not None else {},
        created_by_connector=dict(sync.created_by_connector),
    )


async def _perform_refresh(
    entry: KnowledgeBaseEntry, container: Container, schema_preflight: Callable[[str], None], *, on_counts: Callable[[RefreshCounts], None]
) -> str:
    """Preflight, scan, sync, place and index one knowledge base, telling `on_counts` what the sync brought in before anything after it can raise."""
    schema = kb.load_knowledge_base_schema(entry.path)
    database = await container.get(DatabaseSettings)
    await asyncio.to_thread(schema_preflight, database.url)
    bus = await container.get(MessageBus)

    sync_detail = ""
    sync_errors: tuple[SyncError, ...] = ()
    scan_result: ScanReport | None = None
    if schema.sources.enabled and (entry.path / WATCH_LIST_PATH).is_file():
        scanned = await bus.handle(ScanWatchList())
        if not isinstance(scanned, ScanReport):  # pragma: no cover (the scan handler always returns a ScanReport)
            msg = "scan produced no report"
            raise TypeError(msg)
        scan_result = scanned
        sync_detail = f"scanned {scan_result.found} entries/{scan_result.requested} requested/{len(scan_result.failed)} entries failed, "
    if schema.sources.enabled:
        sources_dir: Path | None = None
        before_landing: frozenset[str] | None = None
        if schema.sources.librarian:
            sources_dir = (await container.get(KnowledgeBaseSettings)).sources_dir
            before_landing = _top_level_mirror_names(sources_dir)
        sync_result = await bus.handle(SyncMirrors())
        if not isinstance(sync_result, SyncReport):  # pragma: no cover (the sync handler always returns a SyncReport)
            msg = "sync produced no report"
            raise TypeError(msg)
        sync_errors = sync_result.errors
        sync_detail += _sync_summary(sync_result)
        on_counts(_counts_of(scan_result, sync_result))
        if sources_dir is not None and before_landing is not None:
            await _run_librarian(container, sources_dir, before_landing)

    index_result = await bus.handle(
        IndexKnowledgeBase(collection=schema.retrieval.collection, embed=True, full=False, dry_run=False, summaries=False)
    )
    if not isinstance(index_result, IndexReport):  # pragma: no cover (the index handler always returns a IndexReport)
        msg = "index produced no report"
        raise TypeError(msg)
    if index_result.validation.n_errors > 0:
        msg = f"index validation failed: {index_result.validation.n_errors} error(s)"
        raise RuntimeError(msg)
    indexed = f"{sync_detail}indexed {index_result.pages} pages, {index_result.embedded} embedded"
    if sync_errors:
        reasons = ", ".join(f"{error.key}: {error.message}" for error in sync_errors)
        msg = f"{indexed}, sync failed: {len(sync_errors)} error(s) ({reasons})"
        raise RuntimeError(msg)
    return indexed


class RefreshKnowledgeBaseJob:
    """The handler for one `kb.refresh` job: the whole refresh of one registered knowledge base.

    Each call re-reads the registry, so a knowledge base registered or moved since the worker started is refreshed.
    Each call builds its own container and disposes it before it returns.
    The counts a completed or abandoned pass announces add up every attempt this process ran for the job, so sources an earlier
    failed attempt created are still announced. An attempt a previous worker process ran is not counted.
    """

    def __init__(
        self,
        *,
        settings_path: Path,
        containers: Callable[[Path], Container],
        dispose: Callable[[Container], Awaitable[None]],
        schema_preflight: Callable[[str], None] | None = None,
    ) -> None:
        """Hold the registry file and the collaborators one pass works through.

        Args:
            settings_path: The settings file whose registry of knowledge bases every pass reads.
            containers: Builds the container one pass works through, given the knowledge base's folder.
            dispose: Releases a container once its pass is over.
            schema_preflight: Checks the database is at the migration head, given its URL, before anything is dispatched.
                `None` uses `engram.init.contracts.assert_schema_at_head`.
        """
        self._settings_path = settings_path
        self._containers = containers
        self._dispose = dispose
        self._preflight = schema_preflight if schema_preflight is not None else assert_schema_at_head
        self._carried: dict[int, RefreshCounts] = {}

    async def __call__(self, payload: RefreshJob, context: JobContext) -> str:
        """Refresh the knowledge base the payload names, announcing the pass on its bus.

        The pass announces `RefreshStarted` before its first command and `RefreshCompleted` when it ends clean.
        A failure on the last attempt announces `RefreshAbandoned` before it raises. An earlier failure announces nothing.

        Args:
            payload: Which knowledge base to refresh, and what asked for it.
            context: The job's id and which attempt this is.

        Returns:
            The clean pass's detail and counts, as the one JSON line `refresh_history` reads back.

        Raises:
            LookupError: When the payload names a knowledge base the registry does not carry.
        """
        registry, _broken = read_registry(self._settings_path)
        entry = registry.get(payload.knowledge_base)
        if entry is None:
            msg = f"no knowledge base named {payload.knowledge_base!r} is registered"
            raise LookupError(msg)
        _logger.info(
            "refresh job {job_id} attempt {attempt} for knowledge base {knowledge_base!r} ({trigger})",
            job_id=str(context.job_id),
            attempt=context.attempt,
            knowledge_base=payload.knowledge_base,
            trigger=str(payload.trigger),
        )
        container = self._containers(entry.path)
        try:
            outbox = (await container.get(MessageBus)).outbox
            await outbox.append([RefreshStarted(knowledge_base=payload.knowledge_base, job_id=context.job_id, trigger=payload.trigger)])
            detail = await self._refresh(entry, container, payload, context, outbox=outbox)
            counts = self._carried.get(context.job_id, RefreshCounts())
            await outbox.append(
                [
                    RefreshCompleted(
                        knowledge_base=payload.knowledge_base, job_id=context.job_id, trigger=payload.trigger, detail=detail, counts=counts
                    )
                ]
            )
            self._carried.pop(context.job_id, None)
            return RefreshResult(detail=detail, counts=counts).model_dump_json()
        finally:
            await self._dispose(container)

    async def _refresh(
        self, entry: KnowledgeBaseEntry, container: Container, payload: RefreshJob, context: JobContext, *, outbox: Outbox
    ) -> str:
        """Run the pass, carrying its counts across the job's attempts and announcing `RefreshAbandoned` when this attempt is the last one."""

        def carry(counts: RefreshCounts) -> None:
            self._carried[context.job_id] = self._carried.get(context.job_id, RefreshCounts()).plus(counts)

        try:
            return await _perform_refresh(entry, container, self._preflight, on_counts=carry)
        except Exception as error:
            if context.attempt >= REFRESH_POLICY.max_attempts:
                counts = self._carried.get(context.job_id, RefreshCounts())
                await outbox.append(
                    [
                        RefreshAbandoned(
                            knowledge_base=payload.knowledge_base,
                            job_id=context.job_id,
                            trigger=payload.trigger,
                            reason=str(error),
                            counts=counts,
                        )
                    ]
                )
                self._carried.pop(context.job_id, None)
            raise


class Ticker:
    """Queues a refresh for every registered knowledge base whose refresh cadence has come due.

    It holds no overlap guard of its own. The jobs table refuses a second live job for the same knowledge base.
    """

    def __init__(
        self,
        *,
        settings_path: Path,
        jobs: JobQueries,
        run_one: Callable[[str, KnowledgeBaseEntry], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Hold the registry file, the job history, what to do with a due knowledge base, and the clock.

        Args:
            settings_path: The settings file whose registry of knowledge bases every tick reads.
            jobs: The job history each knowledge base's last refresh start is read from.
            run_one: Queues one knowledge base's refresh, given its registry name and its registered knowledge base.
            clock: Returns the current time. Defaults to `datetime.now(UTC)`.
        """
        self._settings_path = settings_path
        self._jobs = jobs
        self._run_one = run_one
        self._clock = clock

    async def _last_run_started_at(self, name: str) -> datetime | None:
        """When the knowledge base's most recent refresh job started or was queued, `None` when it has none."""
        history = await self._jobs.recent_jobs(REFRESH_KIND, name, limit=1)
        if not history:
            return None
        return history[0].started_at or history[0].created_at

    async def tick(self) -> None:
        """Check every registered knowledge base's refresh cadence once, queuing each one that is due.

        Every tick reads the registry again, so a changed cadence counts from the next tick.
        A registry that cannot be read, or one knowledge base whose refresh history cannot be read or whose refresh fails to
        queue, is logged and never stops the tick.
        """
        try:
            registry, _broken = read_registry(self._settings_path)
        except (OSError, ValueError) as exc:
            _logger.warning("the refresh tick could not read the registry at {path}: {reason}", path=str(self._settings_path), reason=str(exc))
            return
        now = self._clock()
        for name, entry in registry.items():
            if entry.schedule is None:
                continue
            try:
                if is_due(entry.schedule, await self._last_run_started_at(name), now):
                    await self._run_one(name, entry)
            except Exception:  # one knowledge base's scheduler failure must never break the loop or the rest of this tick
                _logger.exception("the scheduled refresh failed to fire for knowledge base {knowledge_base!r}", knowledge_base=name)
