"""The `studio` command line: the root `app`, its top-level commands and the command groups mounted on it.

This module defines the `index`, `ingest`, `status`, `search`, `fetch`, `mcp`, `sync`, `worker` and
`serve-http` commands and the `export` and `summaries` groups. Every other command and group comes from a
sibling `cli_*` module and is mounted with `app.add_typer`. The helpers this module shares with those
modules live in `studio.knowledge_base.cli_support`, which imports none of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import sys

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from core.ai.embedding import BatchEmbedder, BatchQueryEmbedder, Embeddings
from core.ai.retrieval.contracts import FeedbackEntry, Fetch, FetchResult, RetrievalHandlers, SearchHit, SqlResult
from core.cqrs.contracts import Broadcaster, MessageBus, PostgresJobs, PostgresWake, TopicPublisher, Worker
from core.db import async_dsn, redact_db
from core.observability import LoggingSettings, configure_logging, get_logger
from core.supervisor import SupervisedChild
from engram.authoring.contracts import (
    REINDEX_FINDINGS_SHOWN,
    AuthoringKnowledgeBase,
    AuthoringTools,
    ReindexFinding,
    ReindexResult,
    doc_authoring_knowledge_bases,
)
from engram.evals.commands import KnowledgeBaseGatewayFactory, PolicyEvaluatorFactory, SeedRepositoryFactory
from engram.evals.generation.contracts import GenerationConfig
from engram.evals.tuning.contracts import RetrievalStrategy, load_active_policy, resolve_strategy
from engram.export.contracts import ExportOkf, OkfExportReport
from engram.identity import read_stamp
from engram.indexing.contracts import (
    CollectionIsForeignError,
    CollectionIsIndexedError,
    DryRunPreview,
    FilesystemKnowledgeBaseReader,
    IndexKnowledgeBase,
    IndexReport,
    IngestPage,
    IngestResult,
    IngestValidationError,
    PartitionPreview,
    PendingSummary,
    RetrievalSwitches,
    SummaryGovernance,
)
from engram.init.contracts import SchemaRevisionError, assert_schema_at_head
from engram.kb import contracts as kb
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.contracts import (
    DescribeSchemaHandler,
    EnvelopeHandler,
    FetchHandler,
    GraphExpandHandler,
    GraphPathsHandler,
    ReadOnlySqlGateway,
    ReadSurface,
    Reranking,
    Search,
    SearchHandler,
    read_hits_per_document,
    switch_source,
)
from engram.retrieval.contracts import policy_instructions as read_policy_instructions
from engram.retrieval.infrastructure.mcp_server import PROPOSE_SEEDS, create_mcp_server
from engram.sources.contracts import ScanReport, ScanWatchList, SyncMirrors, SyncReport
from pydantic import ValidationError
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from studio.knowledge_base.cli_decisions import decisions_app
from studio.knowledge_base.cli_evals import evals_app
from studio.knowledge_base.cli_feedback import feedback_app
from studio.knowledge_base.cli_init import init_app
from studio.knowledge_base.cli_note import note_app
from studio.knowledge_base.cli_source_queue import source_queue_app
from studio.knowledge_base.cli_support import (
    _UNREACHABLE_ERRORS,
    FEEDBACK_FILE,
    ActiveKnowledgeBase,
    KnowledgeBaseMissingError,
    _build_container,
    _dispatch_index,
    _fail,
    _print_index_report,
    _run_command,
    announce_db,
    announce_identity,
    announce_knowledge_base,
    knowledge_base_path_origin,
    load_knowledge_base_schema_or_fail,
    read_feedback,
    resolve_knowledge_base_root,
    resolve_partition,
    run_naming_connection_failures,
    settings_knowledge_base_path,
    unreachable_line,
)
from studio.knowledge_base.cli_vocab import vocab_app
from studio.knowledge_base.cli_watch_list import watch_list_app
from studio.knowledge_base.container import dispose_container, run_container_factory
from studio.knowledge_base.refresh.contracts import (
    REFRESH_KIND,
    REFRESH_POLICY,
    REFRESH_TOPIC,
    TICK_INTERVAL_SECONDS,
    RefreshJob,
    RefreshKnowledgeBaseJob,
    Ticker,
)
from studio.knowledge_base.reindex import SubprocessReindex
from studio.knowledge_base.settings import (
    AzureOpenAiSettings,
    DatabaseSettings,
    EvalsSettings,
    IngestionSettings,
    KnowledgeBasePathOrigin,
    KnowledgeBaseSettings,
    RetrievalSettings,
    default_settings_path,
    fetch_application_settings,
    read_registry,
)
from studio.knowledge_base.source_queue import source_queue_at
from studio.knowledge_base.watch_list import WATCH_LIST_PATH
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, ReaderSqlGateway, SearchReranking
from studio.knowledge_base.wiring.indexing import indexer_version_changed

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from collections.abc import Mapping, Sequence

    from core.ai.retrieval.contracts import FeedbackCategory, FeedbackSink, KnowledgeBaseStamp, SqlGateway
    from core.cqrs.contracts import JobObserver, JobsAndQueries, Wake
    from core.resolver import Resolver
    from engram.authoring.contracts import Reindex
    from engram.evals.shared.contracts import DifficultyThresholds
    from engram.evals.tuning.contracts import RetrievalProfile
    from engram.identity import KnowledgeBaseIdentity
    from engram.indexing.contracts import ValidationIssue
    from mcp.server.mcpserver import MCPServer
    from sqlalchemy import Row

    from studio.knowledge_base.settings import ApplicationSettings, KnowledgeBaseEntry, RecommendationSettings

# The marker `api.fetch` appends to a page's card text when it cut the text short. The fetch command
# strips it from the body and prints a hint instead.
_FETCH_TRUNCATION_MARKER = "[truncated — fetch sections instead]"

# The running studio executable, resolved once at import. The `studio mcp` banner prints it as `bin=`,
# so a wrapper earlier on PATH that serves the wrong knowledge base shows its path there.
_STUDIO_BIN = Path(sys.argv[0]).resolve()

# The settings file holding the registry of knowledge bases, resolved once at import. It is the default
# `settings_path` of the MCP server build and of the documentation-authoring build.
_DEFAULT_SETTINGS_PATH = default_settings_path()

# The `kb.*` tables that a `status` row-count read reports, in load order.
_STATUS_TABLES: tuple[str, ...] = (
    "pages",
    "sections",
    "claims",
    "edges",
    "sources_raw",
    "raw_chunks",
    "vocab",
    "query_log",
)

_logger = get_logger(__name__)

app = typer.Typer(name="studio", help="Index the knowledge base into Postgres and serve retrieval (MCP + CLI).", no_args_is_help=True)
export_app = typer.Typer(name="export", help="Project the knowledge base into a portable OKF v0.2 export directory.", no_args_is_help=True)
summaries_app = typer.Typer(
    name="summaries", help="Review and approve index-time disclosure summaries (`index --summaries`).", no_args_is_help=True
)
app.add_typer(note_app)
app.add_typer(export_app)
app.add_typer(evals_app)
app.add_typer(feedback_app)
app.add_typer(watch_list_app)
app.add_typer(decisions_app)
app.add_typer(init_app)
app.add_typer(source_queue_app)
app.add_typer(summaries_app)
app.add_typer(vocab_app)


@app.callback(invoke_without_command=True)
def _root(
    *, show_version: Annotated[bool, typer.Option("--version", is_eager=True, help="Show the studio version and exit.")] = False
) -> None:
    """Index a markdown knowledge base into Postgres and serve retrieval over MCP + CLI (the studio root)."""
    if show_version:
        typer.echo(f"studio {version('studio')}")
        raise typer.Exit


def _resolve_settings(knowledge_base: str | None, *, cwd: Path) -> ActiveKnowledgeBase:
    """The settings and the knowledge base that `knowledge_base` names, or the one `cwd` sits in."""
    try:
        root = resolve_knowledge_base_root(knowledge_base, cwd=cwd)
    except KnowledgeBaseMissingError as exc:
        _fail(str(exc))
    return ActiveKnowledgeBase(application=fetch_application_settings(), knowledge_base=KnowledgeBaseSettings(path=root))


async def _dispatch_sync(container: Resolver, command: SyncMirrors) -> tuple[ScanReport | None, SyncReport]:
    """Scan the knowledge base's watch list when there is one, then dispatch `command` through the message bus."""
    bus = await container.get(MessageBus)
    scan = await _maybe_scan_watch_list(container, bus)
    report = await bus.handle(command)
    if not isinstance(report, SyncReport):  # pragma: no cover (the sync handler returns a SyncReport)
        _fail("sync produced no report.")
    return scan, report


