"""The container registrations for everything this app reaches outside its process, each under the type its consumers declare.

They are three database pools, an HTTP client, a broadcaster, a notification channel, a telemetry
backend, a model list and the shared settings file's infrastructure section. A test may replace any one of them and keep the rest.
"""

from __future__ import annotations

import sys

from collections.abc import AsyncIterator
from typing import Protocol

import asyncpg
import httpx

from core.ai.history.contracts import (
    PoolDatabase,
    PostgresContinuationStore,
    PostgresStepStore,
    RequestParametersSink,
    create_record_schema,
)
from core.cqrs.contracts import Broadcaster, PostgresWake, TopicSink, Wake
from core.db import open_asyncpg_pool
from core.observability import LoggingSettings, PriceUpdater, Tracing, get_logger
from core.resolver import Resolver
from core.settings import InfrastructureSettings, read_infrastructure
from engram.config import RerankSettings, read_rerank_settings
from genai_prices.update_prices import UpdatePrices
from wireup import injectable

from harness.adapters.logfire_query import LogfireOff, LogfireQuery, Telemetry
from harness.adapters.model_list import ModelList, OpenRouterListing
from harness.adapters.model_tiers import ModelBuilder, ModelTiers
from harness.adapters.models import ModelFactory
from harness.adapters.notify import MacNotifier, NoopNotifier, Notifier
from harness.adapters.session_log import SessionLog
from harness.adapters.sessions import LiveSessions
from harness.adapters.store import (
    RECORD_NAMESPACE,
    DatabaseBinding,
    RecordDatabase,
    ScheduleDatabase,
    SessionsDatabase,
    bound_databases,
    bound_databases_fell_back,
    sessions_pool,
    sessions_writer_dsn,
    ssl_terms,
    store_admin_dsn,
)
from harness.adapters.studio_http import StudioApi, studio_http_client
from harness.adapters.topics import BroadcastTopics
from harness.domain.contracts import ConversationHistory, ConversationMessages, SessionEvents, SessionTranscript
from harness.features.running.infrastructure.durable import DurableTurns
from harness.features.scheduling.infrastructure.postgres import schedule_pool
from harness.session import PooledSessions
from harness.settings import (
    ENGRAM_SETTINGS,
    ExternalAgentRegistry,
    ModelSettings,
    StateSettings,
    StudioApiSettings,
    read_external_agents,
    settings_file,
)
from harness.tracing import LogfireTracing


class RecordPoolOpener(Protocol):
    """How this deployment opens the record's pool over the database its sessions are already on."""

    async def __call__(self, sessions: asyncpg.Pool) -> asyncpg.Pool:
        """Open the record's pool, with its schema in place.

        Args:
            sessions: The pool the sessions database is already open on.

        Returns:
            The pool the record reads and writes through.
        """
        ...


async def open_record_pool(sessions: asyncpg.Pool, *, dsn: str) -> asyncpg.Pool:
    """Open the pool whose every connection is born in the record's schema, creating that schema first.

    Every connection has its search path fixed to the record's schema at connect time, so an
    unqualified name resolves only there.

    Args:
        sessions: The pool the sessions database is already open on, which the schema is created over.
        dsn: That same database, for the record's own connections.

    Returns:
        The pool the record reads and writes through.

    Raises:
        RuntimeError: asyncpg answered no pool at all.
    """
    await sessions.execute(f"CREATE SCHEMA IF NOT EXISTS {RECORD_NAMESPACE}")
    return await open_asyncpg_pool(
        dsn, ssl=ssl_terms(dsn), server_settings={"search_path": RECORD_NAMESPACE}, target=f"onto the record in schema {RECORD_NAMESPACE!r}"
    )


@injectable
async def studio_client(settings: StudioApiSettings) -> AsyncIterator[httpx.AsyncClient]:
    """One client on this deployment's studio knowledge base API, closed with the container.

    Args:
        settings: The studio API section.

    Yields:
        The open client.
    """
    async with studio_http_client(settings) as client:
        yield client


@injectable
def studio_api(client: httpx.AsyncClient) -> StudioApi:
    """The same client, under the narrow read a caller outside composition declares.

    Args:
        client: The container's studio API client.

    Returns:
        The same client.
    """
    return client


@injectable
async def sessions_database() -> AsyncIterator[SessionsDatabase]:
    """The pool the session projection reads and writes, closed with the container.

    Yields:
        The sessions database.
    """
    pool = await sessions_pool()
    try:
        yield SessionsDatabase(pool)
    finally:
        await pool.close()


