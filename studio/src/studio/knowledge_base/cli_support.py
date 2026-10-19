"""Helpers the `studio` command modules share: building the container, resolving the knowledge base and printing its identity.

Every command module imports from here, and this module imports none of them. Each command builds its container through
`_build_container`, inside the command and never at import. A test passes a factory through `ctx.obj`, and that factory wins.
"""

from __future__ import annotations

import asyncio
import socket

from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

import typer

from asyncpg.exceptions import InvalidCatalogNameError, InvalidPasswordError
from core.ai.retrieval.contracts import FeedbackEntry
from core.connection_failures import connection_failure_message
from core.cqrs.contracts import ImmediateWake, JobQueries, MemoryJobs, MessageBus, Worker
from core.db import redact_db
from core.observability import get_logger
from core.wiring.contracts import ContainerResolver
from engram.config import KnowledgeBaseSettings
from engram.identity import KnowledgeBaseIdentity, LastRun, disk_content_hashes, read_identity
from engram.indexing.contracts import IndexKnowledgeBase, IndexReport, KnowledgeBaseReader
from engram.init.contracts import code_head_revision
from engram.kb import contracts as kb
from pydantic import ValidationError

from studio.knowledge_base.container import create_container, dispose_container
from studio.knowledge_base.refresh.contracts import REFRESH_KIND, refresh_history
from studio.knowledge_base.settings import (
    KnowledgeBasePathOrigin,
    KnowledgeBasePathSource,
    fetch_application_settings,
    find_knowledge_base_root,
    knowledge_base_slug,
)
from studio.knowledge_base.wiring.contracts import AdminSqlGateway
from studio.knowledge_base.wiring.indexing import indexer_version_changed

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from core.resolver import Resolver
    from core.wiring.contracts import Container
    from engram.indexing.contracts import ValidationIssue

    from studio.knowledge_base.settings import ApplicationSettings

# The errors a failed connection to Postgres raises. A dead port and an unknown host raise an `OSError`
# subtype (`ConnectionRefusedError`, `socket.gaierror`), a wrong password and a wrong database name raise
# an asyncpg error. A query error is not one of them, so a missing schema still reaches the preflight.
_UNREACHABLE_ERRORS: tuple[type[Exception], ...] = (OSError, InvalidPasswordError, InvalidCatalogNameError)

UnreachableReason = Literal["connection refused", "host not found", "timed out", "authentication failed", "database does not exist"]

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ActiveKnowledgeBase:
    """The settings a command runs under, paired with the knowledge base it acts on.

    The knowledge base comes from `--knowledge-base` or the working directory, and the settings come from the settings file.
    """

    application: ApplicationSettings
    knowledge_base: KnowledgeBaseSettings

    @property
    def database(self) -> str:
        """The database URL with its password redacted, as every printed line names it."""
        return redact_db(self.application.infrastructure.database.url)


def _build_container(
    ctx: typer.Context,
    active: ActiveKnowledgeBase | None = None,
    knowledge_base_origin: KnowledgeBasePathOrigin | None = None,
    *,
    application: ApplicationSettings | None = None,
) -> Resolver:
    """Build the command's resolver from a test's `ctx.obj` factory, else a container disposed when the command closes."""
    if ctx.obj is not None:
        injected: Resolver = ctx.obj()
        return injected
    container = create_container(
        active.application if active is not None else (application if application is not None else fetch_application_settings()),
        knowledge_base=active.knowledge_base if active is not None else None,
        knowledge_base_origin=knowledge_base_origin if knowledge_base_origin is not None else KnowledgeBasePathOrigin(),
    )
    ctx.call_on_close(lambda: _dispose_at_command_close(container))
    return ContainerResolver(container)


def _dispose_at_command_close(container: Container) -> None:
    """Dispose a container on a fresh event loop, releasing its connections unclosed and logging any failure."""
    try:
        asyncio.run(dispose_container(container, close_connections=False))
    except Exception as exc:  # noqa: BLE001 (disposal must never fail a completed command, the leak is logged)
        _logger.debug("container disposal at command close failed: {reason}", reason=str(exc))


