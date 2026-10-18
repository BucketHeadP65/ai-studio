"""The knowledge base plugins: the registered knowledge bases, and engram's read and write tools opened on them.

Which knowledge bases are registered is the studio's to answer, so `registered_knowledge_bases` reads it over the
studio's knowledge base API and opens no registry file and no schema file. This checkout's own docs knowledge base is
the one exception, read from its own file by `repo_docs_knowledge_base`. Built on that answer are the directories the
terminal may open and the in-process and cross-process plugins onto the knowledge bases and onto the library.
"""

from __future__ import annotations

import asyncio
import json
import os

from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol
from urllib.parse import quote

import httpx

from core.ai.disclosure.contracts import ProgressiveDisclosure
from core.db import build_reader_engine
from core.observability import get_logger
from engram import (
    KnowledgeBaseConnection,
    KnowledgeBaseSchema,
    SqlKnowledgeBase,
    document_embedder,
    embedded_authoring,
    embedded_knowledge_base,
    embedded_read_tools,
)
from engram.agent_memory.contracts import embedded_memory_store
from engram.authoring.contracts import AuthoringKnowledgeBase, AuthoringTools, ReindexResult, collection_of, reindex_result_from_subprocess
from engram.retrieval.contracts import ReadOnlySqlGateway, Reranking
from engram.sources.contracts import SourceQueue, claimants_for
from fastmcp import Client as FastMCPClient
from fastmcp.client.transports import StdioTransport
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import NullPool
from sqlalchemy.ext.asyncio import create_async_engine

from harness.adapters.embedding import embedding_contract, query_embedder
from harness.adapters.store import (
    KNOWLEDGE_BASE_READER_SECRET_ENV,
    knowledge_base_reader_dsn,
    knowledge_base_reader_secret,
    store_admin_dsn,
    store_writer_dsn,
)
from harness.settings import CHECKOUT, REPO_DOCS_SCHEMA, STUDIO_REPO, WORKFLOW_KNOWLEDGE_BASE, knowledge_base_guidance

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from core.ai.embedding import ModelQueryEmbedder
    from core.ai.retrieval.contracts import SqlResult
    from core.ai.workflows.contracts import Ingest
    from engram import ReadDials, RetrievalKnowledgeBase
    from engram.agent_memory.contracts import EngramMemoryStore
    from engram.authoring.contracts import Reindex
    from sqlalchemy.ext.asyncio import AsyncEngine

    from harness.adapters.studio_http import StudioApi
    from harness.domain.contracts import KnowledgeBaseNames, KnowledgeBaseSurface

_logger = get_logger(__name__)

_NO_KNOWLEDGE_BASES = "No knowledge base is registered with this deployment, so these tools have nothing to read yet."
"""What the SQL plugin says it holds when the registry is empty and this checkout carries no docs knowledge base."""

_DOMAIN_SEPARATOR = "\n\n---\n\n"
"""What separates one knowledge base's `api.about` text from the next in the SQL plugin's domain text."""


_REGISTRY_PATH = "/api/knowledge-bases"
"""The studio knowledge base API's registry listing: every knowledge base registered with this deployment."""


class KnowledgeBaseRow(BaseModel):
    """One knowledge base as the studio's registry listing answers for it, with only the fields the plugins here read.

    `path` is `None` for a knowledge base indexed in the database but registered nowhere, which has no folder to author
    into. `private` is the studio's answer to whether this knowledge base is private or shared, declared when it was
    registered or in its own schema.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    registered: bool = False
    broken: bool = False
    path: Path | None = None
    private: bool = False
    collection: str | None = None


class KnowledgeBaseListing(BaseModel):
    """`GET /api/knowledge-bases`'s body: every knowledge base the studio has registered or indexed."""

    model_config = ConfigDict(frozen=True)

    knowledge_bases: tuple[KnowledgeBaseRow, ...] = ()


@dataclass(frozen=True)
class StudioPatience:
    """How long a registry read waits for the studio to start answering before giving up.

    `attempts` is how many times the read is tried while the studio is not answering yet, the first try included.
    `every` is the pause, in seconds, between one try and the next. The defaults are one try and no wait, so a read at
    request time never stalls on the studio. Only a process that is starting passes `BOOT_PATIENCE`.
    """

    attempts: int = 1
    every: float = 0.0


_DEFAULT_PATIENCE = StudioPatience()

BOOT_PATIENCE: Final = StudioPatience(attempts=30, every=2.0)
"""How long a starting process waits for the studio to answer before it gives up."""


async def _registry_listing(client: StudioApi, patience: StudioPatience = _DEFAULT_PATIENCE) -> KnowledgeBaseListing:
    """The studio's registry listing, validated, retrying only a connection failure, as often as `patience` allows."""
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await client.get(_REGISTRY_PATH)
            response.raise_for_status()
            return KnowledgeBaseListing.model_validate(response.json())
        except httpx.RequestError:
            if attempt >= patience.attempts:
                raise
            _logger.info(
                "the studio at {url} is not answering yet, waiting (try {attempt} of {attempts})",
                url=str(client.base_url),
                attempt=attempt,
                attempts=patience.attempts,
            )
            await asyncio.sleep(patience.every)