async def _sync_after_identity(
    container: Resolver, settings: ActiveKnowledgeBase, command: SyncMirrors, *, collection: str
) -> tuple[ScanReport | None, SyncReport]:
    """Announce the identity, then dispatch the sync, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _dispatch_sync(container, command)


async def _maybe_scan_watch_list(container: Resolver, bus: MessageBus) -> ScanReport | None:
    """Scan the knowledge base's watch list, or return `None` when there is no knowledge base, source layer or watch list."""
    try:
        schema = await container.get(KnowledgeBaseSchema)
        knowledge_base = await container.get(KnowledgeBaseSettings)
    except Exception:  # noqa: BLE001 (a resolver with no knowledge base context has nothing to scan)
        return None
    if not (schema.sources.enabled and (knowledge_base.path / WATCH_LIST_PATH).is_file()):
        return None
    scan = await bus.handle(ScanWatchList())
    if not isinstance(scan, ScanReport):  # pragma: no cover (the scan handler always returns a ScanReport)
        _fail("scan produced no report.")
    return scan


def _print_scan_report(report: ScanReport) -> None:
    """Echo the human-readable scan summary, printed before the sync summary."""
    typer.echo(f"Scanned the watch list: {report.found} found, {report.requested} requested, {len(report.failed)} failed")
    for label, reason in report.failed:
        typer.echo(f"  FAILED {label}: {reason}")


def _print_sync_report(report: SyncReport) -> None:
    """Echo the human-readable sync summary: the plan rows on a dry run, counters otherwise."""
    if report.dry_run:
        typer.echo(f"Planned actions (dry run): {len(report.planned)}")
        for row in report.planned:
            typer.echo(f"  {row.action.value.upper():<9} {row.key}  {row.title}")
    else:
        typer.echo(f"Synced: {report.created} created, {report.updated} updated, {report.archived} archived, {report.skipped} skipped")
    if report.unavailable:
        typer.echo(f"Unavailable, no readable PDF ({len(report.unavailable)}): {', '.join(report.unavailable)}")
    if report.too_old:
        typer.echo(f"Not admitted, older than the policy's max_age_years ({len(report.too_old)}): {', '.join(report.too_old)}")
    for error in report.errors:
        typer.echo(f"  ERROR {error.key}: {error.message}")


async def _dispatch_export(container: Resolver, command: ExportOkf) -> OkfExportReport:
    """Resolve the shared `MessageBus` and dispatch `command`, returning its export report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    if not isinstance(report, OkfExportReport):  # pragma: no cover (the export handler returns an OkfExportReport)
        _fail("export produced no report.")
    return report


async def _export_with_identity(container: Resolver, settings: ActiveKnowledgeBase, command: ExportOkf) -> OkfExportReport:
    """Announce the identity, then dispatch the export, one event loop for both."""
    await announce_identity(container, settings, collection=command.collection)
    return await _dispatch_export(container, command)


def _print_export_report(report: OkfExportReport) -> None:
    """Echo the human-readable OKF-export summary (concepts, skipped meta, relations, files)."""
    typer.echo(
        f"Exported OKF bundle in {report.duration_seconds:.1f}s: {report.concepts} concepts ({report.skipped_meta} meta pages skipped)"
    )
    typer.echo(f"Relations: {report.relations} | Dangling body links flattened: {report.dangling}")
    typer.echo(f"Files written: {report.files_written} (including index.md)")


def _mcp_profile_name(knowledge_base_profile: str, override_profile: str) -> str:
    """The retrieval profile the MCP server serves: a settings profile other than `full` wins over the schema's own."""
    return override_profile if override_profile != RetrievalStrategy.FULL.value else knowledge_base_profile


def _append_jsonl(path: Path, line: str) -> None:
    """Append one `line` to `path` (creating parent dirs), the primitive the feedback sink uses."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _make_feedback_sink(path: Path) -> FeedbackSink:
    """Build the `FeedbackSink` that appends each MCP feedback entry, stamped with the time, as one JSON line to `path`."""

    async def record(*, category: FeedbackCategory, message: str, tool: str | None) -> None:
        entry = FeedbackEntry(created_at=datetime.now(UTC).isoformat(), category=category, message=message, tool=tool)
        await asyncio.to_thread(_append_jsonl, path, entry.model_dump_json())

    return record


def _apply_active_policy(profile: RetrievalProfile, knowledge_base_path: Path) -> tuple[RetrievalProfile, Mapping[str, str] | None, str | None]:
    """The active policy's profile, tool descriptions and instructions, or `profile` alone without one, exiting 1 on a bad file."""
    try:
        resolution = load_active_policy(knowledge_base_path)
    except (ValidationError, ValueError) as exc:
        _fail(f"invalid active-policy file in {knowledge_base_path}: {exc}")
    if resolution is None:
        return profile, None, None
    return resolution.profile, resolution.tool_descriptions, resolution.instructions


_KNOWLEDGE_BASE_SCHEMA = "knowledge-base-schema.json"
"""The file a knowledge base root declares itself with, read from each registered root in turn."""


def _read_surfaces(
    active_root: Path,
    active_schema: KnowledgeBaseSchema,
    settings_path: Path,
    reader_gateway: ReadOnlySqlGateway,
    admin_gateway: SqlGateway,
    embedder: BatchEmbedder,
    *,
    active_hybrid: bool,
    notes: list[str],
    rerank: Reranking | None,
) -> tuple[ReadSurface, ...]:
    """Every knowledge base this server may read, the served one first. `notes` names each public one that fails to load or clashes."""
    active = active_root.expanduser().resolve()
    surfaces = [_read_surface(active, active_schema, reader_gateway, admin_gateway, embedder, hybrid=active_hybrid, rerank=rerank)]
    rows = [row for row in _registered_roots(settings_path, notes=notes) if row.root != active]
    loaded = [(row, _registered_schema(row.name, row.root, notes=[] if row.private else notes)) for row in rows]
    claimed = {claim for surface in surfaces for claim in (surface.name, surface.collection)} | _withheld_claims(loaded)
    for row, schema in loaded:
        if schema is None or row.private or schema.retrieval.private:
            continue
        if {schema.slug, schema.retrieval.collection} & claimed:
            notes.append(f"Knowledge base {row.name!r} repeats a name or collection already claimed here, so it is not searchable.")
            continue
        hybrid = resolve_strategy(schema.retrieval.profile).search_hybrid is not False
        surfaces.append(_read_surface(row.root, schema, reader_gateway, admin_gateway, embedder, hybrid=hybrid, rerank=rerank))
        claimed |= {schema.slug, schema.retrieval.collection}
    return tuple(surfaces)


@dataclass(frozen=True)
class _RegistryRow:
    """One registered knowledge base as this server weighs it: its registered name, its root, and whether it is private."""

    name: str
    root: Path
    private: bool


def _withheld_claims(loaded: Sequence[tuple[_RegistryRow, KnowledgeBaseSchema | None]]) -> set[str]:
    """The slugs and collections of every registered knowledge base whose schema loads and that the registry or its schema marks private."""
    return {
        claim
        for row, schema in loaded
        if schema is not None and (row.private or schema.retrieval.private)
        for claim in (schema.slug, schema.retrieval.collection)
    }


def _registered_roots(settings_path: Path, *, notes: list[str]) -> list[_RegistryRow]:
    """Every registered knowledge base in `settings_path`, private ones included, naming in `notes` what could not be read."""
    if not settings_path.is_file():
        return []
    try:
        registered, broken = read_registry(settings_path)
    except (OSError, json.JSONDecodeError) as error:
        notes.append(f"The knowledge base registry at {settings_path} could not be read ({error}), so only this knowledge base is searchable.")
        return []
    for name, path in broken.items():
        notes.append(f"Knowledge base {name!r} ({path or 'no path recorded'}) is registered but unusable, so it is not searchable.")
    return [_RegistryRow(name=name, root=entry.path.expanduser().resolve(), private=entry.private) for name, entry in registered.items()]