def _fail(message: str) -> NoReturn:
    """Print `message` and exit with code 1."""
    typer.echo(message)
    raise typer.Exit(code=1)


def load_knowledge_base_schema_or_fail(knowledge_base_path: Path) -> kb.KnowledgeBaseSchema:
    """Load a knowledge base's schema, or print why it is missing and exit with code 1.

    A knowledge base with no `knowledge-base-schema.json` prints the error's own message, which names the fix, and no traceback.

    Args:
        knowledge_base_path: The knowledge base root.

    Returns:
        The knowledge base's schema.
    """
    try:
        return kb.load_knowledge_base_schema(knowledge_base_path)
    except kb.KnowledgeBaseSchemaMissingError as exc:
        _fail(str(exc))


def resolve_partition(explicit: str | None, schema: kb.KnowledgeBaseSchema) -> str:
    """The collection a write goes into: `explicit` when given, else the one the knowledge base's schema declares.

    It never falls back to a fixed name. An explicit value is used as given, and indexing one knowledge base into
    another's collection deletes that collection's own pages, sections, claims and manifest rows.

    Args:
        explicit: The collection the caller named, or `None`.
        schema: The knowledge base's schema, whose `retrieval.collection` is the default.

    Returns:
        The collection name.
    """
    return explicit if explicit is not None else schema.retrieval.collection


def knowledge_base_path_origin(knowledge_base_override: str | None) -> KnowledgeBasePathSource:
    """Where the knowledge base root came from: the flag, or the working directory.

    Args:
        knowledge_base_override: The `--knowledge-base` value, or `None`.

    Returns:
        `"--knowledge-base"` when the flag was given, else `"working directory"`.
    """
    return "--knowledge-base" if knowledge_base_override is not None else "working directory"


def announce_knowledge_base(root: Path, *, err: bool = False) -> None:
    """Print which knowledge base a command acts on and its root, before the command touches any file.

    A relative root such as `--knowledge-base .` prints as its absolute path.

    Args:
        root: The knowledge base root, absolute or relative.
        err: Print to stderr when true.
    """
    resolved = root.resolve()
    typer.echo(f"knowledge base {knowledge_base_slug(resolved)} at {resolved}", err=err)


def _mapped_reason(error: Exception) -> UnreachableReason | None:
    if isinstance(error, InvalidPasswordError):
        return "authentication failed"
    if isinstance(error, InvalidCatalogNameError):
        return "database does not exist"
    if isinstance(error, ConnectionRefusedError):
        return "connection refused"
    if isinstance(error, TimeoutError):
        return "timed out"
    if isinstance(error, socket.gaierror):
        return "host not found"
    return None


def unreachable_reason(error: Exception) -> str:
    """The short, readable reason a connection attempt failed.

    An error it does not recognise gives its class name, so the reason is always one readable line.

    Args:
        error: The error the connection attempt raised.

    Returns:
        The reason, such as `connection refused`.
    """
    mapped = _mapped_reason(error)
    return mapped if mapped is not None else type(error).__name__


def unreachable_line(database: str, error: Exception) -> str:
    """The one line printed in place of a traceback when `database` could not be reached.

    Args:
        database: The database URL with its password redacted.
        error: The error the connection attempt raised.

    Returns:
        The line.
    """
    return f"db {database} unreachable, {unreachable_reason(error)}."


FEEDBACK_FILE = Path("_meta") / "mcp-feedback.jsonl"
"""Where `submit_feedback` appends, relative to the knowledge base root."""