def _studio_did_not_answer(client: StudioApi, path: str, error: Exception) -> RuntimeError:
    """The refusal for a studio this app cannot read the registry from: which studio, what was asked, what came back."""
    msg = (
        f"the registry of knowledge bases lives in the studio, and the studio knowledge base API at {client.base_url} did not "
        f"answer GET {path}: {error}. Start it (scripts/stack/stack.sh restart studio-api), then open this app again."
    )
    return RuntimeError(msg)


def knowledge_base_names(client: StudioApi) -> KnowledgeBaseNames:
    """The port a launch reads the knowledge base names through, read fresh from `client` on every call.

    Every knowledge base the studio lists counts, with or without a folder to author into.

    Args:
        client: The studio knowledge base API.

    Returns:
        An async function that answers the names, and raises `RuntimeError` when the studio did not answer or answered
        something that is not its registry.
    """

    async def names() -> frozenset[str]:
        """The registered names, read fresh. Raises `RuntimeError` when the studio did not answer."""
        try:
            listing = await _registry_listing(client)
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError) as error:
            raise _studio_did_not_answer(client, _REGISTRY_PATH, error) from error
        return frozenset(row.name for row in listing.knowledge_bases)

    return names


async def registered_knowledge_base_roots(client: StudioApi, *, patience: StudioPatience = _DEFAULT_PATIENCE) -> tuple[Path, ...]:
    """Every directory the terminal's `cwd` gate may open inside: every registered folder, plus this checkout.

    Read from the studio on every call, with nothing cached, so a knowledge base registered after this process started
    is a legal root on the next connect. Every root is resolved with symlinks followed, so a `cwd` resolved the same way
    can be checked for containment directly. A private knowledge base counts. A studio that does not answer within
    `patience` yields this checkout alone, never an exception.

    Args:
        client: The studio knowledge base API the registered folders are read over.
        patience: How many times, and how far apart, to retry a connection failure before giving up.

    Returns:
        The resolved roots, this checkout last.
    """
    try:
        listing = await _registry_listing(client, patience)
    except (httpx.HTTPError, json.JSONDecodeError, ValidationError) as error:
        _logger.warning(
            "registered_knowledge_base_roots: the studio at {url} did not answer, so this checkout is the only legal root",
            url=str(client.base_url),
            error=error,
        )
        return (CHECKOUT.resolve(),)
    roots = [row.path.expanduser().resolve() for row in listing.knowledge_bases if row.path is not None]
    roots.append(CHECKOUT.resolve())
    return tuple(roots)


def knowledge_base_env() -> Mapping[str, str]:
    """The environment that points a knowledge base subprocess at this deployment's knowledge base database, read-only.

    Both database URLs carry the knowledge base reader role, which is what keeps the subprocess read-only. The reader
    secret goes along, so a private knowledge base is read through its own role there too. The checkout path and the
    collection are left unset, so the subprocess keeps its own.

    Returns:
        The two database URLs, the reader secret and this process's `PATH`.
    """
    reader = knowledge_base_reader_dsn()
    return {
        "APP_INFRASTRUCTURE__DATABASE__URL": reader,
        "APP_INFRASTRUCTURE__DATABASE__READER_URL": reader,
        KNOWLEDGE_BASE_READER_SECRET_ENV: knowledge_base_reader_secret(),
        "PATH": os.environ.get("PATH", ""),
    }


def _engram_mcp(env: Mapping[str, str]) -> StdioTransport:
    merged = {key: value for key, value in {**os.environ, **env}.items() if isinstance(value, str)}
    return StdioTransport(command="uv", args=["run", "studio", "mcp"], cwd=str(STUDIO_REPO), env=merged)


def knowledge_base_stdio_transport() -> StdioTransport:
    """`studio mcp` over stdio, pointed at this deployment's knowledge base database, for a client in another process.

    It serves a client that speaks JSON-RPC from its own process, such as an editor. This app opens the knowledge bases
    in-process through `knowledge_base_capability` instead.

    Returns:
        The transport, which runs `studio mcp` from the studio checkout.
    """
    return _engram_mcp(knowledge_base_env())


def knowledge_base_mcp_toolset() -> MCPToolset[object]:
    """The same knowledge bases as an MCP toolset over `knowledge_base_stdio_transport`, for a client in another process.

    This app reads through `knowledge_base_capability` instead.

    Returns:
        The toolset.
    """
    return MCPToolset(FastMCPClient(knowledge_base_stdio_transport()))


