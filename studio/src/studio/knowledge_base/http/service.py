"""The registry of knowledge bases: what is registered and indexed, and what a client may create, register, change or ask for.

Every read of the registry of knowledge bases goes to the settings file again, so a write from one request is visible to the next.
Every refusal is a `KnowledgeBaseRefusedError`, and its message is the sentence the HTTP answer carries.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from core.db import redact_db
from core.files import has_hidden_component
from core.observability import get_logger
from engram.identity import LastRun, disk_content_hashes, read_identity
from engram.indexing.contracts import FilesystemKnowledgeBaseReader
from engram.init.contracts import code_head_revision, load_template, scaffold, write_scaffold
from engram.kb import contracts as kb
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.contracts import decide_page, list_decisions
from engram.sources.contracts import SourceQueue, WatchListFile, known_scanners
from pydantic import ValidationError

from studio.knowledge_base.http.api_models import (
    DecisionsResult,
    KnowledgeBaseDetail,
    KnowledgeBaseListEntry,
    KnowledgeBaseListResult,
    OutcomeOut,
    PageDecisionOut,
    RefreshRunsResult,
    RegistrationReport,
    decided_page_out,
)
from studio.knowledge_base.http.exceptions import KnowledgeBaseRefusal, KnowledgeBaseRefusedError, unknown_knowledge_base_error
from studio.knowledge_base.refresh.contracts import REFRESH_KIND, REFRESH_POLICY, RefreshJob, refresh_history
from studio.knowledge_base.settings import KnowledgeBaseEntry, knowledge_base_slug
from studio.knowledge_base.source_queue import QUEUE_CONNECTOR, SOURCE_QUEUE_PATH, source_queue_at
from studio.knowledge_base.template_guidance import copy_template_guidance
from studio.knowledge_base.watch_list import WATCH_LIST_PATH

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.cqrs.contracts import Enqueued, Job, JobQueries, Jobs
    from engram.identity import KnowledgeBaseIdentity
    from engram.init.contracts import InitReport, Template
    from engram.retrieval.contracts import KnowledgeBaseInfo, KnowledgeBasesHandler, KnowledgeBaseValidationHandler
    from engram.sources.contracts import SourceQueueEntry, WatchEntry

    from studio.knowledge_base.http.api_models import (
        KnowledgeBaseCreateRequest,
        PageDecisionRequest,
        RegisterKnowledgeBaseRequest,
        SourceQueueAddRequest,
        WatchEntryAddRequest,
    )
    from studio.knowledge_base.http.internal_contracts import RegisteredKnowledgeBaseQueries, RegisteredKnowledgeBases
    from studio.knowledge_base.refresh.contracts import RefreshRun
    from studio.knowledge_base.settings import DatabaseSettings, Schedule
    from studio.knowledge_base.wiring.contracts import AdminSqlGateway

_logger = get_logger(__name__)

HISTORY_DEPTH = 60
"""How many job rows a history read pulls before it keeps the finished refreshes among them.