def _registered_schema(name: str, root: Path, *, notes: list[str]) -> KnowledgeBaseSchema | None:
    """One registered root's own schema, or `None` with a note when the root carries none that loads."""
    schema_path = root / _KNOWLEDGE_BASE_SCHEMA
    if not schema_path.is_file():
        notes.append(f"Knowledge base {name!r} at {root} carries no {_KNOWLEDGE_BASE_SCHEMA}, so it is not searchable.")
        return None
    try:
        return KnowledgeBaseSchema.from_path(schema_path)
    except (OSError, ValueError) as error:
        notes.append(f"Knowledge base {name!r} at {root} did not load its schema ({error}), so it is not searchable.")
        return None


def _read_surface(
    root: Path,
    schema: KnowledgeBaseSchema,
    reader_gateway: ReadOnlySqlGateway,
    admin_gateway: SqlGateway,
    embedder: BatchEmbedder,
    *,
    hybrid: bool,
    rerank: Reranking | None,
) -> ReadSurface:
    """One knowledge base's read route, whose handlers, freshness stamp and facet keys come from its own collection, files and schema."""
    collection = schema.retrieval.collection
    gateway = reader_gateway.for_collection(collection)
    reader = FilesystemKnowledgeBaseReader(root, note_folders=schema.note_folders, note_files=schema.note_files)

    async def stamp() -> KnowledgeBaseStamp:
        return await read_stamp(admin_gateway, reader, slug=schema.slug, collection=collection)

    return ReadSurface(
        name=schema.slug,
        collection=collection,
        domain=schema.purpose,
        stamp=stamp,
        handlers=RetrievalHandlers(
            search=rerank.over(SearchHandler(gateway, embedder), FetchHandler(gateway, collection))
            if rerank is not None
            else SearchHandler(gateway, embedder),
            fetch=FetchHandler(gateway, collection),
            graph_expand=GraphExpandHandler(gateway, collection),
            find_paths=GraphPathsHandler(gateway, collection),
        ),
        envelope=EnvelopeHandler(
            gateway,
            mode=schema.retrieval.disclosure_mode,
            excluded_page_types=schema.disclosure_excluded_page_types(),
            group_by=schema.retrieval.group_by,
            orientation=schema.orientation(),
            collection=collection,
        ),
        guidance=read_policy_instructions(root),
        hybrid=hybrid,
        switch_source=switch_source(gateway, collection) if schema.retrieval.mode == "auto" else None,
        facet_keys=tuple(facet.key for facet in schema.facets),
    )


async def _stored_switches(surface: ReadSurface) -> RetrievalSwitches | None:
    """The stored switches of an auto-mode surface, every switch off when the database cannot be reached, `None` for a fixed-mode one."""
    if surface.switch_source is None:
        return None
    try:
        return await surface.switch_source()
    except _UNREACHABLE_ERRORS:
        return RetrievalSwitches()


async def _resolve_rerank(container: Resolver) -> Reranking | None:
    """The re-ranking the container holds, or `None` when it is off or OpenRouter has no key."""
    return (await container.get(SearchReranking)).reranking


async def _build_mcp_server(
    container: Resolver,
    *,
    authoring: bool = False,
    doc_authoring: bool = False,
    settings_path: Path = _DEFAULT_SETTINGS_PATH,
    logging_settings: LoggingSettings | None = None,
) -> MCPServer:
    """Resolve everything the MCP server needs on this loop and build it, for the caller to run over stdio."""
    configure_logging(logging_settings)
    reader_gateway = await container.get(ReaderSqlGateway)
    describe_handler = await container.get(DescribeSchemaHandler)
    graph_expand_handler = await container.get(GraphExpandHandler)
    find_paths_handler = await container.get(GraphPathsHandler)
    search_handler = await container.get(SearchHandler)
    fetch_handler = await container.get(FetchHandler)
    envelope_handler = await container.get(EnvelopeHandler)
    retrieval_settings = await container.get(RetrievalSettings)
    rerank = await _resolve_rerank(container)
    knowledge_base_settings = await container.get(KnowledgeBaseSettings)
    database = await container.get(DatabaseSettings)

    schema = load_knowledge_base_schema_or_fail(knowledge_base_settings.path)
    # Every read this server makes is this knowledge base's own read, so the SQL surface carries its identity:
    # a knowledge base that is private still serves itself in full here.
    gateway = reader_gateway.for_collection(schema.retrieval.collection)
    profile_name = _mcp_profile_name(schema.retrieval.profile, retrieval_settings.retrieval_profile)
    try:
        profile = resolve_strategy(profile_name)
    except ValueError as exc:
        _fail(str(exc))

    # The active policy file, when present, overrides the schema's profile. The authoring widening below
    # applies on top of the result.
    profile, descriptions, policy_instructions = _apply_active_policy(profile, knowledge_base_settings.path)

    # The documentation-authoring tools are wired when either `--doc-authoring` or the schema's
    # `retrieval.doc_authoring` opts in.
    doc_authoring_tools: AuthoringTools | None = None
    doc_engine: AsyncEngine | None = None
    registry_note: str | None = None
    if doc_authoring or schema.retrieval.doc_authoring:
        doc_authoring_tools, doc_engine, registry_note = _build_doc_authoring(container, database, knowledge_base_settings, schema, settings_path)

    # One read route per knowledge base this server may reach: the one it serves, then every registered
    # one that is shared. Each route's freshness stamp reads the `kb` database schema through
    # `admin_gateway`, which the reader role cannot see, and is read again on every tool call.
    read_notes: list[str] = []
    surfaces = _read_surfaces(
        knowledge_base_settings.path,
        schema,
        settings_path,
        reader_gateway,
        await container.get(AdminSqlGateway),
        await container.get(BatchEmbedder),
        active_hybrid=profile.search_hybrid is not False,
        notes=read_notes,
        rerank=rerank,
    )
    # Each read route's knowledge base has its own cap on hits per document, read once, here, for every `search` description.
    surfaces = tuple(
        [
            replace(surface, hits_per_document=await read_hits_per_document(search_handler, knowledge_base=surface.name, collection=surface.collection))
            for surface in surfaces
        ]
    )
    stamp = surfaces[0].stamp
    # The graph tools are mounted once, so an auto-mode knowledge base's graph switch is read once, here.
    switches = await _stored_switches(surfaces[0])

    # The banner must go to stderr, since stdout carries the MCP protocol. `redact_db` drops the credentials.
    authoring_suffix = " authoring=on" if authoring else ""
    doc_authoring_suffix = " doc_authoring=on" if doc_authoring_tools is not None else ""
    policy_suffix = " policy=tuned" if descriptions is not None else ""
    typer.echo(
        f"studio mcp serving: knowledge_base={knowledge_base_settings.path} db={redact_db(database.reader_url)} "
        f"bin={_STUDIO_BIN} dialect={retrieval_settings.dialect.value} "
        f"graph={retrieval_settings.graph_backend.value} profile={profile_name} "
        f"collection={schema.retrieval.collection}{authoring_suffix}{doc_authoring_suffix}{policy_suffix}",
        err=True,
    )

    # The embedder the read routes already resolved, named by its model so a search compares like with like.
    embed_query = BatchQueryEmbedder(await container.get(BatchEmbedder))

    # Only `--authoring` resolves the authoring collaborators and widens the tool set by `PROPOSE_SEEDS`.
    authoring_deps = await _resolve_authoring_deps(container) if authoring else None
    only = profile.tools | {PROPOSE_SEEDS} if authoring else profile.tools

    # When the agent does not tune `k`, the search hit cap is pinned here and the served tool takes no `k`.
    # The closure holds a copy of the cap, not a live read of the settings.
    pinned_k = retrieval_settings.k_default
    k_source = None if retrieval_settings.agent_tunes_k else (lambda: pinned_k)

    # The server instructions name every registered knowledge base that could not join, and a registry of
    # knowledge bases the authoring side could not read, beside the policy instructions.
    instructions = "\n\n".join(part for part in (policy_instructions, *read_notes) if part) or None
    if registry_note is not None:
        instructions = f"{instructions}\n\n{registry_note}" if instructions else registry_note

    server = create_mcp_server(
        gateway,
        describe_handler,
        embed_query,
        search_handler=search_handler,
        fetch_handler=fetch_handler,
        graph_expand_handler=graph_expand_handler,
        find_paths_handler=find_paths_handler,
        envelope_handler=envelope_handler,
        only=only,
        search_hybrid=profile.search_hybrid,
        switches=switches,
        collection=schema.retrieval.collection,
        feedback_sink=_make_feedback_sink(knowledge_base_settings.path / FEEDBACK_FILE),
        seed_repo_factory=authoring_deps.seed_repo_factory if authoring_deps is not None else None,
        datasets_dir=authoring_deps.datasets_dir if authoring_deps is not None else None,
        thresholds=authoring_deps.thresholds if authoring_deps is not None else None,
        policy_evaluator_factory=authoring_deps.policy_evaluator_factory if authoring_deps is not None else None,
        policies_dir=authoring_deps.policies_dir if authoring_deps is not None else None,
        gateway_factory=authoring_deps.knowledge_base_gateway_factory if authoring_deps is not None else None,
        recommendation_settings=authoring_deps.recommendation_settings if authoring_deps is not None else None,
        recommendations_dir=authoring_deps.recommendations_dir if authoring_deps is not None else None,
        knowledge_base_path=knowledge_base_settings.path if authoring else None,
        descriptions=descriptions,
        instructions=instructions,
        k_source=k_source,
        stamp=stamp,
        doc_authoring_tools=doc_authoring_tools,
        read_surfaces=surfaces,
        hits_per_document=surfaces[0].hits_per_document,
    )
    # The admin engine pools connections and `stamp` reads through it. Disposing it before this loop closes
    # makes the served loop open fresh connections instead of reusing ones bound to this loop.
    engine = await container.get(AsyncEngine)
    await engine.dispose()
    if doc_engine is not None:
        await doc_engine.dispose()
    return server