class _LazyQueryEmbedder:
    """Builds the query embedder on first use, and names its deployment once it can be read."""

    def __init__(self) -> None:
        self._embed: ModelQueryEmbedder | None = None

    def _built(self) -> ModelQueryEmbedder:
        if self._embed is None:
            self._embed = query_embedder()
        return self._embed

    @property
    def model_id(self) -> str:
        try:
            return self._built().model_id
        except ValueError:
            return ""

    async def __call__(self, text: str, /) -> Sequence[float]:
        return await self._built()(text)


def knowledge_base_embedder() -> ModelQueryEmbedder:
    """An embedder that places a `vector_query => '<text>'` in the knowledge base's vector space, built on first use.

    engram's settings file is read on first use, not when the embedder is built, and again on each call until it loads.
    A deployment without that file still opens the knowledge
    base, and a statement that needs a vector falls back to BM25 and says so in the note. Its `model_id` is the
    deployment, or empty while the settings file cannot be read.

    Returns:
        An async function from a query's text to its vector, carrying the deployment as `model_id`.
    """
    return _LazyQueryEmbedder()


class DomainReaderGateway(Protocol):
    """The part of a read gateway that reading one knowledge base's `api.about` needs: one capped SQL read."""

    async def rows(self, sql: str, params: Mapping[str, object] | None = ..., *, cap: int = 50) -> SqlResult:
        """Run one SQL read.

        Args:
            sql: The statement, with named parameters.
            params: The values bound to the statement's parameters.
            cap: The most rows to return.

        Returns:
            At most `cap` rows.
        """
        ...


async def _knowledge_base_domain(gateway: DomainReaderGateway, *, collection: str) -> str:
    """`collection`'s indexed `api.about` body, or one neutral sentence when it has none yet."""
    result = await gateway.rows("SELECT body FROM api.about WHERE collection = :collection", {"collection": collection}, cap=1)
    body = result.rows[0].get("body") if result.rows else None
    return body if isinstance(body, str) and body else f"{collection!r} has not indexed an orientation page yet."


@asynccontextmanager
async def _reader_gateway() -> AsyncGenerator[DomainReaderGateway]:
    """The production `DomainReaderGateway`: a throwaway `ReadOnlySqlGateway` over the knowledge base reader DSN, disposed on exit."""
    engine = build_reader_engine(knowledge_base_reader_dsn())
    try:
        yield ReadOnlySqlGateway(engine)
    finally:
        await engine.dispose()


def _target_folder(row: KnowledgeBaseRow) -> Path | None:
    """`row`'s folder when it is shared, else `None`, logged when the studio reports it broken or private."""
    if not row.registered or row.path is None:
        return None
    if row.broken:
        _logger.warning("registered_knowledge_bases: skipping {knowledge_base!r}, the studio reports its registered knowledge base as broken", knowledge_base=row.name)
        return None
    if row.private:
        _logger.debug("registered_knowledge_bases: skipping {knowledge_base!r}, the studio reports it as private to this deployment", knowledge_base=row.name)
        return None
    return row.path


async def _schema_of(client: StudioApi, name: str) -> KnowledgeBaseSchema | None:
    """`name`'s schema from the studio, or `None`, logged, when it cannot serve one. `RuntimeError` when it stops answering."""
    path = f"{_REGISTRY_PATH}/{quote(name, safe='')}/schema"
    try:
        response = await client.get(path)
        response.raise_for_status()
        return KnowledgeBaseSchema.model_validate(response.json())
    except httpx.RequestError as error:
        raise _studio_did_not_answer(client, path, error) from error
    except (httpx.HTTPStatusError, json.JSONDecodeError, ValidationError) as error:
        _logger.warning("registered_knowledge_bases: skipping {knowledge_base!r}, the studio could not serve its schema", knowledge_base=name, error=error)
        return None


def _repo_docs_slug() -> str | None:
    """This checkout's own docs knowledge base slug, or `None` when its schema file cannot be read."""
    try:
        return KnowledgeBaseSchema.from_path(REPO_DOCS_SCHEMA).slug
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def _collides_with_repo_docs(name: str, schema: KnowledgeBaseSchema, repo_docs_slug: str | None) -> bool:
    """Whether `name` or `schema`'s own slug matches the repo-docs knowledge base, logged as a warning when it does."""
    if repo_docs_slug is None or repo_docs_slug not in (name, schema.slug):
        return False
    _logger.warning(
        "registered_knowledge_bases: skipping {knowledge_base!r}, its name or schema slug matches the repo-docs knowledge base ({repo_docs!r}). "
        "This deployment already mounts repo-docs as its own plugin",
        knowledge_base=name,
        repo_docs=repo_docs_slug,
    )
    return True


def _warn_on_disagreeing_names(name: str, schema: KnowledgeBaseSchema) -> None:
    """Log when `name`, `schema.slug` and `schema.retrieval.collection` disagree. The knowledge base still stands."""
    if not name == schema.slug == schema.retrieval.collection:
        _logger.warning(
            "registered_knowledge_bases: {knowledge_base!r} addresses one knowledge base under three names (registry {knowledge_base!r}, "
            "schema slug {slug!r}, collection {collection!r}). A workflow's `collection` argument reaches only the plugins that share its name",
            knowledge_base=name,
            slug=schema.slug,
            collection=schema.retrieval.collection,
        )