It is wide enough that retries queued ahead of the finished refreshes never hide them.
"""


def entry_schema(entry: KnowledgeBaseEntry | None) -> KnowledgeBaseSchema | None:
    """The schema in a registered knowledge base's folder, or `None` when there is no entry or its schema will not load.

    A knowledge base whose schema is missing or malformed still lists, with the fields its schema fills left empty.

    Args:
        entry: The registered knowledge base, or `None`.

    Returns:
        The schema, or `None`.
    """
    if entry is None:
        return None
    try:
        return KnowledgeBaseSchema.from_path(entry.path / "knowledge-base-schema.json")
    except (OSError, ValueError):
        return None


def knowledge_base_list_entry(name: str, entry: KnowledgeBaseEntry | None, info: KnowledgeBaseInfo | None, broken_raw_path: str | None) -> KnowledgeBaseListEntry:
    """One listing row for `name`, from its registered knowledge base, valid, broken or absent, and its indexed vitals.

    `entry` and `broken_raw_path` are never both set.
    The row is private when the registered knowledge base or its own schema declares it private.

    Args:
        name: The registered or indexed name.
        entry: The registered knowledge base when it passes validation, else `None`.
        info: The indexed vitals, or `None` when it was never indexed.
        broken_raw_path: The unvalidated folder of a registered knowledge base that fails validation, else `None`.

    Returns:
        The listing row.
    """
    return _list_entry(name, entry, info, broken_raw_path, entry_schema(entry))


def _list_entry(
    name: str, entry: KnowledgeBaseEntry | None, info: KnowledgeBaseInfo | None, broken_raw_path: str | None, schema: KnowledgeBaseSchema | None
) -> KnowledgeBaseListEntry:
    """One listing row, built from a schema the caller already loaded."""
    broken = entry is None and broken_raw_path is not None
    return KnowledgeBaseListEntry(
        name=name,
        registered=entry is not None or broken,
        indexed=info is not None,
        broken=broken,
        path=entry.path if entry is not None else (Path(broken_raw_path) if broken_raw_path is not None else None),
        description=entry.description if entry is not None else "",
        pages=info.pages if info is not None else None,
        page_types=info.page_types if info is not None else (),
        last_indexed=info.last_indexed if info is not None else None,
        private=(entry is not None and entry.private) or (schema is not None and schema.retrieval.private),
        collection=schema.retrieval.collection if schema is not None else None,
    )


def sources_connectors(entry: KnowledgeBaseEntry | None) -> tuple[str, ...]:
    """The connectors a registered knowledge base declares, empty when its sources are off or its schema cannot be read.

    Args:
        entry: The registered knowledge base, or `None`.

    Returns:
        The declared connector names.
    """
    return _connectors_of(entry_schema(entry))


def _connectors_of(schema: KnowledgeBaseSchema | None) -> tuple[str, ...]:
    """The connectors `schema` declares, empty when there is no schema or its sources are off."""
    return schema.sources.connectors if schema is not None and schema.sources.enabled else ()


def source_queue(entry: KnowledgeBaseEntry) -> SourceQueue:
    """The knowledge base's source queue at `_meta/source-queue.yaml`, the same file `studio source-queue` works on.

    Args:
        entry: The registered knowledge base.

    Returns:
        The source queue, claimed by the connectors the knowledge base declares.
    """
    return source_queue_at(entry.path, entry_schema(entry))


def read_or_empty[T](read: Callable[[], list[T]], *, what: Path) -> tuple[T, ...]:
    """Read a `_meta/` YAML list, answering an empty tuple when the file cannot be read or parsed.

    A file that fails is logged by its path, and the rest of the detail still renders.

    Args:
        read: Reads the list.
        what: The file `read` reads, named in the log.

    Returns:
        The items, or an empty tuple.
    """
    try:
        return tuple(read())
    except (OSError, yaml.YAMLError, ValidationError) as error:
        _logger.warning("could not read {what}, serving it as empty: {reason}", what=str(what), reason=str(error))
        return ()


def unreadable_error(refusal: KnowledgeBaseRefusal, what: str, name: str, error: OSError | yaml.YAMLError | ValidationError) -> KnowledgeBaseRefusedError:
    """The refusal for a knowledge base's `_meta/` control file that exists but cannot be read or parsed.

    Args:
        refusal: The refusal to carry.
        what: The file in words, such as "the source queue".
        name: The registered name.
        error: What reading the file raised.

    Returns:
        The error to raise.
    """
    return KnowledgeBaseRefusedError(refusal, f"{what} for {name!r} could not be read: {error}")


def knowledge_base_schema(entry: KnowledgeBaseEntry, name: str) -> KnowledgeBaseSchema:
    """The knowledge base's schema, read from its folder.

    Args:
        entry: The registered knowledge base.
        name: The registered name.

    Returns:
        The schema.

    Raises:
        KnowledgeBaseRefusedError: `SCHEMA_NOT_FOUND` when the folder holds no schema, with the folder's own sentence,
            or when the folder's schema file will not load.
    """
    try:
        return kb.load_knowledge_base_schema(entry.path)
    except kb.KnowledgeBaseSchemaMissingError as exc:
        raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.SCHEMA_NOT_FOUND, str(exc)) from exc
    except (OSError, ValueError) as exc:
        msg = f"could not load the schema for {name!r}"
        raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.SCHEMA_NOT_FOUND, msg) from exc


def resolve_create_schema(payload: KnowledgeBaseCreateRequest) -> tuple[KnowledgeBaseSchema, Template | None]:
    """The schema a create request names, and the template it came from.

    Args:
        payload: The create request.

    Returns:
        The schema, and its template, or `None` for an inline schema.

    Raises:
        KnowledgeBaseRefusedError: `SCHEMA_AND_TEMPLATE` when both are given, `UNKNOWN_TEMPLATE` for a slug no template has,
            `TEMPLATE_SCHEMA_UNREADABLE` when the template's own schema will not load, and `SCHEMA_OR_TEMPLATE_REQUIRED` when neither is given.
    """
    if payload.knowledge_base_schema is not None and payload.template is not None:
        msg = "pass schema or template, not both."
        raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.SCHEMA_AND_TEMPLATE, msg)
    if payload.template is not None:
        try:
            template = load_template(payload.template)
        except LookupError as exc:
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.UNKNOWN_TEMPLATE, str(exc)) from exc
        try:
            return KnowledgeBaseSchema.from_path(template.path / "knowledge-base-schema.json"), template
        except (OSError, ValueError) as exc:
            msg = f"template {payload.template!r}'s own knowledge-base-schema.json failed to load: {exc}"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.TEMPLATE_SCHEMA_UNREADABLE, msg) from exc
    if payload.knowledge_base_schema is not None:
        return payload.knowledge_base_schema, None
    msg = "pass schema or template."
    raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.SCHEMA_OR_TEMPLATE_REQUIRED, msg)


def _hidden_directory_refusal(path: Path) -> KnowledgeBaseRefusedError:
    """The refusal for a folder whose path holds a hidden (dot-prefixed) component."""
    return KnowledgeBaseRefusedError(
        KnowledgeBaseRefusal.HIDDEN_DIRECTORY, f"{path} contains a hidden (dot-prefixed) directory component, so it is refused"
    )


def _name_collision_refusal(name: str) -> KnowledgeBaseRefusedError:
    """The refusal for a name the registry of knowledge bases already holds."""
    return KnowledgeBaseRefusedError(KnowledgeBaseRefusal.NAME_COLLISION, f"a knowledge base named {name!r} is already registered")


def _path_collision_refusal(path: Path, registered_name: str) -> KnowledgeBaseRefusedError:
    """The refusal for a folder the registry of knowledge bases already holds under another name."""
    return KnowledgeBaseRefusedError(KnowledgeBaseRefusal.PATH_COLLISION, f"{path} is already registered as {registered_name!r}")


def refuse_target_shape(payload: KnowledgeBaseCreateRequest, schema: KnowledgeBaseSchema) -> None:
    """Refuse a create request whose folder holds a hidden folder, or whose name is not its schema's collection.

    Args:
        payload: The create request.
        schema: The schema it names.

    Raises:
        KnowledgeBaseRefusedError: `HIDDEN_DIRECTORY` or `NAME_COLLECTION_MISMATCH`.
    """
    if has_hidden_component(payload.path):
        raise _hidden_directory_refusal(payload.path)
    if payload.name != schema.retrieval.collection:
        msg = (
            f"the registry name {payload.name!r} must equal the knowledge base's own collection "
            f"{schema.retrieval.collection!r} (registry names are collection namespaces). "
            "Set the schema's retrieval.collection to match, or register under that name instead"
        )
        raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.NAME_COLLECTION_MISMATCH, msg)


def refuse_registry_conflict(payload: KnowledgeBaseCreateRequest, registry: dict[str, KnowledgeBaseEntry]) -> None:
    """Refuse a create request that collides with the registry of knowledge bases or with a folder already in use.

    A new name at a folder already registered is refused here, before the settings would refuse it on write.

    Args:
        payload: The create request.
        registry: The registered knowledge bases that pass validation, by name.

    Raises:
        KnowledgeBaseRefusedError: `NAME_COLLISION`, `PATH_COLLISION`, `INVALID_PATH` when the folder is a file,
            and `DIRECTORY_NOT_EMPTY` when it holds files and `force` is not set.
    """
    if payload.name in registry:
        raise _name_collision_refusal(payload.name)
    colliding_name = next((name for name, entry in registry.items() if entry.path == payload.path), None)
    if colliding_name is not None:
        raise _path_collision_refusal(payload.path, colliding_name)
    if payload.path.exists():
        if not payload.path.is_dir():
            msg = f"{payload.path} exists and is not a directory"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.INVALID_PATH, msg)
        if not payload.force and any(payload.path.iterdir()):
            msg = f"{payload.path} is not empty. Pass force to overwrite"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.DIRECTORY_NOT_EMPTY, msg)


def refuse_create(payload: KnowledgeBaseCreateRequest, schema: KnowledgeBaseSchema, registry: dict[str, KnowledgeBaseEntry]) -> None:
    """Refuse a create request, checking its own shape first and the registry of knowledge bases second.

    Args:
        payload: The create request.
        schema: The schema it names.
        registry: The registered knowledge bases that pass validation, by name.

    Raises:
        KnowledgeBaseRefusedError: Every refusal `refuse_target_shape` and `refuse_registry_conflict` raise.
    """
    refuse_target_shape(payload, schema)
    refuse_registry_conflict(payload, registry)


async def knowledge_base_identity(
    entry: KnowledgeBaseEntry,
    name: str,
    info: KnowledgeBaseInfo | None,
    last_refresh_run: RefreshRun | None,
    *,
    admin: AdminSqlGateway,
    database: DatabaseSettings,
) -> KnowledgeBaseIdentity | None:
    """The knowledge base's identity, read through `admin`, or `None` when no schema can be read for it.

    The note folders and files walked come from the schema in its folder.
    The walk takes the default sources folder, `Sources`, and runs off the event loop.
    A missing schema is logged at info, and one that will not load at warning.

    Args:
        entry: The registered knowledge base.
        name: The registered name.
        info: The indexed vitals, or `None` when it was never indexed.
        last_refresh_run: The newest finished refresh run, or `None`.
        admin: The admin gateway the identity is read through.
        database: The database the identity names, with its password redacted.

    Returns:
        The identity, or `None`.
    """
    try:
        schema = kb.load_knowledge_base_schema(entry.path)
    except kb.KnowledgeBaseSchemaMissingError as exc:
        _logger.info("knowledge base identity for {knowledge_base!r}: {reason}", knowledge_base=name, reason=str(exc))
        return None
    except (OSError, ValueError) as exc:
        _logger.warning("knowledge base identity for {knowledge_base!r}: its knowledge-base-schema.json could not be read: {reason}", knowledge_base=name, reason=str(exc))
        return None
    reader = FilesystemKnowledgeBaseReader(entry.path, note_folders=schema.note_folders, note_files=schema.note_files)
    on_disk = await asyncio.to_thread(disk_content_hashes, reader)
    return await read_identity(
        admin,
        on_disk=on_disk,
        slug=knowledge_base_slug(entry.path),
        root=entry.path,
        database=redact_db(database.url),
        collection=info.name if info is not None else name,
        code_revision=code_head_revision(),
        last_run=(
            LastRun(finished_at=last_refresh_run.finished_at, outcome=last_refresh_run.outcome, detail=last_refresh_run.detail)
            if last_refresh_run is not None
            else None
        ),
    )


def refuse_register(name: str, payload: RegisterKnowledgeBaseRequest, registry: dict[str, KnowledgeBaseEntry]) -> None:
    """Refuse registering a folder under `name`: a hidden folder first, then a name or a folder already in use.

    A name already registered at the same folder is not a collision, so registering it again passes.
    A name registered at another folder is refused, so moving it takes an unregister and then a register.

    Args:
        name: The registered name.
        payload: The registration request.
        registry: The registered knowledge bases that pass validation, by name.

    Raises:
        KnowledgeBaseRefusedError: `HIDDEN_DIRECTORY`, `NAME_COLLISION` or `PATH_COLLISION`.
    """
    if has_hidden_component(payload.path):
        raise _hidden_directory_refusal(payload.path)
    existing = registry.get(name)
    if existing is not None and existing.path != payload.path:
        raise _name_collision_refusal(name)
    colliding_name = next((other for other, entry in registry.items() if entry.path == payload.path and other != name), None)
    if colliding_name is not None:
        raise _path_collision_refusal(payload.path, colliding_name)


def refuse_refresh(name: str, registry: dict[str, KnowledgeBaseEntry], broken: dict[str, str | None]) -> None:
    """Refuse a refresh of a name that is registered but fails validation, or that is not registered at all.

    Args:
        name: The registered name.
        registry: The registered knowledge bases that pass validation, by name.
        broken: The registered knowledge bases that fail validation, each with its unvalidated folder.

    Raises:
        KnowledgeBaseRefusedError: `BROKEN_KNOWLEDGE_BASE`, checked first, or `UNKNOWN_KNOWLEDGE_BASE`.
    """
    if name in registry:
        return
    if name in broken:
        raw_path = broken[name]
        where = f" ({raw_path})" if raw_path else ""
        msg = (
            f"knowledge base {name!r} is registered but its directory{where} is unusable. Re-register it at its current path "
            f"(PUT /api/knowledge-bases/{name}/registration) or remove the stale row (DELETE /api/knowledge-bases/{name}/registration) "
            "before refreshing it"
        )
        raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.BROKEN_KNOWLEDGE_BASE, msg)
    raise unknown_knowledge_base_error(name)


async def refresh_runs_of(name: str, *, jobs: JobQueries, limit: int) -> tuple[RefreshRun, ...]:
    """The newest `limit` finished refreshes of `name`, read from its job rows.

    Args:
        name: The registered name.
        jobs: The reads of the jobs table.
        limit: The most refresh runs to answer.

    Returns:
        The refresh runs, newest first.
    """
    return refresh_history(await jobs.recent_jobs(REFRESH_KIND, name, limit=HISTORY_DEPTH), limit=limit)


class KnowledgeBaseRegistry:
    """The registry of knowledge bases: lists, reads, creates, registers and changes the registered knowledge bases.

    There is no default knowledge base. A method that acts on one knowledge base takes its name from the caller.
    """

    def __init__(
        self,
        registered_knowledge_bases: RegisteredKnowledgeBases,
        queries: RegisteredKnowledgeBaseQueries,
        database: DatabaseSettings,
        *,
        indexed_knowledge_bases: KnowledgeBasesHandler,
        validation: KnowledgeBaseValidationHandler,
        admin: AdminSqlGateway,
        jobs: Jobs,
        job_queries: JobQueries,
    ) -> None:
        """Hold the ports over the registry of knowledge bases, the database it names, and the reads and the job store behind it.

        Args:
            registered_knowledge_bases: The writes to the registry of knowledge bases.
            queries: The reads of the registry of knowledge bases.
            database: The database a knowledge base's identity names.
            indexed_knowledge_bases: Reads the indexed vitals of every knowledge base.
            validation: Reads the standing validation issues of one knowledge base.
            admin: The admin gateway a knowledge base's identity is read through.
            jobs: The writes to the jobs table, where a refresh is enqueued.
            job_queries: The reads of the jobs table.
        """
        self._registered_knowledge_bases = registered_knowledge_bases
        self._queries = queries
        self._database = database
        self._indexed_knowledge_bases = indexed_knowledge_bases
        self._validation = validation
        self._admin = admin
        self._jobs = jobs
        self._job_queries = job_queries

    def _registered_entry(self, name: str) -> KnowledgeBaseEntry:
        """The registered knowledge base `name` that passes validation, or the refusal for a name nothing registers."""
        entry = self._queries.registered().get(name)
        if entry is None:
            raise unknown_knowledge_base_error(name)
        return entry

    async def list_knowledge_bases(self) -> KnowledgeBaseListResult:
        """Every name the registry of knowledge bases or the database knows, merged into one row each.

        Returns:
            The rows, sorted by name.
        """
        registry, broken = self._queries.registered_and_broken()
        indexed = await self._indexed_knowledge_bases()
        by_name = {info.name: info for info in indexed.collections}
        names = sorted(set(registry) | set(by_name) | set(broken))
        entries = tuple(knowledge_base_list_entry(name, registry.get(name), by_name.get(name), broken.get(name)) for name in names)
        return KnowledgeBaseListResult(knowledge_bases=entries)

    def read_knowledge_base_schema(self, name: str) -> KnowledgeBaseSchema:
        """The registered knowledge base's schema, read from its folder.

        Args:
            name: The registered name.

        Returns:
            The schema.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`, or `SCHEMA_NOT_FOUND` when the schema file is missing or will not load.
        """
        entry = self._registered_entry(name)
        schema_path = entry.path / "knowledge-base-schema.json"
        try:
            return KnowledgeBaseSchema.from_path(schema_path)
        except (OSError, ValueError) as exc:
            msg = f"could not load {schema_path}: {exc}"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.SCHEMA_NOT_FOUND, msg) from exc

    async def knowledge_base_detail(self, name: str) -> KnowledgeBaseDetail:
        """One knowledge base whole: its listing row, its vitals, its validation, its control files and its last refresh.

        A control file that cannot be read is served as empty.

        Args:
            name: The registered or indexed name.

        Returns:
            The detail.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE` when the name is neither registered nor indexed.
        """
        registry, broken = self._queries.registered_and_broken()
        indexed = await self._indexed_knowledge_bases()
        info = next((candidate for candidate in indexed.collections if candidate.name == name), None)
        entry = registry.get(name)
        if entry is None and info is None and name not in broken:
            msg = f"no knowledge base named {name!r} is registered or indexed"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.UNKNOWN_KNOWLEDGE_BASE, msg)

        issues = (await self._validation(name)).issues if info is not None else ()
        schema = entry_schema(entry)
        connectors = _connectors_of(schema)
        queue_path = entry.path / SOURCE_QUEUE_PATH if entry is not None else Path()
        watch_list_path = entry.path / WATCH_LIST_PATH if entry is not None else Path()
        queued = (
            read_or_empty(source_queue_at(entry.path, schema).entries, what=queue_path) if entry is not None and QUEUE_CONNECTOR in connectors else ()
        )
        watched = read_or_empty(WatchListFile(watch_list_path).entries, what=watch_list_path) if entry is not None else ()
        last_rows = await refresh_runs_of(name, jobs=self._job_queries, limit=1) if entry is not None else ()
        last_refresh_run = last_rows[0] if last_rows else None
        identity = (
            await knowledge_base_identity(entry, name, info, last_refresh_run, admin=self._admin, database=self._database)
            if entry is not None
            else None
        )
        list_entry = _list_entry(name, entry, info, broken.get(name), schema)
        return KnowledgeBaseDetail(
            **list_entry.model_dump(),
            sources_connectors=connectors,
            validation=issues,
            source_queue=queued,
            watch_list=watched,
            last_refresh_run=last_refresh_run,
            schedule=entry.schedule if entry is not None else None,
            identity=identity,
        )

    async def graph_collection(self, knowledge_base: str | None) -> str:
        """The `retrieval.collection` of a registered knowledge base's schema, which its graph is read under.

        There is no default knowledge base, so a missing name is refused.

        Args:
            knowledge_base: The registered name, or `None` when the request named none.

        Returns:
            The schema's `retrieval.collection`.

        Raises:
            KnowledgeBaseRefusedError: `KNOWLEDGE_BASE_REQUIRED` for a missing name,
                and every refusal `read_knowledge_base_schema` raises.
        """
        if knowledge_base is None:
            msg = "the graph read needs a knowledge base name, pass ?knowledge_base=<registered knowledge base>."
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.KNOWLEDGE_BASE_REQUIRED, msg)
        schema = await asyncio.to_thread(self.read_knowledge_base_schema, knowledge_base)
        return schema.retrieval.collection

    async def job_detail(self, job_id: int) -> Job:
        """One job whole, by its id.

        Args:
            job_id: The job's id.

        Returns:
            The job.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_JOB` when no job has the id.
        """
        job = await self._job_queries.read(job_id)
        if job is None:
            msg = f"no job with id {job_id}"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.UNKNOWN_JOB, msg)
        return job

    async def refresh_runs(self, name: str) -> RefreshRunsResult:
        """The last 20 finished refreshes of a registered knowledge base.

        Args:
            name: The registered name.

        Returns:
            The refresh runs, newest first.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`.
        """
        self._registered_entry(name)
        return RefreshRunsResult(refresh_runs=await refresh_runs_of(name, jobs=self._job_queries, limit=20))

    async def list_decisions_for(self, name: str) -> DecisionsResult:
        """Every page of a registered knowledge base waiting for a decision, and the outcomes of each decision page type.

        Args:
            name: The registered name.

        Returns:
            The pages and the outcomes.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`, then every refusal `knowledge_base_schema` raises.
        """
        entry = self._registered_entry(name)
        schema = knowledge_base_schema(entry, name)
        listing = list_decisions(entry.path, schema)
        outcomes_by_page_type: dict[str, tuple[OutcomeOut, ...]] = {}
        for page_type in schema.decision_page_types:
            decision = page_type.decision
            if decision is None:  # narrows for pyright, schema.decision_page_types only ever holds types with one
                continue
            outcomes_by_page_type[page_type.page_type] = tuple(OutcomeOut(status=outcome.status, link=outcome.link) for outcome in decision.outcomes)
        return DecisionsResult(
            rows=tuple(decided_page_out(row) for row in listing.rows), skipped=listing.skipped, outcomes_by_page_type=outcomes_by_page_type
        )

    def create_knowledge_base_at(self, payload: KnowledgeBaseCreateRequest) -> InitReport:
        """Scaffold a knowledge base from its schema, inline or from a template, and register it.

        Every refusal is raised before anything is written.

        Args:
            payload: The create request.

        Returns:
            What the scaffold wrote.

        Raises:
            KnowledgeBaseRefusedError: Every refusal `resolve_create_schema` and `refuse_create` raise,
                and `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """
        schema, template = resolve_create_schema(payload)
        registry = self._queries.registered()
        refuse_create(payload, schema, registry)
        files = scaffold(schema)
        report = write_scaffold(payload.path, files, force=payload.force)
        if template is not None:
            copy_template_guidance(template, payload.path)
        # Built only after the scaffold: its validator refuses a folder with no knowledge-base-schema.json.
        entry = KnowledgeBaseEntry(path=payload.path, description=payload.description)
        self._registered_knowledge_bases.register(payload.name, entry)
        return report

    def register_knowledge_base(self, name: str, payload: RegisterKnowledgeBaseRequest) -> RegistrationReport:
        """Register an existing knowledge base folder under `name`, writing only the registry of knowledge bases.

        Registering a name again at its same folder keeps its schedule and its privacy.
        A new name starts with no schedule and shared.

        Args:
            name: The registered name.
            payload: The registration request.

        Returns:
            The registered knowledge base as it now stands.

        Raises:
            KnowledgeBaseRefusedError: Every refusal `refuse_register` raises, `INVALID_PATH` when the folder is not a knowledge base,
                and `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """
        registry = self._queries.registered()
        refuse_register(name, payload, registry)
        existing = registry.get(name)
        try:
            entry = KnowledgeBaseEntry(
                path=payload.path,
                description=payload.description,
                schedule=existing.schedule if existing is not None else None,
                private=existing.private if existing is not None else False,
            )
        except ValidationError as exc:
            msg = f"{payload.path} is not a knowledge base directory (it needs a knowledge-base-schema.json): {exc}"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.INVALID_PATH, msg) from exc
        self._registered_knowledge_bases.register(name, entry)
        return RegistrationReport(name=name, path=entry.path, description=entry.description, registered=True)

    def unregister_knowledge_base(self, name: str) -> RegistrationReport:
        """Remove the registered knowledge base `name`, leaving its files and its index as they are.

        Args:
            name: The registered name.

        Returns:
            The registered knowledge base as it stood before its removal.

        Raises:
            KnowledgeBaseRefusedError: Every refusal `RegisteredKnowledgeBases.unregister` raises.
        """
        path, description = self._registered_knowledge_bases.unregister(name)
        return RegistrationReport(name=name, path=path, description=description, registered=False)

    def write_schedule(self, name: str, schedule: Schedule | None) -> None:
        """Set or clear the refresh schedule of the registered knowledge base `name`.

        Args:
            name: The registered name.
            schedule: The schedule to set, or `None` to clear it.

        Raises:
            KnowledgeBaseRefusedError: Every refusal `RegisteredKnowledgeBases.write_schedule` raises.
        """
        self._registered_knowledge_bases.write_schedule(name, schedule)

    def queue_source_url(self, name: str, payload: SourceQueueAddRequest) -> SourceQueueEntry:
        """Put one URL on a registered knowledge base's source queue, for `studio sync` to resolve later.

        Args:
            name: The registered name.
            payload: The URL to queue.

        Returns:
            The source-queue row for the URL.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`, `SOURCES_WITHOUT_QUEUE` when no source-queue connector is declared,
                and `SOURCE_QUEUE_UNREADABLE`.
        """
        entry = self._registered_entry(name)
        connectors = sources_connectors(entry)
        if QUEUE_CONNECTOR not in connectors:
            msg = (
                f"knowledge base {name!r} declares no source-queue connector. Set sources to "
                f'{{"enabled": true, "connectors": ["{QUEUE_CONNECTOR}"]}} in its knowledge-base-schema.json to accept URLs'
            )
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.SOURCES_WITHOUT_QUEUE, msg)
        try:
            return source_queue(entry).add(payload.url)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise unreadable_error(KnowledgeBaseRefusal.SOURCE_QUEUE_UNREADABLE, "the source queue", name, exc) from exc

    async def start_refresh_for(self, name: str) -> Enqueued:
        """Enqueue one `kb.refresh` job for a registered knowledge base.

        At most one refresh of a knowledge base is queued or running at a time, across every process.
        A refresh already live is answered, not started again.

        Args:
            name: The registered name.

        Returns:
            The job's id, and whether this call created it.

        Raises:
            KnowledgeBaseRefusedError: Every refusal `refuse_refresh` raises.
        """
        registry, broken = self._queries.registered_and_broken()
        refuse_refresh(name, registry, broken)
        return await self._jobs.enqueue(REFRESH_KIND, name, RefreshJob(knowledge_base=name, trigger="manual"), policy=REFRESH_POLICY)

    def add_watch_list_entry(self, name: str, payload: WatchEntryAddRequest) -> WatchEntry:
        """Add an entry to a registered knowledge base's watch list, or answer the one it already has for the same scanner and ref.

        Args:
            name: The registered name.
            payload: The entry to add.

        Returns:
            The watch-list entry.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`, `UNKNOWN_SCANNER` naming the scanners this machine has,
                and `WATCH_LIST_UNREADABLE`.
        """
        entry = self._registered_entry(name)
        known = known_scanners()
        if payload.scanner not in known:
            msg = f"no scanner named {payload.scanner!r}, this machine has {', '.join(known)}"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.UNKNOWN_SCANNER, msg)
        try:
            return WatchListFile(entry.path / WATCH_LIST_PATH).add(payload.scanner, payload.ref, max_items=payload.max_items)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise unreadable_error(KnowledgeBaseRefusal.WATCH_LIST_UNREADABLE, "the watch list", name, exc) from exc

    def remove_watch_list_entry(self, name: str, scanner: str, ref: str) -> bool:
        """Drop an entry from a registered knowledge base's watch list, whether or not its scanner is still installed.

        Args:
            name: The registered name.
            scanner: The entry's scanner.
            ref: The entry's ref.

        Returns:
            Whether an entry was removed.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE` or `WATCH_LIST_UNREADABLE`.
        """
        entry = self._registered_entry(name)
        try:
            return WatchListFile(entry.path / WATCH_LIST_PATH).remove(scanner, ref)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise unreadable_error(KnowledgeBaseRefusal.WATCH_LIST_UNREADABLE, "the watch list", name, exc) from exc

    def retry_queued_source_row(self, name: str, key: str) -> SourceQueueEntry:
        """Set a `failed` row of a registered knowledge base's source queue back to `requested`.

        Args:
            name: The registered name.
            key: The row's key.

        Returns:
            The row as it now stands.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`, `SOURCE_QUEUE_UNREADABLE`, and `NOTHING_TO_RETRY` when the row is not failed.
        """
        entry = self._registered_entry(name)
        try:
            row = source_queue(entry).retry(key)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise unreadable_error(KnowledgeBaseRefusal.SOURCE_QUEUE_UNREADABLE, "the source queue", name, exc) from exc
        if row is None:
            msg = f"{key!r} is not a failed row"
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.NOTHING_TO_RETRY, msg)
        return row

    async def decide_one_page(self, name: str, page_id: str, payload: PageDecisionRequest) -> PageDecisionOut:
        """Move one page to a new status, write its links, then enqueue the knowledge base's refresh.

        The page is written first. The refresh is the same `kb.refresh` job a refresh request enqueues, with at most one live at a time.

        Args:
            name: The registered name.
            page_id: The page's id.
            payload: The new status, the links and the reason.

        Returns:
            The page as it now reads, with the refresh that indexes it.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE`, every refusal `knowledge_base_schema` raises, `UNKNOWN_PAGE`,
                and `INVALID_DECISION` for an empty status, or a status or a link field the page's page type does not allow.
        """
        entry = self._registered_entry(name)
        schema = knowledge_base_schema(entry, name)
        if not payload.status:
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.INVALID_DECISION, "status is empty. Give one of the statuses the page's page type allows")
        try:
            row = decide_page(entry.path, schema, page_id, payload.status, links=payload.links, reason=payload.reason)
        except LookupError as exc:
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.UNKNOWN_PAGE, str(exc)) from exc
        except ValueError as exc:
            raise KnowledgeBaseRefusedError(KnowledgeBaseRefusal.INVALID_DECISION, str(exc)) from exc
        answer = await self._jobs.enqueue(REFRESH_KIND, name, RefreshJob(knowledge_base=name, trigger="manual"), policy=REFRESH_POLICY)
        return PageDecisionOut(**decided_page_out(row).model_dump(), job_id=answer.job_id, index="queued" if answer.created else "in_progress")