async def _mcp_server_after_identity(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, authoring: bool, doc_authoring: bool = False
) -> MCPServer:
    """Announce the identity, then build the MCP server on the same loop, even when the database is unreachable."""
    announce_knowledge_base(settings_knowledge_base_path(settings), err=True)
    identity = await announce_db(container, settings, collection=collection, err=True)
    if identity is None:
        typer.echo("serving anyway, every call fails until the database is reachable.", err=True)
    return await _build_mcp_server(container, authoring=authoring, doc_authoring=doc_authoring, logging_settings=settings.application.logging)


@dataclass(frozen=True, slots=True)
class _AuthoringDeps:
    """The collaborators the write tools of `studio mcp --authoring` need."""

    seed_repo_factory: SeedRepositoryFactory
    datasets_dir: Path
    thresholds: DifficultyThresholds
    policy_evaluator_factory: PolicyEvaluatorFactory
    policies_dir: Path
    knowledge_base_gateway_factory: KnowledgeBaseGatewayFactory
    recommendation_settings: RecommendationSettings
    recommendations_dir: Path


async def _resolve_authoring_deps(container: Resolver) -> _AuthoringDeps:
    """Resolve the collaborators the `--authoring` write tools need from the container."""
    seed_repo_factory = await container.get(SeedRepositoryFactory)
    evals_settings = await container.get(EvalsSettings)
    config = await container.get(GenerationConfig)
    policy_evaluator_factory = await container.get(PolicyEvaluatorFactory)
    knowledge_base_gateway_factory = await container.get(KnowledgeBaseGatewayFactory)
    return _AuthoringDeps(
        seed_repo_factory=seed_repo_factory,
        datasets_dir=evals_settings.datasets_dir,
        thresholds=config.thresholds,
        policy_evaluator_factory=policy_evaluator_factory,
        policies_dir=evals_settings.tuning.policies_dir,
        knowledge_base_gateway_factory=knowledge_base_gateway_factory,
        recommendation_settings=evals_settings.recommendation,
        recommendations_dir=evals_settings.recommendation.recommendations_dir,
    )


class _BusReindex:
    """`Reindex` through this process's own message bus, correct only for the knowledge base the process serves."""

    def __init__(self, container: Resolver) -> None:
        """Store the resolver the message bus is dispatched through."""
        self._container = container

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Reindex `knowledge_base`'s collection through the studio's own index command, reading its own validation report."""
        command = IndexKnowledgeBase(collection=knowledge_base.collection, embed=True, collection_named=True)
        report = await _dispatch_index(self._container, command)
        if report.validation.n_errors == 0:
            return ReindexResult(outcome="clean", detail=f"reindexed {knowledge_base.path}")
        findings = tuple(
            ReindexFinding(page_id=issue.page_id, rule=issue.rule, message=issue.message)
            for issue in report.validation.errors[:REINDEX_FINDINGS_SHOWN]
        )
        return ReindexResult(
            outcome="validation_errors",
            detail=f"reindexed {knowledge_base.path} with {report.validation.n_errors} validation error(s)",
            findings=findings,
            findings_total=report.validation.n_errors,
        )


class _MultiKnowledgeBaseReindex:
    """`Reindex` that indexes the served knowledge base in this process and every other one in a subprocess."""

    def __init__(self, own_name: str, own: Reindex, other: Reindex) -> None:
        """Store the session's own knowledge base name and the two `Reindex` ports to route between."""
        self._own_name = own_name
        self._own = own
        self._other = other

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Reindex `knowledge_base` through whichever port is correct for it."""
        reindex = self._own if knowledge_base.name == self._own_name else self._other
        return await reindex(knowledge_base)


def _degraded_registry_note(settings_path: Path, own_name: str) -> str:
    """The instructions-line note for when `doc_authoring_knowledge_bases` could not read `settings_path`."""
    return (
        f"The list of registered knowledge bases at {settings_path} could not be read, so only this "
        f"session's own knowledge base, '{own_name}', is mounted for documentation authoring."
    )


def _build_doc_authoring(
    container: Resolver,
    database: DatabaseSettings,
    knowledge_base_settings: KnowledgeBaseSettings,
    schema: KnowledgeBaseSchema,
    settings_path: Path = _DEFAULT_SETTINGS_PATH,
) -> tuple[AuthoringTools, AsyncEngine, str | None]:
    """Build the documentation-authoring tools, the engine the caller disposes, and a note when the registry cannot be read."""
    own = AuthoringKnowledgeBase(name=schema.slug, collection=schema.retrieval.collection, path=knowledge_base_settings.path, schema=schema)
    scan = doc_authoring_knowledge_bases(settings_path)
    knowledge_bases = {knowledge_base.name: knowledge_base for knowledge_base in scan.knowledge_bases}
    knowledge_bases[own.name] = own
    engine = create_async_engine(async_dsn(database.url), poolclass=NullPool)
    reindex = _MultiKnowledgeBaseReindex(own.name, _BusReindex(container), SubprocessReindex())
    authoring = AuthoringTools(
        knowledge_bases=knowledge_bases,
        reindex=reindex,
        admin=ReadOnlySqlGateway(engine),
        queue_for=lambda target: source_queue_at(target.path, target.schema),
    )
    note = None if scan.reachable else _degraded_registry_note(settings_path, own.name)
    return authoring, engine, note


@dataclass(frozen=True, slots=True)
class _IndexFingerprint:
    """One collection's stored index fingerprint, as `studio status` reports it."""

    indexer_version: str
    spec_hash: str
    vocabulary_hash: str
    embed_model: str
    indexed_at: datetime

    @classmethod
    def from_row(cls, row: Row) -> _IndexFingerprint:
        """Build the value object from the projected `index_fingerprint` row (a `Row.__getattr__` read)."""
        return cls(
            indexer_version=str(row.indexer_version),
            spec_hash=str(row.spec_hash),
            vocabulary_hash=str(row.vocabulary_hash),
            embed_model=str(row.embed_model),
            indexed_at=row.indexed_at,
        )