async def registered_knowledge_bases(
    client: StudioApi, *, patience: StudioPatience = _DEFAULT_PATIENCE
) -> tuple[AuthoringKnowledgeBase, ...]:
    """Every registered knowledge base as an authoring knowledge base: its name, collection, folder and schema.

    This reads the studio's knowledge base API and no file: its listing for the names, the folders and which of them are
    shared, and its schema route for each one's schema. A studio that is not answering yet is retried as
    `patience` allows. One that still has not answered, or answered something other than its registry, is a refusal
    naming it, never an empty result.

    A knowledge base is skipped, without taking the others down, when it has no folder to author into, when the studio
    reports it broken or private, or when the studio cannot serve its schema. It is also skipped, and logged, when its
    name or its schema's slug equals the repo-docs knowledge base's slug, because that one is mounted as its own plugin.

    One `collection` argument reaches a knowledge base through three names: its registered name (what the authoring
    tools take), its schema's slug (what `search` and `fetch` take) and `retrieval.collection` (what SQL reads). A
    knowledge base whose three names disagree is kept and logged, because one argument then reaches only some of its
    plugins.

    Args:
        client: The studio knowledge base API this deployment reads its registry from.
        patience: How many times, and how far apart, to retry a connection failure before giving up.

    Returns:
        The shared knowledge bases, in the studio's order.

    Raises:
        RuntimeError: That studio did not answer within `patience`, or answered something that is not its registry.
    """
    try:
        listing = await _registry_listing(client, patience)
    except (httpx.HTTPError, json.JSONDecodeError, ValidationError) as error:
        raise _studio_did_not_answer(client, _REGISTRY_PATH, error) from error
    repo_docs_slug = _repo_docs_slug()
    knowledge_bases: list[AuthoringKnowledgeBase] = []
    for row in listing.knowledge_bases:
        folder = _target_folder(row)
        if folder is None:
            continue
        schema = await _schema_of(client, row.name)
        if schema is None or _collides_with_repo_docs(row.name, schema, repo_docs_slug):
            continue
        _warn_on_disagreeing_names(row.name, schema)
        knowledge_bases.append(AuthoringKnowledgeBase(name=row.name, collection=schema.retrieval.collection, path=folder, schema=schema))
    return tuple(knowledge_bases)


def repo_docs_knowledge_base(schema_path: Path = REPO_DOCS_SCHEMA) -> AuthoringKnowledgeBase | None:
    """The repo-docs knowledge base as an authoring knowledge base, named by its own schema's slug.

    The registry never lists it, so the plugins that write into this repository's own docs take it from the checkout.

    Args:
        schema_path: The knowledge base's `knowledge-base-schema.json`. Defaults to this checkout's own docs knowledge base schema.

    Returns:
        The knowledge base, or `None` when `schema_path` is missing or does not load, logged as a warning in the second case.
    """
    if not schema_path.is_file():
        return None
    try:
        schema = KnowledgeBaseSchema.from_path(schema_path)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        _logger.warning("repo_docs_knowledge_base: {path} did not load, the repo-docs knowledge base stays read-only", path=str(schema_path), error=error)
        return None
    return AuthoringKnowledgeBase(name=schema.slug, collection=schema.retrieval.collection, path=schema_path.parent, schema=schema)


async def _readable_knowledge_bases(
    client: StudioApi, registry: Sequence[AuthoringKnowledgeBase] | None = None
) -> tuple[AuthoringKnowledgeBase, ...]:
    """Every knowledge base this deployment reads: `registry`, read from `client` when not given, then this repo's docs."""
    knowledge_bases = tuple(registry) if registry is not None else await registered_knowledge_bases(client)
    repo_docs = repo_docs_knowledge_base()
    return knowledge_bases if repo_docs is None else (*knowledge_bases, repo_docs)


@dataclass(frozen=True)
class KnowledgeBaseReading:
    """How this deployment reads its knowledge bases."""

    guidance: str
    """How to retrieve well from any of them, beneath each one's own applied policy."""
    cross_search: bool = False
    """Whether one read may cross knowledge bases. Off makes `knowledge_base` a required argument on
    `search` and `fetch`, so a step or the chat agent names the base it reads."""


