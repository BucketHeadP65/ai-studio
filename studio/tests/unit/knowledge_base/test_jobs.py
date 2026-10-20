"""The refresh job: the pipeline it runs, the facts it announces, and what it answers.

`RefreshKnowledgeBaseJob` is what the worker runs for one `kb.refresh` job. It resolves the knowledge base from
the settings registry, preflights the schema head, then dispatches scan, sync and index through
that knowledge base's own bus, placing newly landed mirrors in between when the schema asks for it. A clean
pass answers a one-line detail, and every failure raises, which is what turns into a retry or an abandonment.
"""

from __future__ import annotations

import json

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

from core.cqrs.contracts import JobContext, MemoryOutbox, MessageBus
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport, ValidationIssue, ValidationReport
from engram.init.contracts import SchemaBehindHeadError
from engram.sources.contracts import ScanReport, ScanWatchList, SyncError, SyncMirrors, SyncReport

from studio.knowledge_base.container import dispose_container
from studio.knowledge_base.refresh.contracts import (
    REFRESH_KIND,
    REFRESH_POLICY,
    RefreshAbandoned,
    RefreshCompleted,
    RefreshCounts,
    RefreshJob,
    RefreshKnowledgeBaseJob,
    RefreshResult,
    RefreshStarted,
)
from studio.knowledge_base.settings import AzureOpenAiSettings, DatabaseSettings, IngestionSettings, KnowledgeBaseSettings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import JsonValue

CONTEXT = JobContext(job_id=1, kind=REFRESH_KIND, key="demo", attempt=1)


def _one_validation_error() -> ValidationReport:
    """A validation report carrying one error, which is what makes an index a failed run."""
    issue = ValidationIssue(severity="error", rule="missing-quote", page_id="paper.x", message="claim is missing the required quote")
    return ValidationReport(errors=(issue,))


def _knowledge_base(root: Path) -> Path:
    """A minimal knowledge base tree satisfying `KnowledgeBaseEntry.path` validation."""
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    return root


def _spec_payload(slug: str) -> dict[str, JsonValue]:
    """The smallest `KnowledgeBaseSchema` body that validates: one page type and an explicit collection."""
    return {"slug": slug, "page_types": [{"page_type": "concept", "id_prefix": "c-"}], "retrieval": {"collection": slug}}


def _plain_knowledge_base(root: Path, slug: str) -> Path:
    """A knowledge base directory carrying its own schema and declaring no source layer."""
    _knowledge_base(root)
    (root / "knowledge-base-schema.json").write_text(json.dumps(_spec_payload(slug)), encoding="utf-8")
    return root