def read_feedback(path: Path) -> tuple[tuple[FeedbackEntry, ...], tuple[int, ...]]:
    """Every entry in a knowledge base's feedback file, newest first, with the numbers of the lines that would not parse.

    A missing file is no feedback, not an error. A line that will not parse is reported by its number and never skipped in silence.

    Args:
        path: The feedback file.

    Returns:
        The entries, newest first, and the numbers of the lines that did not parse.
    """
    if not path.is_file():
        return (), ()
    entries: list[FeedbackEntry] = []
    unreadable: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(FeedbackEntry.model_validate_json(line))
        except ValidationError:
            unreadable.append(number)
    entries.sort(key=lambda entry: entry.created_at, reverse=True)
    return tuple(entries), tuple(unreadable)


def _when(identity: KnowledgeBaseIdentity) -> str:
    """The last index time in local time with its zone, a naive value read as UTC."""
    if identity.last_indexed_at is None:
        return "never"
    when = identity.last_indexed_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _changed_tail(identity: KnowledgeBaseIdentity, *, rebuilds_everything: bool) -> str:
    """How far the knowledge base moved since its last index, and whether the next index rebuilds everything."""
    count = identity.changed_since
    noun = "file" if count == 1 else "files"
    if identity.last_indexed_at is None:
        return f"{count} {noun} awaiting index"
    if rebuilds_everything:
        files = f"{count} {noun} and " if count else ""
        return f"{files}the indexer or its chunk settings changed since, the next index rebuilds everything"
    if count == 0:
        return "nothing changed since"
    return f"{count} {noun} changed since"


def _db_line(identity: KnowledgeBaseIdentity, *, rebuilds_everything: bool) -> str:
    revision = identity.db_revision if identity.db_revision is not None else "no revision"
    reason = f" ({identity.last_index_reason})" if identity.last_index_reason else ""
    # A collection never indexed has no outcome, and "last indexed never" already says so.
    outcome = "" if identity.last_index_outcome == "never" else f", {identity.last_index_outcome}{reason}"
    return (
        f"db {identity.database} at {revision} (code expects {identity.code_revision}), "
        f"last indexed {_when(identity)}{outcome}, {_changed_tail(identity, rebuilds_everything=rebuilds_everything)}"
    )


async def _last_run_of(container: Resolver, active: ActiveKnowledgeBase) -> LastRun | None:
    name = active.application.active_knowledge_base_name(active.knowledge_base)
    if name is None:
        return None
    jobs = await container.get(JobQueries)
    runs = refresh_history(await jobs.recent_jobs(REFRESH_KIND, name, limit=1), limit=1)
    if not runs:
        return None
    newest = runs[0]
    return LastRun(finished_at=newest.finished_at, outcome=newest.outcome, detail=newest.detail)


async def announce_db(
    container: Resolver, active: ActiveKnowledgeBase, *, collection: str, err: bool = False
) -> KnowledgeBaseIdentity | None:
    """Print the database line and return the identity, or print one unreachable line and return `None`.

    A failed connection never raises, and the caller decides what a `None` means. A file on disk that cannot be
    read still raises its own error.

    Args:
        container: The command's resolver.
        active: The settings and knowledge base the command resolved.
        collection: The collection whose index state the line reports.
        err: Print to stderr when true.

    Returns:
        The knowledge base's identity, or `None` when the database could not be reached.
    """
    root = active.knowledge_base.path
    database = active.database
    reader = await container.get(KnowledgeBaseReader)
    on_disk = disk_content_hashes(reader)
    try:
        gateway = await container.get(AdminSqlGateway)
        identity = await read_identity(
            gateway,
            on_disk=on_disk,
            slug=knowledge_base_slug(root),
            root=root,
            database=database,
            collection=collection,
            code_revision=code_head_revision(),
            last_run=await _last_run_of(container, active),
        )
    except _UNREACHABLE_ERRORS as exc:
        typer.echo(unreachable_line(database, exc), err=err)
        return None
    application = active.application
    rebuilds_everything = indexer_version_changed(identity.indexer_version, application.infrastructure.azure_openai, application.ingestion)
    typer.echo(_db_line(identity, rebuilds_everything=rebuilds_everything), err=err)
    return identity