# How many leading hex characters of the schema and vocabulary hashes `studio status` prints.
_HASH_DISPLAY_LEN = 12


def _index_drift_lines(fingerprint: _IndexFingerprint | None, files_tracked: int, *, version_changed: bool) -> list[str]:
    """The index drift lines of `studio status`: mode, tracked files, fingerprint, and a hint when the next index runs full."""
    tracked = fingerprint is not None and files_tracked > 0
    stale = files_tracked == 0 or version_changed
    lines = [f"Index mode (last run): {'incremental' if tracked else 'full'}", f"Files tracked (manifest): {files_tracked}"]
    if fingerprint is None:
        lines.append("Fingerprint: none yet")
    else:
        lines.append(
            f"Fingerprint: indexer={fingerprint.indexer_version} spec={fingerprint.spec_hash[:_HASH_DISPLAY_LEN]} "
            f"registry={fingerprint.vocabulary_hash[:_HASH_DISPLAY_LEN]} embed_model={fingerprint.embed_model} "
            f"at {fingerprint.indexed_at}"
        )
    if stale:
        lines.append("Manifest stale ⇒ next run is --full (cold start, or an indexer-version, chunk-size or chunk-planner change).")
    return lines


async def _print_status(container: Resolver, *, collection: str) -> None:
    """Print the table row counts, embedding metadata, last query time and index drift of `collection`, writing nothing."""
    engine = await container.get(AsyncEngine)
    azure_openai = await container.get(AzureOpenAiSettings)
    ingestion = await container.get(IngestionSettings)
    async with engine.connect() as conn:
        typer.echo("Row counts:")
        for table in _STATUS_TABLES:
            count = await conn.scalar(text(f"SELECT count(*) FROM kb.{table}"))  # noqa: S608 (table name from a fixed allowlist)
            typer.echo(f"  kb.{table}: {count}")
        embed_meta = (await conn.execute(text("SELECT model, dim, embedded_at FROM kb.embed_meta LIMIT 1"))).first()
        typer.echo(f"Embeddings: {f'{embed_meta.model} (dim {embed_meta.dim}) at {embed_meta.embedded_at}' if embed_meta else 'none yet'}")
        last_query = await conn.scalar(text("SELECT max(asked_at) FROM kb.query_log"))
        typer.echo(f"Last query logged: {last_query or 'never'}")
        files_tracked = await conn.scalar(
            text("SELECT count(*) FROM kb.index_manifest WHERE collection = :collection"), {"collection": collection}
        )
        fingerprint_row = (
            await conn.execute(
                text(
                    "SELECT indexer_version, spec_hash, vocabulary_hash, embed_model, indexed_at "
                    "FROM kb.index_fingerprint WHERE collection = :collection"
                ),
                {"collection": collection},
            )
        ).first()
        fingerprint = _IndexFingerprint.from_row(fingerprint_row) if fingerprint_row is not None else None
        version_changed = indexer_version_changed(fingerprint.indexer_version if fingerprint is not None else None, azure_openai, ingestion)
        for line in _index_drift_lines(fingerprint, files_tracked or 0, version_changed=version_changed):
            typer.echo(line)


async def _status_after_identity(container: Resolver, settings: ActiveKnowledgeBase, *, collection: str) -> None:
    """Announce the identity, then print the status, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    await _print_status(container, collection=collection)


class _UnusedEmbedder:
    """A stand-in `BatchEmbedder` for BM25-only search, whose `embed_batch` raises when called."""

    @property
    def model_id(self) -> str:  # pragma: no cover (never recorded on the read path)
        return "unused"

    @property
    def dimensions(self) -> int:  # pragma: no cover (never recorded on the read path)
        return 0

    async def embed_batch(self, texts: Sequence[str]) -> Embeddings:  # pragma: no cover (never called on BM25-only)
        del texts
        msg = "the BM25-only search path must never embed the query"
        raise RuntimeError(msg)


async def _run_search(container: Resolver, query: Search, *, hybrid: bool) -> SqlResult:
    """Run `query` as the collection it names, resolving the embedder only when `hybrid` is set."""
    gateway = await container.get(ReaderSqlGateway)
    embedder: BatchEmbedder = await container.get(BatchEmbedder) if hybrid else _UnusedEmbedder()
    handler = SearchHandler(gateway.for_collection(query.collection) if query.collection else gateway, embedder)
    rerank = await _resolve_rerank(container)
    fetch = FetchHandler(gateway.for_collection(query.collection) if query.collection else gateway, query.collection or "")
    searched = rerank.over(handler, fetch) if rerank is not None else handler
    return await searched(query)


async def _run_fetch(container: Resolver, query: Fetch, collection: str) -> SqlResult:
    """Fetch one unit by id, reading as `collection` rather than as the served knowledge base's own."""
    gateway = await container.get(ReaderSqlGateway)
    handler = FetchHandler(gateway.for_collection(collection), collection)
    return await handler(query)


async def _search_with_identity(
    container: Resolver, settings: ActiveKnowledgeBase, message: Search, *, collection: str, hybrid: bool
) -> SqlResult:
    """Announce the identity, then run the search, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _run_search(container, message, hybrid=hybrid)


async def _fetch_with_identity(container: Resolver, settings: ActiveKnowledgeBase, query: Fetch, collection: str) -> SqlResult:
    """Announce the identity, then run the fetch, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _run_fetch(container, query, collection)


def _print_search_hits(result: SqlResult) -> None:
    """Echo the ranked hits as `score  id  [level]  title/snippet` lines, each folded unit and its snippet indented under its hit, or a no-hits note."""
    if not result.rows:
        typer.echo("No hits.")
        return
    for row in result.rows:
        hit = SearchHit.model_validate(dict(row))
        label = hit.page_title or hit.snippet or ""
        typer.echo(f"{hit.score:.3f}  {hit.id}  [{hit.hit_level}]  {label}".rstrip())
        for unit in hit.folded_units:
            typer.echo(f"       {unit.id}  [{unit.hit_level}]")
            snippet = " ".join((unit.snippet or "").split())
            if snippet:
                typer.echo(f"         {snippet}")
    if result.truncated_message:
        typer.echo(result.truncated_message)


def _print_fetch_unit(result: SqlResult, unit_id: str) -> None:
    """Echo the fetched unit and a hint when its text was cut short, exiting 1 when nothing matched `unit_id`."""
    if not result.rows:
        _fail(f"No unit found with id {unit_id!r}.")
    unit = FetchResult.model_validate(dict(result.rows[0]))
    typer.echo(f"{unit.id}  [{unit.level}]" + (f"  {unit.title}" if unit.title else ""))
    body = unit.body or ""
    truncated = _FETCH_TRUNCATION_MARKER in body
    typer.echo(body.replace(_FETCH_TRUNCATION_MARKER, "").rstrip() if truncated else body)
    if truncated:
        typer.echo("(truncated: fetch its sections for the full text)")


def _print_dry_run_preview(preview: DryRunPreview) -> None:
    """Echo the `--dry-run` preview: a full-index note when due, each partition's count and samples, then the other signals."""
    typer.echo("Dry run: previewing the dirty set (nothing written):")
    if preview.would_full:
        typer.echo("  NOTE: a real run would do a FULL rebuild (cold start or fingerprint change), so every file is re-processed.")
    for label, partition in (
        ("added", preview.added),
        ("removed", preview.removed),
        ("content-changed", preview.content_changed),
        ("inbound-closure", preview.inbound_closure),
        ("mirror-citers", preview.mirror_citers),
    ):
        _print_dry_run_partition(label, partition)
    typer.echo(f"pages_to_reembed: {preview.pages_to_reembed} | edges_to_rebuild: {preview.edges_to_rebuild}")
    typer.echo(f"graph_refresh: {'yes' if preview.graph_refresh else 'no'}")


def _print_dry_run_partition(label: str, partition: PartitionPreview) -> None:
    """Echo one preview partition: its label + count, then each sample path (truncation noted)."""
    typer.echo(f"  {label}: {partition.count}")
    for path in partition.sample:
        typer.echo(f"    - {path}")
    if partition.count > len(partition.sample):
        typer.echo(f"    … and {partition.count - len(partition.sample)} more")