@asynccontextmanager
async def knowledge_base_capability(
    client: StudioApi,
    gateway: Callable[[], AbstractAsyncContextManager[DomainReaderGateway]] = _reader_gateway,
    knowledge_bases: Callable[[StudioApi], Awaitable[Sequence[AuthoringKnowledgeBase]]] = _readable_knowledge_bases,
    reader_dsn: Callable[[], str] = knowledge_base_reader_dsn,
    *,
    reading: KnowledgeBaseReading,
) -> AsyncGenerator[SqlKnowledgeBase[object]]:
    """Every knowledge base this deployment reads, behind engram's two SQL tools, opened in this process.

    What each knowledge base holds is read fresh from its own indexed `api.about`, one section per knowledge base
    under its collection name, and `describe_schema` names each one's page types and example ids from its own schema.
    The plugin reads with no collection of its own, so it sees exactly what `api.visible_collection` allows. Its
    guidance is the deployment's floor alone. Each knowledge base's own policy rides on its search plugin in
    `studio_read_tools` instead.

    The capability takes `object` deps, so every agent here can hold it. The engine underneath lives until the
    context exits.

    Args:
        client: The studio knowledge base API the registry of knowledge bases is read from.
        gateway: Opens the connection `api.about` is read through, before the knowledge bases open.
        knowledge_bases: Reads the knowledge bases this plugin speaks for through `client`, once as it opens.
        reader_dsn: Answers the database the knowledge bases are read from, asked once as the plugin opens. The default
            is this deployment's own derivation. A caller with a database of its own names it instead.
        reading: How this deployment reads, the same shape `studio_read_tools` takes. Only its guidance reaches these
            tools, because each statement names the knowledge bases it reads.

    Yields:
        engram's bare `SqlKnowledgeBase`, never wrapped in `Earned`.

    Raises:
        RuntimeError: The studio did not answer, so which knowledge bases exist is unknown.
        TypeError: `embedded_knowledge_base` wrapped the capability in `Earned`.
    """
    known = tuple(await knowledge_bases(client))
    async with gateway() as reader:
        blocks = [
            f"{knowledge_base.collection}: {await _knowledge_base_domain(reader, collection=knowledge_base.collection)}"
            for knowledge_base in known
        ]
    async with embedded_knowledge_base(
        reader_dsn(),
        domain=_DOMAIN_SEPARATOR.join(blocks) if blocks else _NO_KNOWLEDGE_BASES,
        embed_query=knowledge_base_embedder(),
        guidance=knowledge_base_guidance(None, reading.guidance),
        grounding=[knowledge_base.schema for knowledge_base in known],
    ) as capability:
        if not isinstance(capability, SqlKnowledgeBase):
            msg = "this plugin is opened with spec=None, embedded_knowledge_base should never wrap it in Earned"
            raise TypeError(msg)
        yield capability


class CaptureRunner(Protocol):
    """Run one command and report its exit code together with the tail of its combined output."""

    async def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        """Run the command `argv` and wait for it to end.

        Args:
            argv: The program and its arguments.

        Returns:
            The command's exit code and the last part of its combined stdout and stderr.
        """
        ...


# The most characters of output a failed index's `ReindexResult.detail` carries.
_INDEX_ERROR_TAIL = 1500


async def _run_captured(argv: Sequence[str]) -> tuple[int, str]:
    """The default `CaptureRunner`: run `argv` with stdout+stderr merged, report the exit code and the output tail."""
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    output, _ = await process.communicate()
    text = output.decode(errors="replace").strip()
    return process.returncode if process.returncode is not None else -1, text[-_INDEX_ERROR_TAIL:]