async def announce_identity(container: Resolver, active: ActiveKnowledgeBase, *, collection: str, err: bool = False) -> KnowledgeBaseIdentity:
    """Print the two identity lines, the knowledge base and the database, before a command does any work.

    It exits with code 1 and no traceback when the database cannot be reached. A command that must go on anyway
    calls `announce_knowledge_base` and `announce_db` itself.

    Args:
        container: The command's resolver.
        active: The settings and knowledge base the command resolved.
        collection: The collection whose index state the database line reports.
        err: Print to stderr when true.

    Returns:
        The knowledge base's identity.
    """
    announce_knowledge_base(active.knowledge_base.path, err=err)
    identity = await announce_db(container, active, collection=collection, err=err)
    if identity is None:
        raise typer.Exit(code=1)
    return identity


def run_naming_connection_failures[T](coro: Coroutine[object, object, T], *, on_error: str, database: str | None) -> T:
    """Run `coro` on a fresh event loop, turning a failed connection into one line and an exit with code 1.

    The line is `on_error`, then the service the connection reached and the error's own text. Any other error is raised
    as it is.

    Args:
        coro: The command's work.
        on_error: What the line opens with, such as `index failed`.
        database: The redacted URL of the database the command opens, or None when it opens none.

    Returns:
        What `coro` returns.

    Raises:
        typer.Exit: With code 1, after the line is printed, when a connection to a service failed.
    """
    try:
        return asyncio.run(coro)
    except Exception as exc:
        named = connection_failure_message(exc, database=database)
        if named is None:
            raise
        _fail(f"{on_error}: {named}")


def _run_command[T](coro: Coroutine[object, object, T], *, on_error: str, database: str | None) -> T:
    """Run `coro` like `run_naming_connection_failures`, turning any other error but `typer.Exit` into an exit prefixed `on_error`."""
    try:
        return run_naming_connection_failures(coro, on_error=on_error, database=database)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 (top-level CLI guard: any failure becomes a clean _fail exit)
        _fail(f"{on_error}: {exc}")


class KnowledgeBaseMissingError(ValueError):
    """No knowledge base was named and the working directory is not one."""