async def _index_after_identity(
    container: Resolver, settings: ActiveKnowledgeBase, command: IndexKnowledgeBase, *, collection: str
) -> tuple[KnowledgeBaseIdentity, IndexReport]:
    """Announce the knowledge base, check the migration head, announce the database, then index, returning the identity read before."""
    announce_knowledge_base(settings_knowledge_base_path(settings))
    try:
        await asyncio.to_thread(assert_schema_at_head, settings.application.infrastructure.database.url)
    except SchemaRevisionError as exc:
        _fail(str(exc))
    except _UNREACHABLE_ERRORS as exc:
        typer.echo(unreachable_line(redact_db(settings.application.infrastructure.database.url), exc))
        raise typer.Exit(code=1) from None
    identity = await announce_db(container, settings, collection=collection)
    if identity is None:
        raise typer.Exit(code=1)
    return identity, await _dispatch_index(container, command)


@app.command(name="index")
def index_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    embed: bool = typer.Option(default=True, help="Run the embedding stage. Pass --no-embed to skip it for a fast dev/CI loop."),
    full: bool = typer.Option(default=False, help="Force a whole knowledge base rebuild. The default re-indexes only the changed (dirty) set."),
    dry_run: bool = typer.Option(default=False, help="Preview the dirty set and write nothing (no upserts/embed/graph). Always exits 0."),
    summaries: bool = typer.Option(
        default=False,
        help="Run the disclosure-summary stage after embed. It writes pending "
        "kb.unit_summaries rows only, and you review and promote them with `studio summaries approve`.",
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to index. Defaults to the knowledge base schema's own retrieval.collection."
    ),
) -> None:
    """Index the knowledge base into Postgres: parse, resolve, validate, load and embed.

    By default only the files changed since the last index are indexed again. `--full` indexes the whole
    knowledge base, and `--no-embed` skips embedding with either. `--dry-run` previews the changed set
    (counts and sample paths per partition, and the re-embed, edge and graph refresh signals), writes
    nothing, runs no validation and always exits 0. `--summaries` also writes a pending disclosure summary
    for each unit without a current one. Review the source diff, then run `studio summaries approve` to
    make them active. Exits 1 when any file of the knowledge base carries a validation error, changed in
    this run or not, though the rows still load. Refuses before writing anything when the database is
    behind the code's migration head, naming both revisions and the fix.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    configure_logging(settings.application.logging)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    container = _build_container(ctx, settings, knowledge_base_origin=KnowledgeBasePathOrigin(source=knowledge_base_path_origin(knowledge_base)))
    command = IndexKnowledgeBase(
        collection=partition, embed=embed, full=full, dry_run=dry_run, summaries=summaries, collection_named=collection is not None
    )
    try:
        identity, report = run_naming_connection_failures(
            _index_after_identity(container, settings, command, collection=partition), on_error="index failed", database=settings.database
        )
    except CollectionIsForeignError as exc:
        typer.echo(
            f"collection {exc.collection} was indexed from {exc.stored_root}, this knowledge base is at {exc.knowledge_base_root}. "
            f"Pass --collection {exc.collection} to take the collection over, or index the knowledge base that owns it."
        )
        raise typer.Exit(code=2) from None
    if report.dry_run is not None:
        _print_dry_run_preview(report.dry_run)
        return  # a preview writes nothing and always exits 0
    entries, _ = read_feedback(settings_knowledge_base_path(settings) / FEEDBACK_FILE)
    floor = identity.last_indexed_at
    unreviewed = sum(1 for entry in entries if floor is None or datetime.fromisoformat(entry.created_at) > floor)
    _print_index_report(report, unreviewed=unreviewed)
    standing = report.standing_validation if report.standing_validation is not None else report.validation
    if standing.n_errors > 0:
        raise typer.Exit(code=1)


async def _dispatch_ingest(container: Resolver, command: IngestPage) -> IngestResult:
    """Resolve the shared `MessageBus` and dispatch `command`, returning its ingest result."""
    bus = await container.get(MessageBus)
    result = await bus.handle(command)
    if not isinstance(result, IngestResult):  # pragma: no cover (the ingest handler returns an IngestResult)
        _fail("ingest produced no result.")
    return result


async def _ingest_after_identity(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str, payload_path: str, embed: bool, schema: kb.KnowledgeBaseSchema
) -> IngestResult:
    """Announce the identity, validate the payload, then dispatch the ingest, one event loop for all three."""
    await announce_identity(container, settings, collection=collection)
    try:
        raw = await asyncio.to_thread(Path(payload_path).read_text, encoding="utf-8")
        authored = kb.AuthoringPayload.model_validate({**json.loads(raw), "knowledge_base_schema": schema})
    except (OSError, ValueError) as exc:
        _fail(f"invalid payload: {exc}")
    try:
        return await _dispatch_ingest(container, IngestPage(collection=collection, payload=authored, embed=embed))
    except CollectionIsIndexedError as exc:
        _fail(str(exc))
    except IngestValidationError as exc:
        _print_ingest_findings(exc.findings)
        _fail(f"ingest refused: {len(exc.findings)} vocabulary finding(s)")


_VOCAB_PROPOSAL_HINT = "  propose a new facet value: studio vocab propose --facet <key> --value <value> --page <page id>"


def _print_ingest_findings(findings: Sequence[ValidationIssue]) -> None:
    """Print each refusing finding, then the `studio vocab propose` command when a facet value is unknown."""
    for issue in findings:
        anchor = f" {issue.anchor}" if issue.anchor else ""
        typer.echo(f"  {issue.severity.upper()} {issue.page_id}{anchor} — {issue.rule}: {issue.message}")
    if any(issue.rule == "unknown-facet-value" for issue in findings):
        typer.echo(_VOCAB_PROPOSAL_HINT)


@app.command(name="ingest")
def ingest_cmd(
    ctx: typer.Context,
    *,
    payload: str = typer.Option(..., help="Path to the authoring-payload JSON (the same contract `note build` renders)."),
    collection: str = typer.Option(..., help="Written collection to ingest into. It must be one no knowledge base indexes."),
    embed: bool = typer.Option(default=True, help="Embed the written rows. --no-embed leaves them for a later pass."),
    knowledge_base: str | None = typer.Option(default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."),
) -> None:
    """Write one page straight into Postgres, with no knowledge base file and no index.

    It serves a producer that has no file, such as an extractor, an agent or another service. It takes
    the same `AuthoringPayload` JSON that `note build` renders to markdown, and writes the page, its
    sections and its claims as rows, embedded and found by `api.search` like an indexed page. `--collection` must
    name a collection no knowledge base indexes, because an index deletes every page of its collection
    that no file produced. Naming an indexed one is refused and exits 1.

    The payload's frontmatter is validated against the knowledge base's schema before anything is
    written, as in `note build`. Its entity type, facet values, claim types and relations are checked
    against the knowledge base's vocabulary, and an unregistered value is refused with the finding
    printed, exiting 1. The checks that need the whole knowledge base (duplicate ids, schema and
    vocabulary drift, quote checks) do not run. Run `note validate` for those before ingesting.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    configure_logging(settings.application.logging)
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    result = run_naming_connection_failures(
        _ingest_after_identity(container, settings, collection=collection, payload_path=payload, embed=embed, schema=schema),
        on_error="ingest failed",
        database=settings.database,
    )
    typer.echo(
        f"ingested {result.page_id} into {result.collection}: "
        f"{result.sections} section(s), {result.claims} claim(s), {result.edges} edge(s), "
        f"{result.embedded} embedded / {result.reused} reused"
    )


async def _list_pending_summaries(container: Resolver, *, collection: str) -> Sequence[PendingSummary]:
    """Read `collection`'s pending queue via the shared governance module (read-only)."""
    engine = await container.get(AsyncEngine)
    return await SummaryGovernance(engine).list_pending(collection)