@injectable
def open_pool() -> RecordPoolOpener:
    """How this deployment's record pool is opened: over the sessions database, born in the record's schema.

    Returns:
        The opener.
    """

    async def open_it(sessions: asyncpg.Pool) -> asyncpg.Pool:
        return await open_record_pool(sessions, dsn=sessions_writer_dsn())

    return open_it


@injectable
async def record_database(sessions: SessionsDatabase, open_it: RecordPoolOpener) -> AsyncIterator[RecordDatabase]:
    """The pool the record lives in, on the sessions database, closed with the container.

    Resolving it creates the record's schema and tables, so every composition root that resolves it
    finds them in place.

    Args:
        sessions: The sessions database the record's schema is created on.
        open_it: How the record's own pool is opened.

    Yields:
        The record database.
    """
    pool = await open_it(sessions.pool)
    try:
        await create_record_schema(PoolDatabase(pool))
        yield RecordDatabase(pool)
    finally:
        await pool.close()


@injectable
async def schedule_database() -> AsyncIterator[ScheduleDatabase]:
    """The pool the standing schedules live in, closed with the container.

    Yields:
        The schedule database.
    """
    pool = await schedule_pool()
    try:
        yield ScheduleDatabase(pool)
    finally:
        await pool.close()


@injectable
async def broadcaster() -> AsyncIterator[Broadcaster]:
    """The change channel every producer announces through, started and stopped with the container.

    Yields:
        The started channel.
    """
    channel = Broadcaster(store_admin_dsn())
    await channel.start()
    try:
        yield channel
    finally:
        await channel.stop()


@injectable
def session_log(state: StateSettings, topics: Broadcaster) -> SessionEvents:
    """This deployment's session log, announcing every appended event. Creates the log's folder.

    It is opened with the forgotten marker, so `boot_sweep()` turns a marker an older process left
    into the boundary event resume honours.

    Args:
        state: The state section, which names the log file and the marker.
        topics: The change channel each append is announced on.

    Returns:
        The session log.
    """
    state.session_log.parent.mkdir(parents=True, exist_ok=True)
    return SessionLog(state.session_log, forgotten_marker=state.forgotten_marker, topics=topics)


def _record(database: RecordDatabase) -> PostgresStepStore:
    """The record on its own pool, built only here so every port over it reaches the same rows."""
    return PostgresStepStore(PoolDatabase(database.pool))


@injectable
def record_store(database: RecordDatabase) -> SessionTranscript:
    """Every session's transcript: the record, in the sessions database, on the record's own pool.

    One store serves the checkout and every project. A run belongs to the session whose
    conversation id it carries.

    Args:
        database: The record database.

    Returns:
        The record, as the session transcript.
    """
    return _record(database)


@injectable
def conversation_history(database: RecordDatabase) -> ConversationHistory:
    """The same record, under the narrow read a turn starts from.

    Args:
        database: The record database.

    Returns:
        The record, as the conversation history.
    """
    return _record(database)


@injectable
def conversation_messages(database: RecordDatabase) -> ConversationMessages:
    """The same record, under the read that answers a whole recorded conversation.

    Args:
        database: The record database.

    Returns:
        The record, as the conversation messages.
    """
    return _record(database)


@injectable
def durable_turns(database: RecordDatabase) -> DurableTurns:
    """Where a durable run's turns are kept: the continuation checkpoints and the record beside them.

    Both are on the record's own pool, so a turn's claim and the messages it commits settle in one database.

    Args:
        database: The record database.

    Returns:
        The durable turns store.
    """
    return DurableTurns(checkpoints=PostgresContinuationStore(PoolDatabase(database.pool)), record=_record(database))


@injectable
def request_parameters(database: RecordDatabase) -> RequestParametersSink:
    """The record, written as the sink every request's parameters go to.

    Args:
        database: The record database.

    Returns:
        The record, as the request parameters sink.
    """
    return _record(database)


@injectable
def topics_port(channel: Broadcaster) -> TopicSink:
    """The same channel, under the port a producer declares instead of the concrete class.

    Args:
        channel: The change channel.

    Returns:
        The topics port over it.
    """
    return BroadcastTopics(channel)


@injectable
def held_sessions(resolver: Resolver) -> PooledSessions:
    """The pool this process holds, resolved when a tool asks rather than when it was built.

    Args:
        resolver: The application's own resolver.

    Returns:
        The live sessions lookup.
    """
    return LiveSessions(resolver)


_logger = get_logger(__name__)


@injectable
def infrastructure() -> InfrastructureSettings:
    """The shared settings file's infrastructure section: every external system's address and key.

    Returns:
        The infrastructure section.
    """
    path = settings_file()
    if path != ENGRAM_SETTINGS:
        _logger.warning("no settings file at {missing}, reading the committed example {path}", missing=str(ENGRAM_SETTINGS), path=str(path))
    return read_infrastructure(path)