def _youtube_knowledge_base(root: Path, slug: str, *, librarian: bool = False) -> Path:
    """A knowledge base whose schema declares the YouTube connector."""
    _knowledge_base(root)
    payload = _spec_payload(slug)
    payload["sources"] = {
        "enabled": True,
        "connectors": ["youtube"],
        "librarian": librarian,
        "key_field": "zotero_key",
        "digest_link_field": "digest_of",
        "hash_field": "source_hash",
        "digest_types": {"video": "concept"},
    }
    (root / "knowledge-base-schema.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _write_settings_json(path: Path, knowledge_base_path: Path, knowledge_bases: dict[str, dict[str, JsonValue]]) -> None:
    """A real settings JSON file on disk — the job re-reads the registry from it on every run."""
    data = {
        "knowledge_base": {"path": str(knowledge_base_path)},
        "knowledge_bases": knowledge_bases,
        "infrastructure": {
            "database": {"url": "postgresql+asyncpg://knowledge_bases@db/knowledge_bases", "reader_url": "postgresql+asyncpg://kb_reader@db/knowledge_bases"},
            "azure_openai": {"endpoint": "https://example.openai.azure.com/"},
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _register(tmp_path: Path, name: str, knowledge_base_path: Path) -> Path:
    """Register `knowledge_base_path` under `name` and return the settings file's path."""
    settings_path = tmp_path / "application_settings.json"
    _write_settings_json(settings_path, knowledge_base_path, {name: {"path": str(knowledge_base_path), "description": ""}})
    return settings_path


def _fake_database_settings() -> DatabaseSettings:
    """A `DatabaseSettings` good enough for the job's `container.get` resolution."""
    return DatabaseSettings(url="postgresql+asyncpg://knowledge_bases@db/knowledge_bases", reader_url="postgresql+asyncpg://kb_reader@db/knowledge_bases")


def _no_op_preflight(_db_url: str) -> None:
    """A schema preflight that always passes."""


def _land_a_mirror(knowledge_base_path: Path) -> Callable[[], None]:
    """An `on_sync` side effect: write one mirror at the `Sources/` top level, as a real sync would."""

    def land() -> None:
        sources_dir = knowledge_base_path / "Sources"
        sources_dir.mkdir(exist_ok=True)
        (sources_dir / "New Mirror.md").write_text("---\ntitle: New Mirror\nitem_type: video\n---\nBody.\n", encoding="utf-8")

    return land


@dataclass
class FakeMessageBus:
    """Fakes `MessageBus.handle` for the job's scan / sync / index dispatch.

    `handled` records every dispatched command in order, and `outbox` holds the facts the pass
    announces. `sync_error` and `index_error` script a pipeline failure without a real knowledge base
    or database, and `on_sync` runs right before a successful `SyncMirrors` answers, the seam a test
    uses to actually land a mirror on disk.
    """

    handled: list[object] = field(default_factory=list)
    outbox: MemoryOutbox = field(default_factory=MemoryOutbox)
    scan_report: ScanReport = field(default_factory=ScanReport)
    sync_report: SyncReport = field(default_factory=SyncReport)
    sync_error: Exception | None = None
    index_report: IndexReport = field(default_factory=lambda: IndexReport(pages=3, embedded=3))
    index_error: Exception | None = None
    on_sync: Callable[[], None] | None = None

    async def handle(self, command: object) -> object:
        self.handled.append(command)
        if isinstance(command, ScanWatchList):
            return self.scan_report
        if isinstance(command, SyncMirrors):
            if self.sync_error is not None:
                raise self.sync_error
            if self.on_sync is not None:
                self.on_sync()
            return self.sync_report
        if isinstance(command, IndexKnowledgeBase):
            if self.index_error is not None:
                raise self.index_error
            return self.index_report
        msg = f"FakeMessageBus has no canned response for {command!r}"
        raise KeyError(msg)


@dataclass
class FakeRunContainer:
    """The short-lived per-knowledge_base container the job resolves its collaborators from.

    `closed` proves the job disposes it after every run, win or lose. `knowledge_base_settings` / `azure_openai`
    / `ingestion` are what the librarian pass resolves; left `None` they are simply not provided,
    which is the shape of a knowledge base that never turns the librarian on.
    """

    bus: FakeMessageBus = field(default_factory=FakeMessageBus)
    database: DatabaseSettings = field(default_factory=_fake_database_settings)
    knowledge_base_settings: KnowledgeBaseSettings | None = None
    azure_openai: AzureOpenAiSettings | None = None
    ingestion: IngestionSettings | None = None
    closed: bool = False

    async def get[T](self, klass: type[T]) -> T:
        if klass is MessageBus:
            return cast("T", self.bus)
        if klass is DatabaseSettings:
            return cast("T", self.database)
        if klass is KnowledgeBaseSettings and self.knowledge_base_settings is not None:
            return cast("T", self.knowledge_base_settings)
        if klass is AzureOpenAiSettings and self.azure_openai is not None:
            return cast("T", self.azure_openai)
        if klass is IngestionSettings and self.ingestion is not None:
            return cast("T", self.ingestion)
        msg = f"FakeRunContainer has no registration for {klass!r}"
        raise KeyError(msg)

    async def close(self) -> None:
        self.closed = True


def _job(settings_path: Path, container: FakeRunContainer, *, preflight: Callable[[str], None] | None = None) -> RefreshKnowledgeBaseJob:
    """The job under test, always handed the same fake container whatever knowledge base it asks for."""
    return RefreshKnowledgeBaseJob(
        settings_path=settings_path,
        containers=lambda _path: container,
        dispose=dispose_container,
        schema_preflight=preflight if preflight is not None else _no_op_preflight,
    )


class TestPolicy:
    def test_the_kind_and_the_policy_are_the_declared_ones(self):
        assert REFRESH_KIND == "kb.refresh"
        assert (REFRESH_POLICY.max_attempts, REFRESH_POLICY.lease_seconds) == (3, 1800)
        assert (REFRESH_POLICY.backoff_base_seconds, REFRESH_POLICY.backoff_cap_seconds) == (30.0, 600.0)

    def test_the_payload_defaults_to_a_manual_trigger(self):
        assert RefreshJob(knowledge_base="demo").trigger == "manual"


class TestPipeline:
    async def test_indexes_only_when_the_schema_declares_no_connector(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer()

        returned = await _job(settings_path, container)(RefreshJob(knowledge_base="demo"), CONTEXT)

        assert [type(command).__name__ for command in container.bus.handled] == ["IndexKnowledgeBase"]
        assert RefreshResult.model_validate_json(returned).detail == "indexed 3 pages, 3 embedded"
        assert container.closed is True

    async def test_syncs_then_indexes_when_the_schema_declares_a_connector(self, tmp_path: Path):
        settings_path = _register(tmp_path, "tube", _youtube_knowledge_base(tmp_path / "tube", "tube"))
        container = FakeRunContainer()

        await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        assert [type(command).__name__ for command in container.bus.handled] == ["SyncMirrors", "IndexKnowledgeBase"]

    async def test_scans_the_watch_list_first_when_the_knowledge_base_has_one(self, tmp_path: Path):
        knowledge_base_path = _youtube_knowledge_base(tmp_path / "tube", "tube")
        (knowledge_base_path / "_meta" / "watch-list.yaml").write_text("[]", encoding="utf-8")
        settings_path = _register(tmp_path, "tube", knowledge_base_path)
        container = FakeRunContainer()

        await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        assert [type(command).__name__ for command in container.bus.handled] == ["ScanWatchList", "SyncMirrors", "IndexKnowledgeBase"]

    async def test_a_connector_error_still_indexes_and_then_raises(self, tmp_path: Path):
        settings_path = _register(tmp_path, "tube", _youtube_knowledge_base(tmp_path / "tube", "tube"))
        container = FakeRunContainer(
            bus=FakeMessageBus(sync_report=SyncReport(errors=(SyncError(key="ABC", message="404 from the connector"),)))
        )

        with pytest.raises(RuntimeError, match="404 from the connector"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        assert [type(command).__name__ for command in container.bus.handled] == ["SyncMirrors", "IndexKnowledgeBase"]

    async def test_items_refused_by_the_age_rule_are_counted_in_the_detail(self, tmp_path: Path):
        settings_path = _register(tmp_path, "tube", _youtube_knowledge_base(tmp_path / "tube", "tube"))
        container = FakeRunContainer(bus=FakeMessageBus(sync_report=SyncReport(too_old=("OLD00001",))))

        returned = await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        assert "1 too old" in RefreshResult.model_validate_json(returned).detail

    async def test_a_validation_error_raises(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer(bus=FakeMessageBus(index_report=IndexReport(pages=1, validation=_one_validation_error())))

        with pytest.raises(RuntimeError, match="index validation failed"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="demo"), CONTEXT)

    async def test_the_container_is_disposed_even_when_the_run_raises(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer(bus=FakeMessageBus(index_error=RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="demo"), CONTEXT)

        assert container.closed is True

    async def test_an_unregistered_knowledge_base_raises_naming_it(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer()

        with pytest.raises(LookupError, match="ghost"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="ghost"), CONTEXT)

        assert container.bus.handled == []


class TestSchemaPreflight:
    async def test_a_database_behind_head_raises_before_any_dispatch(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer()

        def behind(_db_url: str) -> None:
            msg = "knowledge base DB is at revision 0033, but the code expects head 0034"
            raise SchemaBehindHeadError(msg)

        with pytest.raises(SchemaBehindHeadError):
            await _job(settings_path, container, preflight=behind)(RefreshJob(knowledge_base="demo"), CONTEXT)

        assert container.bus.handled == []

    async def test_the_preflight_is_handed_the_containers_database_url(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer()
        seen: list[str] = []

        await _job(settings_path, container, preflight=seen.append)(RefreshJob(knowledge_base="demo"), CONTEXT)

        assert seen == [container.database.url]


class TestLibrarian:
    async def test_off_by_default_a_new_mirror_stays_at_the_top_level(self, tmp_path: Path):
        knowledge_base_path = _youtube_knowledge_base(tmp_path / "tube", "tube")
        settings_path = _register(tmp_path, "tube", knowledge_base_path)
        container = FakeRunContainer(bus=FakeMessageBus(on_sync=_land_a_mirror(knowledge_base_path)))

        await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        assert list((knowledge_base_path / "Sources").iterdir()) == [knowledge_base_path / "Sources" / "New Mirror.md"]

    async def test_on_the_pass_runs_and_the_run_still_completes(self, tmp_path: Path):
        knowledge_base_path = _youtube_knowledge_base(tmp_path / "tube", "tube", librarian=True)
        settings_path = _register(tmp_path, "tube", knowledge_base_path)
        container = FakeRunContainer(
            bus=FakeMessageBus(on_sync=_land_a_mirror(knowledge_base_path)),
            knowledge_base_settings=KnowledgeBaseSettings(path=knowledge_base_path),
            azure_openai=AzureOpenAiSettings(endpoint=""),
            ingestion=IngestionSettings(summary_model="test-model"),
        )

        await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        remaining = list((knowledge_base_path / "Sources").rglob("*.md"))
        assert len(remaining) == 1
        assert remaining[0].read_text(encoding="utf-8").endswith("Body.\n")

    async def test_on_but_the_placement_agent_is_unreachable_the_run_still_completes(self, tmp_path: Path):
        knowledge_base_path = _youtube_knowledge_base(tmp_path / "tube", "tube", librarian=True)
        settings_path = _register(tmp_path, "tube", knowledge_base_path)
        container = FakeRunContainer(
            bus=FakeMessageBus(on_sync=_land_a_mirror(knowledge_base_path)), knowledge_base_settings=KnowledgeBaseSettings(path=knowledge_base_path)
        )

        await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        assert list((knowledge_base_path / "Sources").iterdir()) == [knowledge_base_path / "Sources" / "New Mirror.md"]


class TestAnnouncedFacts:
    async def test_a_clean_pass_announces_its_start_and_its_completion(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer()

        returned = await _job(settings_path, container)(RefreshJob(knowledge_base="demo", trigger="schedule"), CONTEXT)

        started, completed = container.bus.outbox.appended
        assert isinstance(started, RefreshStarted)
        assert isinstance(completed, RefreshCompleted)
        assert (started.knowledge_base, started.job_id, started.trigger) == ("demo", CONTEXT.job_id, "schedule")
        assert completed.detail == RefreshResult.model_validate_json(returned).detail

    async def test_the_last_attempt_that_fails_announces_the_abandonment_with_its_reason(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer(bus=FakeMessageBus(index_error=RuntimeError("boom")))
        last = CONTEXT.model_copy(update={"attempt": REFRESH_POLICY.max_attempts})

        with pytest.raises(RuntimeError, match="boom"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="demo"), last)

        started, abandoned = container.bus.outbox.appended
        assert isinstance(started, RefreshStarted)
        assert isinstance(abandoned, RefreshAbandoned)
        assert abandoned.reason == "boom"

    async def test_an_earlier_attempt_that_fails_announces_no_ending_at_all(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer(bus=FakeMessageBus(index_error=RuntimeError("boom")))

        with pytest.raises(RuntimeError, match="boom"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="demo"), CONTEXT)

        assert [type(fact).__name__ for fact in container.bus.outbox.appended] == ["RefreshStarted"]

    async def test_an_unregistered_knowledge_base_announces_nothing(self, tmp_path: Path):
        settings_path = _register(tmp_path, "demo", _plain_knowledge_base(tmp_path / "demo", "demo"))
        container = FakeRunContainer()

        with pytest.raises(LookupError, match="ghost"):
            await _job(settings_path, container)(RefreshJob(knowledge_base="ghost"), CONTEXT)

        assert container.bus.outbox.appended == []


class TestCounts:
    async def test_a_clean_pass_announces_and_returns_what_the_scan_and_the_sync_brought_in(self, tmp_path: Path):
        knowledge_base_path = _youtube_knowledge_base(tmp_path / "tube", "tube")
        (knowledge_base_path / "_meta" / "watch-list.yaml").write_text("[]", encoding="utf-8")
        settings_path = _register(tmp_path, "tube", knowledge_base_path)
        container = FakeRunContainer(
            bus=FakeMessageBus(
                scan_report=ScanReport(found=2, requested=2, requested_by_entry={"feed:news": 2}),
                sync_report=SyncReport(created=2, created_by_connector={"queue": 2}),
            )
        )

        returned = await _job(settings_path, container)(RefreshJob(knowledge_base="tube"), CONTEXT)

        expected = RefreshCounts(sources_created=2, requested_by_entry={"feed:news": 2}, created_by_connector={"queue": 2})
        _, completed = container.bus.outbox.appended
        assert isinstance(completed, RefreshCompleted)
        assert completed.counts == expected
        assert RefreshResult.model_validate_json(returned) == RefreshResult(detail=completed.detail, counts=expected)

    async def test_sources_an_earlier_failed_attempt_created_reach_the_abandonment(self, tmp_path: Path):
        settings_path = _register(tmp_path, "tube", _youtube_knowledge_base(tmp_path / "tube", "tube"))
        failing = (SyncError(key="BROKEN", message="404 from the connector"),)
        container = FakeRunContainer(bus=FakeMessageBus(sync_report=SyncReport(created=6, created_by_connector={"queue": 6}, errors=failing)))
        job = _job(settings_path, container)
        payload = RefreshJob(knowledge_base="tube")

        with pytest.raises(RuntimeError, match="404"):
            await job(payload, CONTEXT)
        container.bus.sync_report = SyncReport(errors=failing)
        for attempt in (2, REFRESH_POLICY.max_attempts):
            with pytest.raises(RuntimeError, match="404"):
                await job(payload, CONTEXT.model_copy(update={"attempt": attempt}))

        abandoned = container.bus.outbox.appended[-1]
        assert isinstance(abandoned, RefreshAbandoned)
        assert abandoned.counts.sources_created == 6, "attempts two and three created nothing, the job as a whole created six"
        assert job._carried == {}, "the abandonment hands the job's counts on and keeps none"

    async def test_a_job_that_completes_after_a_failed_attempt_announces_both_attempts_counts(self, tmp_path: Path):
        settings_path = _register(tmp_path, "tube", _youtube_knowledge_base(tmp_path / "tube", "tube"))
        container = FakeRunContainer(
            bus=FakeMessageBus(sync_report=SyncReport(created=6, errors=(SyncError(key="BROKEN", message="404 from the connector"),)))
        )
        job = _job(settings_path, container)
        payload = RefreshJob(knowledge_base="tube")

        with pytest.raises(RuntimeError, match="404"):
            await job(payload, CONTEXT)
        container.bus.sync_report = SyncReport(created=1)
        returned = await job(payload, CONTEXT.model_copy(update={"attempt": 2}))

        completed = container.bus.outbox.appended[-1]
        assert isinstance(completed, RefreshCompleted)
        assert completed.counts.sources_created == 7
        assert RefreshResult.model_validate_json(returned).counts.sources_created == 7