async def _list_pending_summaries_after_identity(
    container: Resolver, settings: ActiveKnowledgeBase, *, collection: str
) -> Sequence[PendingSummary]:
    """Announce the identity, then read the pending queue, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _list_pending_summaries(container, collection=collection)


def _print_pending_summaries(rows: Sequence[PendingSummary], *, collection: str) -> None:
    """Echo one line per pending summary (`id [kind] model=... indexed_at=...`), or a clean empty note."""
    if not rows:
        typer.echo(f"No pending summaries for collection {collection!r}.")
        return
    typer.echo(f"Pending summaries for collection {collection!r}: {len(rows)}")
    for row in rows:
        typer.echo(f"  {row.unit_id}  [{row.unit_type}]  model={row.model}  indexed_at={row.indexed_at}")


@summaries_app.command(name="list")
def summaries_list_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to list pending summaries for, defaults to the knowledge base schema's own retrieval.collection."
    ),
) -> None:
    """List every pending disclosure summary for `--collection`, oldest first (read-only).

    A pending row is one `index --summaries` wrote and no one has reviewed yet: it is never disclosed until
    `studio summaries approve` promotes it to `active`. This command only reads `kb.unit_summaries`.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    rows = _run_command(
        _list_pending_summaries_after_identity(container, settings, collection=partition), on_error="summaries list failed", database=settings.database
    )
    _print_pending_summaries(rows, collection=partition)


async def _approve_pending_summaries(container: Resolver, *, collection: str) -> int:
    """Promote `collection`'s pending summaries to `active` via the shared governance module."""
    engine = await container.get(AsyncEngine)
    return await SummaryGovernance(engine).approve_pending(collection)


async def _approve_pending_summaries_after_identity(container: Resolver, settings: ActiveKnowledgeBase, *, collection: str) -> int:
    """Announce the identity, then approve the pending queue, one event loop for both."""
    await announce_identity(container, settings, collection=collection)
    return await _approve_pending_summaries(container, collection=collection)


@summaries_app.command(name="approve")
def summaries_approve_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to approve pending summaries for, defaults to the knowledge base schema's own retrieval.collection."
    ),
) -> None:
    """Promote every pending disclosure summary of `--collection` to `active`.

    No summary that `index --summaries` writes is disclosed until this runs. Run it after reviewing the
    source diff of the summarized pages and sections in git. It changes only database state and reads
    nothing from the knowledge base. Run `index --summaries` again to refresh a summary whose source
    changed.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    configure_logging(settings.application.logging)
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    count = _run_command(
        _approve_pending_summaries_after_identity(container, settings, collection=partition), on_error="summaries approve failed", database=settings.database
    )
    typer.echo(f"Approved {count} pending summary(ies) for collection {partition!r}.")


@app.command(name="status")
def status_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to report index drift for, defaults to the knowledge base schema's own retrieval.collection."
    ),
) -> None:
    """Print the row count of each `kb` table, the embedding metadata and the index drift.

    The index drift block shows the last index's mode (`incremental` when `--collection` has a stored
    fingerprint and tracked files, `full` otherwise), the count of tracked files, the stored fingerprint,
    and a hint when the next index will run full after a cold start or an indexer version change.
    `status` never writes.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    asyncio.run(_status_after_identity(container, settings, collection=partition))


_FACET_OPTION = typer.Option(
    [], "--facet", help="Pre-filter facet as key=value. Repeatable, and multiple values for one key accumulate."
)


def _parse_facets(pairs: list[str]) -> dict[str, list[str]]:
    """Parse repeated `--facet key=value` options into `{key: [values...]}`, accumulating repeats of a key."""
    facets: dict[str, list[str]] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            msg = f"--facet expects key=value, got {pair!r}"
            raise typer.BadParameter(msg)
        facets.setdefault(key, []).append(value)
    return facets


@app.command(name="search")
def search_cmd(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="The natural-language search query."),
    k: int = typer.Option(12, "-k", "--k", help="Number of results to return."),
    level: str | None = typer.Option(default=None, help="Restrict to a hit level (card|section|claim|raw)."),
    facet: list[str] = _FACET_OPTION,
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to search, defaults to the knowledge base schema's own retrieval.collection."
    ),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    *,
    bm25_only: bool = typer.Option(default=False, help="Skip embedding the query, so retrieval is BM25-only (no Azure needed)."),
) -> None:
    """Search the indexed knowledge base with BM25 and vectors together, or with BM25 alone under `--bm25-only`.

    Each `--facet key=value` narrows the search on that facet key. Repeat it to accumulate several values
    under one key or to filter on several keys at once. A hit's folded units, more matching units of its
    document, print indented under it as `id  [level]`, each with its snippet on the line below, and `studio fetch ID`
    reads one. `--bm25-only` never embeds the query, so it needs
    no embedding provider. An omitted `--collection` uses the knowledge base's own declared collection. A
    failed read, such as an unreachable database or a query the role refuses, exits 1 with a message.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    message = Search(query=query, k=k, level=level, facets=_parse_facets(facet), collection=partition, hybrid=not bm25_only)
    result = _run_command(
        _search_with_identity(container, settings, message, collection=partition, hybrid=not bm25_only), on_error="search failed", database=settings.database
    )
    _print_search_hits(result)


@app.command(name="fetch")
def fetch_cmd(
    ctx: typer.Context,
    unit_id: str = typer.Argument(..., help="The page, section, claim, or raw-chunk id to fetch."),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to fetch from, defaults to the knowledge base schema's own retrieval.collection."
    ),
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
) -> None:
    """Fetch one unit's full text by id, exiting 1 when the id matches nothing.

    An id is unique only within its collection, so `--collection` picks the collection the id is looked
    up in, and defaults to the knowledge base's own declared collection. A failed read, such as an
    unreachable database or a query the role refuses, exits 1 with a message.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    result = _run_command(_fetch_with_identity(container, settings, Fetch(id=unit_id), partition), on_error="fetch failed", database=settings.database)
    _print_fetch_unit(result, unit_id)


@app.command(name="mcp")
def mcp_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    authoring: bool = typer.Option(
        default=False,
        help="Opt in to the write-capable authoring + tuning + recommendation tools (propose_eval_seeds / submit_eval_cases / diagnose / evaluate_policy / submit_tuning / diagnose_structure / what_if_edges / submit_recommendation). Off by default, to keep the surface read-only.",
    ),
    doc_authoring: bool = typer.Option(
        default=False,
        help="Opt in to the documentation-authoring tools (knowledge_base_profile, skeleton, write_note, decide_page, resolve_entities, read_page) "
        "for this knowledge base plus every other registered knowledge base that shares and opts in, even when this knowledge base's "
        "own knowledge-base-schema.json sets retrieval.doc_authoring to off. A knowledge base that always wants it sets that instead.",
    ),
) -> None:
    """Run the MCP retrieval server over stdio.

    `--knowledge-base .` serves the knowledge base in the current directory, which is how a plugin runs
    it. No `application_settings.json` is needed, and the database URL and dialect come from the
    environment. The tools served follow the knowledge base's `retrieval.profile` in
    `knowledge-base-schema.json`, for example `code-mode` adds `run_code`.

    `--authoring` also serves the write tools: `propose_eval_seeds` and `submit_eval_cases` to write
    evaluation cases, `diagnose`, `evaluate_policy` and `submit_tuning` to tune a retrieval policy by
    hand, and `diagnose_structure`, `what_if_edges` and `submit_recommendation` to propose schema changes
    that a person approves. It is off by default.

    `--doc-authoring` also serves `knowledge_base_profile`, `skeleton`, `write_note`, `decide_page`,
    `resolve_entities` and `read_page`, the tools a writing step uses to read a knowledge base's conventions, draft a blank
    page of one page type, write and index it, move a page between its page type's statuses, turn the
    names a page mentions into entity page ids, and read an indexed page back as a write_note payload before rewriting it. They reach this knowledge base and every other registered
    one that is shared and sets `retrieval.doc_authoring`. Each tool's `knowledge_base` argument picks one,
    and `api.about` lists them. A knowledge base whose schema sets
    `retrieval.doc_authoring` gets these tools without the flag.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    server = asyncio.run(
        _mcp_server_after_identity(
            container, settings, collection=schema.retrieval.collection, authoring=authoring, doc_authoring=doc_authoring
        )
    )
    server.run(transport="stdio")


@app.command(name="sync")
def sync_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    dry_run: bool = typer.Option(
        default=False,
        help="Plan only: print each item's action without writing mirrors or converting. On a connector-backed knowledge base "
        "(e.g. youtube), the listing phase itself still fetches each item over the network and updates the source queue "
        "before the plan is even computed. Only the mirror write and the conversion are skipped.",
    ),
    limit: int | None = typer.Option(default=None, help="Process at most N items. A limited run never archives orphans."),
) -> None:
    """Bring the knowledge base's `Sources/` mirrors in line with its connector: convert changed PDFs, refresh metadata, archive orphans.

    It first scans the knowledge base's watch list, when it has one, and adds what is new to the source
    queue. Then it syncs the mirrors and prints both reports. Exits 1 when any item failed. Items whose
    PDF could not be read are listed apart and do not fail the run. A dry run never fails.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    configure_logging(settings.application.logging)
    container = _build_container(ctx, settings)
    schema = load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings))
    scan, report = run_naming_connection_failures(
        _sync_after_identity(container, settings, SyncMirrors(dry_run=dry_run, limit=limit), collection=schema.retrieval.collection),
        on_error="sync failed",
        database=settings.database,
    )
    if scan is not None:
        _print_scan_report(scan)
    _print_sync_report(report)
    if report.errors:
        raise typer.Exit(code=1)