@injectable
def rerank_settings() -> RerankSettings:
    """The shared settings file's `retrieval.rerank` block: how search hits are re-ranked.

    Returns:
        The re-rank settings, the defaults when the file carries no block.
    """
    return read_rerank_settings(settings_file())


@injectable
def telemetry(infrastructure: InfrastructureSettings) -> Telemetry:
    """The telemetry backend a spend report queries. With no Logfire read token on file, every query says Logfire is off.

    Args:
        infrastructure: The infrastructure section, read for its Logfire read token.

    Returns:
        Logfire's query API, or `LogfireOff` when the settings file carries no `infrastructure.logfire.read_token`.
    """
    logfire = infrastructure.logfire
    if not logfire.read_token:
        _logger.info("no infrastructure.logfire.read_token on file, so spend reports say Logfire is off")
        return LogfireOff()
    return LogfireQuery(base_url=logfire.base_url, token=logfire.read_token)


@injectable
def model_list(infrastructure: InfrastructureSettings) -> ModelList:
    """The one model list this process keeps. No OpenRouter key on file means the pinned catalog alone.

    Args:
        infrastructure: The infrastructure section, read for its OpenRouter key.

    Returns:
        The model list.
    """
    key = infrastructure.openrouter.api_key
    return ModelList(listing=OpenRouterListing(api_key=key) if key else None)


@injectable
def external_agents(state: StateSettings) -> ExternalAgentRegistry:
    """How a caller reads the ACP registry: per call, so an edit reaches the next turn with no restart.

    Args:
        state: The state section, which names the registry file.

    Returns:
        The reader.
    """
    return lambda: read_external_agents(state.external_agents)


@injectable
async def wake() -> AsyncIterator[Wake]:
    """How this process's loop hears that another process has work for it.

    It listens on its own connection to the sessions database, where the commands live. It is closed
    with the container, after the loop that waits on it has been cancelled.

    Yields:
        The wake channel.
    """
    channel = PostgresWake(sessions_writer_dsn())
    try:
        yield channel
    finally:
        await channel.close()


@injectable
def tracing(infrastructure: InfrastructureSettings, logging: LoggingSettings) -> Tracing:
    """Where this process's spans go: Logfire, with the write token on file.

    Args:
        infrastructure: The infrastructure section, read for its Logfire write token.
        logging: The logging section: the service identity, the level and the console choice.

    Returns:
        The tracing backend.
    """
    return LogfireTracing(infrastructure.logfire, logging)


@injectable
def price_updater() -> PriceUpdater:
    """How the price table is refreshed: the price package's own updater, polling in a daemon thread.

    Returns:
        The updater.
    """
    return UpdatePrices()


@injectable
def notifier() -> Notifier:
    """The notification channel: a real one on Darwin, a silent one on every other host.

    Returns:
        The notifier.
    """
    return MacNotifier() if sys.platform == "darwin" else NoopNotifier()


@injectable
def build_model(infrastructure: InfrastructureSettings, models: ModelSettings) -> ModelBuilder:
    """How a tier name becomes a model, with this deployment's provider keys and routes already bound.

    Args:
        infrastructure: The infrastructure section, read for its gateway and OpenRouter keys.
        models: The models section, which carries the routes.

    Returns:
        The model builder.
    """
    return ModelFactory(infrastructure, models)


@injectable
def model_tiers(state: StateSettings, models: ModelList, build: ModelBuilder) -> ModelTiers:
    """The three named tiers: where the owner's entries are stored, what they are checked against, how one is built.

    Args:
        state: The state section, which names where the owner's entries are stored.
        models: The model list the entries are checked against.
        build: How a tier's model is built.

    Returns:
        The model tiers.
    """
    return ModelTiers(state=state, models=models, build=build)


@injectable
def database_binding() -> DatabaseBinding:
    """Which databases this process is about to open, and whether the deployment named them itself.

    Returns:
        The binding.
    """
    return DatabaseBinding(description=bound_databases(), named=not bound_databases_fell_back())


REGISTRATIONS: list[object] = [
    studio_client,
    studio_api,
    sessions_database,
    open_pool,
    record_database,
    schedule_database,
    broadcaster,
    session_log,
    record_store,
    durable_turns,
    conversation_history,
    conversation_messages,
    request_parameters,
    topics_port,
    held_sessions,
    infrastructure,
    rerank_settings,
    telemetry,
    model_list,
    external_agents,
    wake,
    tracing,
    price_updater,
    notifier,
    build_model,
    model_tiers,
    database_binding,
]
"""Everything this app puts in the container, each under the type its consumers declare."""