class StudioReindex:
    """`Reindex` over `studio index --knowledge-base <path>`, the same incremental index the ticker runs."""

    def __init__(self, run: CaptureRunner = _run_captured) -> None:
        """Hold the command runner.

        Args:
            run: Runs `studio index` and answers its exit code and output tail. The default starts a real subprocess.
        """
        self._run = run

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Reindex through a `studio index` subprocess, reading the outcome from its exit code and output tail.

        Args:
            knowledge_base: The knowledge base whose checkout is reindexed.

        Returns:
            The outcome of the reindex.
        """
        argv = ("uv", "run", "--project", str(STUDIO_REPO), "studio", "index", "--knowledge-base", str(knowledge_base.path))
        code, tail = await self._run(argv)
        return reindex_result_from_subprocess(knowledge_base, code, tail)


class RunIndexing:
    """The knowledge bases one in-process workflow run wrote under run indexing once per run, indexed together when it ends.

    A run opens one with `hold_run_writes` and calls `flush` when it ends. Once `flush` has begun, `hold`
    refuses, so a write that lands later indexes at once rather than waiting on an index that already ran.
    """

    def __init__(self, run_id: str) -> None:
        """Start with nothing held.

        Args:
            run_id: The run whose writes this holds back.
        """
        self.run_id = run_id
        self._held: dict[str, tuple[AuthoringKnowledgeBase, Reindex]] = {}
        self._flushing = False

    def hold(self, knowledge_base: AuthoringKnowledgeBase, reindex: Reindex) -> bool:
        """Hold back `knowledge_base`'s index until the run ends. A knowledge base held twice is indexed once.

        Args:
            knowledge_base: The knowledge base just written.
            reindex: What indexes it when the run ends.

        Returns:
            True when it is held, False once `flush` has begun and the caller must index it itself.
        """
        if self._flushing:
            return False
        self._held[knowledge_base.name] = (knowledge_base, reindex)
        return True

    async def flush(self) -> None:
        """Index each held knowledge base once, logging every outcome, and finish even when cancelled.

        It never raises for a failed index, so one knowledge base that does not index leaves the others
        indexed. A knowledge base stays held until its index is done. A cancellation that arrives while it
        indexes waits until every held knowledge base is indexed, and is raised after that.

        Raises:
            CancelledError: The task was cancelled while it indexed. It is raised once every index is done.
        """
        self._flushing = True
        cancelled = False
        while self._held:
            name, (knowledge_base, reindex) = next(iter(self._held.items()))
            indexing = asyncio.create_task(reindex(knowledge_base))
            cancelled = await _wait_through_cancellations(indexing) or cancelled
            del self._held[name]
            _log_run_end_index(self.run_id, knowledge_base, indexing)
        if cancelled:
            raise asyncio.CancelledError


async def _wait_through_cancellations(indexing: asyncio.Task[ReindexResult]) -> bool:
    """Wait for `indexing` to finish however often the waiter is cancelled, and answer whether it was."""
    cancelled = False
    while not indexing.done():
        try:
            await asyncio.wait({indexing})
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


def _log_run_end_index(run_id: str, knowledge_base: AuthoringKnowledgeBase, indexing: asyncio.Task[ReindexResult]) -> None:
    """Log how one end-of-run index went: indexed, indexed with validation errors, not indexed, or failed."""
    error = None if indexing.cancelled() else indexing.exception()
    if indexing.cancelled() or error is not None:
        _logger.error(
            "run {run_id} could not index {knowledge_base} at its end", error=error, run_id=run_id, knowledge_base=knowledge_base.name
        )
        return
    result = indexing.result()
    if result.outcome == "not_indexed":
        _logger.warning(
            "run {run_id} could not index {knowledge_base} at its end: {detail}",
            run_id=run_id,
            knowledge_base=knowledge_base.name,
            detail=result.detail,
        )
    elif result.outcome == "validation_errors":
        _logger.warning(
            "run {run_id} indexed {knowledge_base} at its end with {count} validation errors",
            run_id=run_id,
            knowledge_base=knowledge_base.name,
            count=result.findings_total,
        )
    else:
        _logger.info("run {run_id} indexed {knowledge_base} at its end", run_id=run_id, knowledge_base=knowledge_base.name)


_RUN_INDEXING: ContextVar[RunIndexing | None] = ContextVar("harness_run_indexing", default=None)
"""The run indexing of the in-process workflow run the code now executing works for, `None` outside every such run."""


@contextmanager
def hold_run_writes(run_id: str) -> Generator[RunIndexing]:
    """Open run indexing for one in-process workflow run, bound for as long as the block runs.

    Call it from inside the coroutine that executes the run, because a task copies its creator's context
    when it is created and only then does every step the run starts see it. The caller must await `flush`
    before the block ends, because nothing here flushes.

    Args:
        run_id: The run whose writes are held back.

    Yields:
        The run indexing the block's writes are held on.
    """
    held = RunIndexing(run_id)
    token = _RUN_INDEXING.set(held)
    try:
        yield held
    finally:
        _RUN_INDEXING.reset(token)


class RunScopedReindex:
    """`Reindex` for run indexing: a write to a `once_per_run` knowledge base inside a run is indexed when that run ends.

    Inside a run that opened `hold_run_writes`, such a write is held on that run and answers `"deferred"`.
    Every other write indexes at once: one outside every run, and one to a knowledge base whose schema
    says `each_write`. A run on the Temporal engine never opens run indexing, so its writes index after
    each write.
    """

    def __init__(self, inner: Reindex) -> None:
        """Wrap the reindex that does the indexing.

        Args:
            inner: Indexes a knowledge base, at once for a write that is not held and at the run's end for one that is.
        """
        self._inner = inner

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Hold `knowledge_base`'s index on the bound run when its schema asks for that, else index it now.

        Args:
            knowledge_base: The knowledge base just written.

        Returns:
            `"deferred"` for a held write, else the outcome of the index.
        """
        held = _RUN_INDEXING.get() if knowledge_base.schema.run_indexing == "once_per_run" else None
        if held is not None and held.hold(knowledge_base, self._inner):
            return ReindexResult(outcome="deferred", detail=f"{knowledge_base.path} is indexed once run {held.run_id} ends")
        return await self._inner(knowledge_base)


GROUNDING_TOOLS: tuple[str, ...] = ("knowledge_base_profile", "recall_beliefs", "remember_belief", "skip_digest")
"""The tools a step may ask for by name that `AuthoringTools.tools()` leaves out: three grounding tools and `skip_digest`.

The chat agent never reaches them. A step mounts them only through its own grant, as it does `WEB_SEARCH_TOOL`.
"""