@export_app.command(name="okf")
def export_okf_cmd(
    ctx: typer.Context,
    *,
    knowledge_base: str | None = typer.Option(
        default=None, help="Knowledge base root, defaults to the working directory when it holds a knowledge-base-schema.json."
    ),
    out: str = typer.Option(..., "--out", help="Destination directory for the OKF v0.1 export."),
    collection: str | None = typer.Option(
        default=None, help="Knowledge base partition to export, defaults to the knowledge base schema's own retrieval.collection."
    ),
) -> None:
    """Write one collection to disk as a conformant OKF v0.2 export directory.

    `--knowledge-base` names the knowledge base, the working directory otherwise, as for `index` and
    `sync`. It parses, resolves and projects the notes, writes the files, then prints a report. A failed
    export, such as a missing or invalid knowledge base path, exits 1 with a message. The export can
    always be rebuilt, and this never changes the knowledge base or the database.
    """
    settings = _resolve_settings(knowledge_base, cwd=Path.cwd())
    configure_logging(settings.application.logging)
    container = _build_container(ctx, settings)
    partition = resolve_partition(collection, load_knowledge_base_schema_or_fail(settings_knowledge_base_path(settings)))
    report = _run_command(
        _export_with_identity(container, settings, ExportOkf(collection=partition, out_dir=out)), on_error="export failed", database=settings.database
    )
    _print_export_report(report)


def build_knowledge_base_worker(
    *, jobs: JobsAndQueries, bus: MessageBus, wake: Wake, settings_path: Path, name: str, observer: JobObserver | None = None
) -> Worker:
    """Build the background worker that runs refresh jobs, ticks the schedules, and delivers the outbox.

    Args:
        jobs: The queue it claims `kb.refresh` jobs from and the ticker reads due-ness off.
        bus: The bus a delivered event's subscriptions come from. Its outbox is what gets delivered.
        wake: The wake-up signal the loop waits on between passes.
        settings_path: The settings JSON whose registry of knowledge bases both the ticker and each job read again.
        name: This worker's lease-holder name, unique per process.
        observer: Told after every job transition this worker causes. `None` watches nothing.

    Returns:
        The worker, not yet running.
    """
    worker = Worker(jobs=jobs, outbox=bus.outbox, bus=bus, wake=wake, name=name, observer=observer)
    worker.serve(
        REFRESH_KIND,
        RefreshJob,
        RefreshKnowledgeBaseJob(settings_path=settings_path, containers=run_container_factory(settings_path), dispose=dispose_container),
        policy=REFRESH_POLICY,
    )

    async def enqueue(knowledge_base: str, entry: KnowledgeBaseEntry) -> None:
        """The ticker's `run_one`: queue the same job the HTTP route queues, stamped as scheduled."""
        del entry
        await jobs.enqueue(REFRESH_KIND, knowledge_base, RefreshJob(knowledge_base=knowledge_base, trigger="schedule"), policy=REFRESH_POLICY)

    ticker = Ticker(settings_path=settings_path, jobs=jobs, run_one=enqueue)
    worker.every(TICK_INTERVAL_SECONDS, ticker.tick, name="knowledge-base-schedules")
    return worker


async def _serve_worker(container: Resolver, settings: ApplicationSettings, settings_path: Path) -> None:
    """Run the knowledge base worker over Postgres until SIGINT or SIGTERM, then close the listener."""
    engine = await container.get(AsyncEngine)
    bus = await container.get(MessageBus)
    jobs = PostgresJobs(engine)
    wake = PostgresWake(settings.infrastructure.database.url)
    broadcaster = Broadcaster(settings.infrastructure.database.url)
    try:
        await broadcaster.start()
        worker = build_knowledge_base_worker(
            jobs=jobs,
            bus=bus,
            wake=wake,
            settings_path=settings_path,
            name=f"{socket.gethostname()}:{os.getpid()}",
            observer=TopicPublisher(broadcaster, key_topics={REFRESH_KIND: REFRESH_TOPIC}),
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for received in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):  # a platform without signal handlers stops on the default handler
                loop.add_signal_handler(received, stop.set)
        _logger.info("knowledge base worker ready")
        await worker.run_forever(stop=stop)
    finally:
        await wake.close()
        await broadcaster.stop()


@app.command(name="worker")
def worker_cmd(ctx: typer.Context) -> None:
    """Run the background worker, claiming refresh jobs, ticking the schedules, and delivering events.

    It runs as one long-lived process beside `serve-http`. It claims `kb.refresh` jobs from
    `coordination.jobs` under a lease, runs each knowledge base's whole sync-then-index pipeline, checks
    every registered knowledge base's declared schedule once a minute, and delivers whatever handlers wrote
    to the outbox. Stops cleanly on SIGINT or SIGTERM.
    """
    settings = fetch_application_settings()
    configure_logging(settings.logging)
    container = _build_container(ctx, application=settings)
    asyncio.run(_serve_worker(container, settings, default_settings_path()))


def _worker_child() -> SupervisedChild:
    """The `studio worker` child this process supervises, on this interpreter and this environment."""

    async def spawn() -> Process:
        return await asyncio.create_subprocess_exec(sys.executable, "-m", "studio.knowledge_base.cli", "worker")

    return SupervisedChild(spawn, name="studio worker")


@app.command(name="serve-http")
def serve_http_cmd(
    *,
    host: str = typer.Option(default="127.0.0.1", help="Interface to bind (default 127.0.0.1, local only)."),
    port: int = typer.Option(default=8100, help="Port to listen on."),
    worker: bool = typer.Option(default=True, help="Run `studio worker` as a supervised child. --no-worker serves the API alone."),
) -> None:
    """Serve the knowledge base HTTP API (`/api`) over uvicorn, bound to 127.0.0.1 by default.

    The app reads its settings from the environment and disposes both database engines on shutdown.
    The default binding keeps the API off every public interface.

    A `studio worker` child runs beside it by default, started with this interpreter and this
    process's environment so it reads the same settings, and restarted with backoff when it exits.
    `--no-worker` leaves it out for a deployment that runs the worker itself. The API queues jobs
    either way and never runs one in its own process.
    """
    settings = fetch_application_settings()
    configure_logging(settings.logging)
    import uvicorn  # noqa: PLC0415 (lazy: fastapi/uvicorn load only when serving, not on every CLI call)

    from engram.evals.harness.contracts import configure_logfire  # noqa: PLC0415 (same: keep logfire off the hot CLI path)

    from studio.knowledge_base.http.contracts import create_app  # noqa: PLC0415 (same: keep the http/fastapi graph off the hot CLI path)

    configure_logfire(settings.infrastructure.logfire)  # best-effort: no-ops (returns False) when no write token is set
    app_instance = create_app(settings, worker=_worker_child() if worker else None)
    server = uvicorn.Server(uvicorn.Config(app_instance, host=host, port=port, log_level="info"))
    server.run()


if __name__ == "__main__":  # pragma: no cover
    app()