def resolve_knowledge_base_root(explicit: str | None, *, cwd: Path) -> Path:
    """The knowledge base a command acts on: `--knowledge-base` when given, else the nearest `knowledge-base-schema.json` above `cwd`.

    Args:
        explicit: The `--knowledge-base` value, or `None`.
        cwd: The folder to search upward from.

    Returns:
        The knowledge base root, an explicit path made absolute.

    Raises:
        KnowledgeBaseMissingError: When neither names a knowledge base. The message names the fix.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    found = find_knowledge_base_root(cwd)
    if found is not None:
        return found
    msg = "No knowledge base named. Pass --knowledge-base PATH, or run inside a knowledge base (a folder holding knowledge-base-schema.json)."
    raise KnowledgeBaseMissingError(msg)


def settings_knowledge_base_path(active: ActiveKnowledgeBase) -> Path:
    """The root of the knowledge base a command resolved.

    Args:
        active: The settings and knowledge base the command resolved.

    Returns:
        The knowledge base root.
    """
    return active.knowledge_base.path


def _resolve_knowledge_base_path(knowledge_base_override: str | None) -> Path:
    """The knowledge base root from `--knowledge-base` or the working directory, or print the fix and exit with code 1."""
    try:
        return resolve_knowledge_base_root(knowledge_base_override, cwd=Path.cwd())
    except KnowledgeBaseMissingError as exc:
        _fail(str(exc))


def _default_knowledge_base_settings(path: Path) -> KnowledgeBaseSettings:
    """Build the `KnowledgeBaseSettings` of a knowledge base root from its path alone."""
    return KnowledgeBaseSettings(path=path)


def _resolve_knowledge_base_for_authoring(
    knowledge_base_override: str | None, *, settings_for: Callable[[Path], KnowledgeBaseSettings] = _default_knowledge_base_settings
) -> tuple[Path, str]:
    """Resolve the knowledge base root and its `sources_subdir` for `note build` and `note verify`, never reading the settings file."""
    try:
        root = resolve_knowledge_base_root(knowledge_base_override, cwd=Path.cwd())
    except KnowledgeBaseMissingError as exc:
        _fail(str(exc))
    return root, settings_for(root).sources_subdir


def _load_vocabulary(knowledge_base_path: Path) -> kb.VocabularyIndex:
    """Read `<knowledge_base>/_meta/vocabulary/*.md` into a `VocabularyIndex`, exiting with a message when that folder is missing."""
    vocabulary_dir = knowledge_base_path / "_meta" / "vocabulary"
    if not vocabulary_dir.is_dir():
        _fail(f"Knowledge base has no vocabulary directory: {vocabulary_dir}")
    documents = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(vocabulary_dir.glob("*.md"))]
    return kb.parse_vocabulary(documents).index


async def _dispatch_index(container: Resolver, command: IndexKnowledgeBase) -> IndexReport:
    """Dispatch `command` on the message bus, then make one delivery pass over the outbox before returning the report."""
    bus = await container.get(MessageBus)
    report = await bus.handle(command)
    await _deliver_once(bus)
    if not isinstance(report, IndexReport):  # pragma: no cover (the index handler returns an IndexReport)
        _fail("index produced no report.")
    return report


async def _deliver_once(bus: MessageBus) -> None:
    """Make one delivery pass over the bus's outbox, over an empty in-memory job store so no job is claimed."""
    await Worker(jobs=MemoryJobs(), outbox=bus.outbox, bus=bus, wake=ImmediateWake(), name="cli").deliver_once()


def _print_index_report(report: IndexReport, *, unreviewed: int = 0) -> None:
    """Echo the human-readable index summary (counts, embeddings, cut plans, validation, duration, unreviewed feedback)."""
    typer.echo(
        f"Indexed in {report.duration_seconds:.1f}s: "
        f"{report.pages} pages, {report.sections} sections, {report.claims} claims, {report.edges} edges"
    )
    tokens = f" ({report.embedding_tokens:,} tokens)" if report.embedding_tokens else ""
    typer.echo(f"Pruned: {report.pruned} | Embedded: {report.embedded}{tokens}, reused: {report.reused}")
    cut_plans = report.cut_plans
    if cut_plans.reused or cut_plans.chosen or cut_plans.filled:
        typer.echo(f"Cut plans: {cut_plans.reused} reused, {cut_plans.chosen} chosen, {cut_plans.filled} filled")
    # The run's own validation is printed last, because a subprocess reindex reads only the lines after it.
    standing = report.standing_validation
    if standing is not None:
        typer.echo(f"Standing in the knowledge base: {standing.n_errors} error(s), {standing.n_warnings} warning(s)")
        for issue in standing.errors:
            if issue not in report.validation.errors:
                typer.echo(_error_line(issue))
    typer.echo(f"Validation: {report.validation.n_errors} error(s), {report.validation.n_warnings} warning(s)")
    if report.validation.ingest_queue:
        typer.echo(f"Ingest queue ({len(report.validation.ingest_queue)}): {', '.join(report.validation.ingest_queue)}")
    for issue in report.validation.errors:
        typer.echo(_error_line(issue))
    if unreviewed:
        noun = "entry" if unreviewed == 1 else "entries"
        pronoun = "it" if unreviewed == 1 else "them"
        typer.echo(f"{unreviewed} feedback {noun} filed since the last index, read {pronoun} with studio feedback")


def _error_line(issue: ValidationIssue) -> str:
    """One validation error as the index summary prints it."""
    return f"  ERROR {issue.page_id}{f' {issue.anchor}' if issue.anchor else ''}: {issue.rule}: {issue.message}"