def grounding_toolset(plugins: AuthoringTools) -> FunctionToolset[object]:
    """`knowledge_base_profile`, `recall_beliefs`, `remember_belief` and `skip_digest` as the toolset a step's grant mounts.

    Build it once, and narrow it to the names one step was granted with `.filtered(...)`.

    Args:
        plugins: The authoring tools this deployment opened.

    Returns:
        The four tools as one toolset.
    """
    return FunctionToolset[object]([plugins.knowledge_base_profile, plugins.recall_beliefs, plugins.remember_belief, plugins.skip_digest])


def authoring_tools(
    knowledge_bases: Sequence[AuthoringKnowledgeBase],
    *,
    memory_for: Callable[[AuthoringKnowledgeBase], EngramMemoryStore] | None = None,
    current_run: Callable[[], str | None] | None = None,
) -> tuple[AuthoringTools, AsyncEngine]:
    """The write-side tools over every registered knowledge base, plus the admin engine behind them.

    The caller owns the engine and must dispose of it once the tools are no longer in use.

    Args:
        knowledge_bases: The knowledge bases the tools may write into. The tools address each one by its name.
        memory_for: Looks up each knowledge base's agent-memory store, already opened. `None` makes `recall_beliefs`
            and `remember_belief` answer that agent memory is not mounted.
        current_run: Answers the id of the run the calling step works for, or `None` outside every run. A policy's
            `per_pass_cap` counts the references one run queues. `None` means no call is in a run.

    Returns:
        The tools and the admin engine.
    """
    engine = create_async_engine(store_admin_dsn(), poolclass=NullPool)
    plugins = AuthoringTools(
        knowledge_bases={knowledge_base.name: knowledge_base for knowledge_base in knowledge_bases},
        reindex=RunScopedReindex(StudioReindex()),
        admin=ReadOnlySqlGateway(engine),
        queue_for=lambda knowledge_base: SourceQueue(
            knowledge_base.path / "_meta" / "source-queue.yaml", connectors=claimants_for(knowledge_base.schema.sources.connectors)
        ),
        memory_for=memory_for,
        current_run=current_run,
    )
    return plugins, engine


async def _memory_stores(knowledge_bases: Sequence[AuthoringKnowledgeBase], stack: AsyncExitStack) -> dict[str, EngramMemoryStore]:
    """Open one agent-memory store per knowledge base on `stack`, keyed by name, embedding the way indexed pages do."""
    contract = embedding_contract()
    embedder = document_embedder(deployment=contract.deployment, infrastructure=contract.infrastructure, dimensions=contract.dimensions)
    embed_query = knowledge_base_embedder()
    stores: dict[str, EngramMemoryStore] = {}
    for knowledge_base in knowledge_bases:
        stores[knowledge_base.name] = await stack.enter_async_context(
            embedded_memory_store(
                store_admin_dsn(),
                embedder=embedder,
                embed_query=embed_query,
                collection=collection_of(knowledge_base),
                # One pool per knowledge base, so a larger size multiplies the idle connections this process holds.
                pool_min_size=1,
                pool_max_size=2,
            )
        )
    return stores


class StudioReadToolsOpener(Protocol):
    """How a composition opens the studio's knowledge bases. `studio_read_tools` below is the one implementation.

    The reader is yielded as `KnowledgeBaseSurface`, the three members a step's grants are read off. A composition that
    mounts engram's read capability itself checks for it with `isinstance`, so a stand-in that opens no database still
    satisfies this port.
    """

    def __call__(  # noqa: PLR0913 (the call shape of studio_read_tools, patience included)
        self,
        k_source: Callable[[], int] | None = None,
        dials_source: Callable[[], ReadDials] | None = None,
        default_knowledge_base_source: Callable[[], str | None] | None = None,
        *,
        client: StudioApi,
        reading: KnowledgeBaseReading,
        patience: StudioPatience = _DEFAULT_PATIENCE,
        current_run: Callable[[], str | None] | None = None,
        rerank: Reranking | None = None,
    ) -> AbstractAsyncContextManager[tuple[KnowledgeBaseSurface, ProgressiveDisclosure[object], AuthoringTools]]:
        """Open every knowledge base this deployment reads, and yield the reader, its disclosure governor and the plugins.

        Args:
            k_source: The search row cap when the host owns it, or `None` to leave the cap to the agent.
            dials_source: The live read modes when the host owns them, or `None` for hybrid search and leveled reads.
            default_knowledge_base_source: The knowledge base a search without `knowledge_base` reads, or `None`.
            client: The studio knowledge base API the registry of knowledge bases is read from.
            reading: How this deployment reads its knowledge bases.
            patience: How many times, and how far apart, to retry a failed registry read.
            current_run: Answers the id of the run the calling step works for, or `None` outside every run.
            rerank: How every knowledge base's search hits are re-ranked. `None` keeps each search's own order.

        Returns:
            A context manager that yields the reader, its disclosure governor, and the authoring tools.

        Raises:
            RuntimeError: The studio did not answer within `patience`, so which knowledge bases exist is unknown.
        """
        ...


@asynccontextmanager
async def studio_read_tools(  # noqa: PLR0913 (patience and current_run are keywords past the reader's own, passed only by a composition)
    k_source: Callable[[], int] | None = None,
    dials_source: Callable[[], ReadDials] | None = None,
    default_knowledge_base_source: Callable[[], str | None] | None = None,
    *,
    client: StudioApi,
    reading: KnowledgeBaseReading,
    patience: StudioPatience = _DEFAULT_PATIENCE,
    current_run: Callable[[], str | None] | None = None,
    rerank: Reranking | None = None,
) -> AsyncGenerator[tuple[RetrievalKnowledgeBase[object], ProgressiveDisclosure[object], AuthoringTools]]:
    """The studio's knowledge bases behind engram's two read tools, opened in this process.

    One `search` and one leveled `fetch` over every registered knowledge base plus this repository's own docs, each
    with its own domain text and guidance, and `ProgressiveDisclosure` governing what the leveled reads cost in context.
    All of them read over one read-only DSN. A step granted the authoring tools (`write_note`, `request_source`,
    `list_undigested`, `read_source`) may write into any registered knowledge base, and the chat agent narrows the same
    tools to reads. Each knowledge base's agent-memory store is open before the tools are, so a step granted
    `knowledge_base_profile`, `recall_beliefs` or `remember_belief` finds it live, scoped to that knowledge base.

    The tools take `object` deps, so every agent here can hold them.

    Args:
        k_source: The search row cap when this host owns it rather than the agent, read afresh on every search. `None`
            leaves the cap to the agent.
        dials_source: The live read modes (`ReadDials`) when this host owns them, read afresh on every call. `None`
            serves hybrid search and leveled reads.
        default_knowledge_base_source: The knowledge base a search without `knowledge_base` reads, when this host owns
            one. engram reads it only while `cross_search` is on.
        client: The studio knowledge base API the registry of knowledge bases is read from.
        reading: How this deployment reads: the guidance floor beneath each knowledge base's own policy, and whether one
            read may cross knowledge bases.
        patience: How many times, and how far apart, to retry a connection failure on the registry read before giving
            up. One try by default. A process that is starting passes `BOOT_PATIENCE`.
        current_run: Answers the id of the run the calling step works for, or `None` outside every run, read on each
            `request_source` call. `None` means no call is in a run, so no policy's `per_pass_cap` holds anything back.
        rerank: How every knowledge base's search hits are re-ranked. `None` keeps each search's own order.

    Yields:
        The reader, its disclosure governor, and the `AuthoringTools` instance itself, which `grounding_toolset` takes.

    Raises:
        RuntimeError: The studio did not answer within `patience`, so which knowledge bases exist is unknown.
    """
    knowledge_bases = await registered_knowledge_bases(client, patience=patience)
    readable = await _readable_knowledge_bases(client, knowledge_bases)
    async with AsyncExitStack() as stack:
        memory_stores = await _memory_stores(readable, stack)
        plugins, admin_engine = authoring_tools(
            readable, memory_for=lambda knowledge_base: memory_stores[knowledge_base.name], current_run=current_run
        )
        stack.push_async_callback(admin_engine.dispose)
        async with embedded_read_tools(
            *(
                KnowledgeBaseConnection(
                    knowledge_base_reader_dsn(),
                    spec=knowledge_base.schema,
                    embed_query=knowledge_base_embedder(),
                    guidance=knowledge_base_guidance(knowledge_base.path, reading.guidance),
                    reader_secret=knowledge_base_reader_secret(),
                )
                for knowledge_base in knowledge_bases
            ),
            KnowledgeBaseConnection(
                knowledge_base_reader_dsn(), spec=REPO_DOCS_SCHEMA, embed_query=knowledge_base_embedder(), reader_secret=knowledge_base_reader_secret()
            ),
            k_source=k_source,
            dials_source=dials_source,
            cross_search=reading.cross_search,
            default_knowledge_base_source=default_knowledge_base_source,
            authoring=plugins,
            rerank=rerank,
        ) as reader:
            yield (reader, ProgressiveDisclosure[object](), plugins)


@asynccontextmanager
async def store_ingest() -> AsyncGenerator[Ingest]:
    """The library's write path for a saved workflow: engram's validated ingest, opened in this process over the writer DSN.

    A payload is validated against the workflow knowledge base's own schema and lands embedded, so a saved workflow
    ranks in the library's search the same way any indexed page does.

    Yields:
        The ingest function, which writes one validated payload into the collection it names.
    """
    contract = embedding_contract()
    async with embedded_authoring(
        store_writer_dsn(),
        spec=WORKFLOW_KNOWLEDGE_BASE / "knowledge-base-schema.json",
        embedder=document_embedder(deployment=contract.deployment, infrastructure=contract.infrastructure, dimensions=contract.dimensions),
    ) as write:

        async def ingest(payload: Mapping[str, Any], *, collection: str) -> None:
            await write(payload, collection=collection)

        yield ingest
