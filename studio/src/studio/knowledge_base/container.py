"""The composition root: builds the wireup container from the settings and every slice's registrations.

A container built with a knowledge base gets `bus_provider`, which routes every command. One built with none gets
`registry_bus_provider`, which routes no command and delivers events for every registered knowledge base. Each bus
is fully assembled when it is built, and nothing registers on it afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import wireup

from core.cqrs.contracts import MessageBus, PostgresOutbox
from core.observability import get_logger
from core.resolver import Resolver
from core.wiring.contracts import Container, ContainerResolver, sections_of
from engram.evals.commands import (
    CheckFreshness,
    CheckGenerator,
    ComputeCoverage,
    FreezeDataset,
    GenerateEvalDataset,
    RecommendStructure,
    RollbackPolicy,
    ScoreDataset,
    TuneRetrieval,
    VerifyRecommendation,
)
from engram.evals.handlers import (
    CheckFreshnessHandler,
    CheckGeneratorHandler,
    ComputeCoverageHandler,
    FreezeDatasetHandler,
    GenerateEvalDatasetHandler,
    RecommendStructureHandler,
    RollbackPolicyHandler,
    ScoreDatasetHandler,
    TuneRetrievalHandler,
    VerifyRecommendationHandler,
)
from engram.export.contracts import ExportOkf, ExportOkfHandler
from engram.indexing.contracts import (
    CitingPagesChanged,
    IndexKnowledgeBase,
    IndexKnowledgeBaseHandler,
    IngestPage,
    IngestPageHandler,
    PagesLoaded,
)
from engram.sources.contracts import MultiConnectorSync, ScanWatchList, ScanWatchListHandler, SyncMirrors
from sqlalchemy.ext.asyncio import AsyncEngine

from studio.knowledge_base.indexing.infrastructure.harness_forward import HarnessIndexForward
from studio.knowledge_base.qa_routing import KnowledgeBaseQaRouter, pending_pages_queues
from studio.knowledge_base.refresh.contracts import RefreshAbandoned, RefreshCompleted, RefreshStarted
from studio.knowledge_base.refresh.infrastructure.harness_forward import HarnessRefreshForward
from studio.knowledge_base.settings import (
    EvalsSettings,
    KnowledgeBasePathOrigin,
    KnowledgeBaseSettings,
    SettingsFile,
    default_settings_path,
    fetch_application_settings,
)
from studio.knowledge_base.wiring import (
    conversion,
    evals,
    export,
    index_forward,
    indexing,
    mirror,
    refresh,
    serve,
    sources,
    sync,
    watch_list,
)
from studio.knowledge_base.wiring.contracts import ReaderEngine

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType

    from core.cqrs.contracts import Command, CommandHandler, Event, EventSubscriber

    from studio.knowledge_base.settings import ApplicationSettings

_logger = get_logger(__name__)

WIRING_MODULES: tuple[ModuleType, ...] = (
    indexing,
    index_forward,
    serve,
    conversion,
    sources,
    mirror,
    sync,
    watch_list,
    refresh,
    export,
    evals,
)
"""Every slice that registers, read for its `REGISTRATIONS` list. A new slice is one more entry."""


def build_command_routes(
    *,
    index_handler: CommandHandler,
    ingest_handler: CommandHandler,
    sync_handler: CommandHandler,
    scan_handler: CommandHandler,
    export_handler: CommandHandler,
    generate_handler: CommandHandler,
    tune_handler: CommandHandler,
    recommend_handler: CommandHandler,
    verify_handler: CommandHandler,
    coverage_handler: CommandHandler,
    freshness_handler: CommandHandler,
    score_handler: CommandHandler,
    check_generator_handler: CommandHandler,
    freeze_handler: CommandHandler,
    rollback_handler: CommandHandler,
) -> dict[type[Command], CommandHandler]:
    """Build the declarative command route table, one handler per command type.

    It is the one place a command is bound to its handler.

    Args:
        index_handler: Handles `IndexKnowledgeBase`.
        ingest_handler: Handles `IngestPage`.
        sync_handler: Handles `SyncMirrors`.
        scan_handler: Handles `ScanWatchList`.
        export_handler: Handles `ExportOkf`.
        generate_handler: Handles `GenerateEvalDataset`.
        tune_handler: Handles `TuneRetrieval`.
        recommend_handler: Handles `RecommendStructure`.
        verify_handler: Handles `VerifyRecommendation`.
        coverage_handler: Handles `ComputeCoverage`.
        freshness_handler: Handles `CheckFreshness`.
        score_handler: Handles `ScoreDataset`.
        check_generator_handler: Handles `CheckGenerator`.
        freeze_handler: Handles `FreezeDataset`.
        rollback_handler: Handles `RollbackPolicy`.

    Returns:
        The full command route table the bus is constructed from.
    """
    return {
        IndexKnowledgeBase: index_handler,
        IngestPage: ingest_handler,
        SyncMirrors: sync_handler,
        ScanWatchList: scan_handler,
        ExportOkf: export_handler,
        GenerateEvalDataset: generate_handler,
        TuneRetrieval: tune_handler,
        RecommendStructure: recommend_handler,
        VerifyRecommendation: verify_handler,
        ComputeCoverage: coverage_handler,
        CheckFreshness: freshness_handler,
        ScoreDataset: score_handler,
        CheckGenerator: check_generator_handler,
        FreezeDataset: freeze_handler,
        RollbackPolicy: rollback_handler,
    }


def build_event_subscriptions(
    *, qa_subscriber: EventSubscriber, harness_forward: EventSubscriber, index_forward: EventSubscriber
) -> dict[type[Event], list[tuple[str, EventSubscriber]]]:
    """Build the declarative event subscription table, each subscriber under the name its deliveries are recorded by.

    It is the one place an event is bound to its subscribers, and each name is what `coordination.event_deliveries`
    keys on. An event type with no subscribers is entered with an empty list, so the worker closes such an event
    quietly and warns only about a type this table does not declare.

    Args:
        qa_subscriber: The QA-generation subscriber observing `CitingPagesChanged`.
        harness_forward: The subscriber posting each ended refresh run to the harness.
        index_forward: The subscriber posting each index's changed pages to the harness.

    Returns:
        The full event subscription table the bus is constructed from.
    """
    return {
        CitingPagesChanged: [("qa_generation", qa_subscriber)],
        PagesLoaded: [("harness_index_forward", index_forward)],
        RefreshStarted: [],
        RefreshCompleted: [("harness_forward", harness_forward)],
        RefreshAbandoned: [("harness_forward", harness_forward)],
    }


@wireup.injectable
def resolver_provider(container: wireup.AsyncContainer) -> Resolver:
    """Provide the `Resolver` port over this container.

    A factory that must resolve services each time it is called injects `Resolver`, never the container.

    Args:
        container: The container being built.

    Returns:
        A resolver over `container`.
    """
    return ContainerResolver(container)


@wireup.injectable
def bus_provider(
    engine: AsyncEngine,
    index_handler: IndexKnowledgeBaseHandler,
    ingest_handler: IngestPageHandler,
    sync_handler: MultiConnectorSync,
    scan_handler: ScanWatchListHandler,
    export_handler: ExportOkfHandler,
    generate_handler: GenerateEvalDatasetHandler,
    tune_handler: TuneRetrievalHandler,
    recommend_handler: RecommendStructureHandler,
    verify_handler: VerifyRecommendationHandler,
    coverage_handler: ComputeCoverageHandler,
    freshness_handler: CheckFreshnessHandler,
    score_handler: ScoreDatasetHandler,
    check_generator_handler: CheckGeneratorHandler,
    freeze_handler: FreezeDatasetHandler,
    rollback_handler: RollbackPolicyHandler,
    qa_subscriber: KnowledgeBaseQaRouter,
    harness_forward: HarnessRefreshForward,
    index_forward: HarnessIndexForward,
) -> MessageBus:
    """Provide the full `MessageBus`, every command routed, for a container built with a knowledge base.

    The bus writes events to the Postgres outbox, so a handler's events outlive the process. Every event type
    `build_event_subscriptions` declares is known to the bus, including one with no subscribers.

    Args:
        engine: The admin engine the outbox writes through.
        index_handler: Handles `IndexKnowledgeBase`.
        ingest_handler: Handles `IngestPage`.
        sync_handler: Handles `SyncMirrors`.
        scan_handler: Handles `ScanWatchList`.
        export_handler: Handles `ExportOkf`.
        generate_handler: Handles `GenerateEvalDataset`.
        tune_handler: Handles `TuneRetrieval`.
        recommend_handler: Handles `RecommendStructure`.
        verify_handler: Handles `VerifyRecommendation`.
        coverage_handler: Handles `ComputeCoverage`.
        freshness_handler: Handles `CheckFreshness`.
        score_handler: Handles `ScoreDataset`.
        check_generator_handler: Handles `CheckGenerator`.
        freeze_handler: Handles `FreezeDataset`.
        rollback_handler: Handles `RollbackPolicy`.
        qa_subscriber: The QA-generation subscriber observing `CitingPagesChanged`.
        harness_forward: The subscriber posting each ended refresh run to the harness.
        index_forward: The subscriber posting each index's changed pages to the harness.

    Returns:
        The bus with every command route and every event subscription in place.
    """
    subscriptions = build_event_subscriptions(qa_subscriber=qa_subscriber, harness_forward=harness_forward, index_forward=index_forward)
    bus = MessageBus(
        build_command_routes(
            index_handler=index_handler,
            ingest_handler=ingest_handler,
            sync_handler=sync_handler,
            scan_handler=scan_handler,
            export_handler=export_handler,
            generate_handler=generate_handler,
            tune_handler=tune_handler,
            recommend_handler=recommend_handler,
            verify_handler=verify_handler,
            coverage_handler=coverage_handler,
            freshness_handler=freshness_handler,
            score_handler=score_handler,
            check_generator_handler=check_generator_handler,
            freeze_handler=freeze_handler,
            rollback_handler=rollback_handler,
        ),
        {event_type: [] for event_type in subscriptions},
        outbox=PostgresOutbox(engine),
    )
    for event_type, declared in subscriptions.items():
        for name, subscriber in declared:
            bus.register_subscriber(event_type, subscriber, name=name)
    return bus


@wireup.injectable
def qa_router_for_knowledge_base(evals: EvalsSettings, knowledge_base: KnowledgeBaseSettings) -> KnowledgeBaseQaRouter:
    """Provide the QA-generation subscriber for a container built with a knowledge base.

    It routes this knowledge base's events whether or not it is registered, and every registered knowledge base's
    events by their collection. Each index's digest page ids go onto that knowledge base's own file queue beside its
    datasets, for `studio evals gen-dataset` to drain, unless `evals.autogen` is `"off"`.

    Args:
        evals: The evals settings, whose `datasets_dir` places the queues and whose `autogen` turns generation on or off.
        knowledge_base: The knowledge base this container acts on.

    Returns:
        The router over this knowledge base and the registry of knowledge bases.
    """
    return KnowledgeBaseQaRouter(
        settings_path=default_settings_path(),
        active_knowledge_base=knowledge_base.path,
        queue_for=pending_pages_queues(evals.datasets_dir),
        autogen=evals.autogen,
    )


@wireup.injectable
def qa_router_for_registry(evals: EvalsSettings) -> KnowledgeBaseQaRouter:
    """Provide the QA-generation subscriber for a container with no knowledge base of its own.

    It routes each event by the registry of knowledge bases alone, onto the same per-collection queues.

    Args:
        evals: The evals settings, whose `datasets_dir` places the queues and whose `autogen` turns generation on or off.

    Returns:
        The router over the registry of knowledge bases.
    """
    return KnowledgeBaseQaRouter(
        settings_path=default_settings_path(),
        active_knowledge_base=None,
        queue_for=pending_pages_queues(evals.datasets_dir),
        autogen=evals.autogen,
    )


@wireup.injectable
def registry_bus_provider(
    engine: AsyncEngine, qa_subscriber: KnowledgeBaseQaRouter, harness_forward: HarnessRefreshForward, index_forward: HarnessIndexForward
) -> MessageBus:
    """Provide the `MessageBus` for a container with no knowledge base of its own.

    It routes no command and resolves no command handler, so a command goes through the full bus of a container
    built for its knowledge base. It delivers the outbox's events to the same subscriptions as the full bus, the
    QA-generation subscriber routing by the registry of knowledge bases alone.

    Args:
        engine: The admin engine the outbox writes through.
        qa_subscriber: The QA-generation subscriber observing `CitingPagesChanged`.
        harness_forward: The subscriber posting each ended refresh run to the harness.
        index_forward: The subscriber posting each index's changed pages to the harness.

    Returns:
        The bus with no command route and every event subscription in place.
    """
    subscriptions = build_event_subscriptions(qa_subscriber=qa_subscriber, harness_forward=harness_forward, index_forward=index_forward)
    bus = MessageBus({}, {event_type: [] for event_type in subscriptions}, outbox=PostgresOutbox(engine))
    for event_type, declared in subscriptions.items():
        for name, subscriber in declared:
            bus.register_subscriber(event_type, subscriber, name=name)
    return bus


@wireup.injectable(as_type=KnowledgeBaseSettings)
def _refuse_knowledge_base_settings() -> KnowledgeBaseSettings:
    """Stand in for `KnowledgeBaseSettings` in a container with no knowledge base, refusing when anything resolves it."""
    msg = "this process serves the registry and acts on no knowledge base, name one"
    raise LookupError(msg)


def create_container(
    settings: ApplicationSettings,
    *,
    knowledge_base: KnowledgeBaseSettings | None = None,
    knowledge_base_origin: KnowledgeBasePathOrigin | None = None,
) -> wireup.AsyncContainer:
    """Wire the settings sections and every slice's registrations into one async DI container.

    Each settings section is registered under its own type. With a knowledge base, `KnowledgeBaseSettings` resolves
    to it, the bus routes every command and the QA subscriber is seeded with it. With none, `KnowledgeBaseSettings`
    refuses when resolved, the bus routes no command, and the QA subscriber routes by the registry of knowledge bases alone.

    Args:
        settings: The settings this container serves.
        knowledge_base: The knowledge base this process acts on, or `None` for a process that serves
            the registry of knowledge bases.
        knowledge_base_origin: Where the knowledge base's path came from, read only by the index
            slice's validation messages. `None` records the working directory.

    Returns:
        A fully configured wireup async container.
    """
    resolved_origin = knowledge_base_origin if knowledge_base_origin is not None else KnowledgeBasePathOrigin()
    has_knowledge_base = knowledge_base is not None
    knowledge_base_injectable = (
        wireup.instance(knowledge_base, as_type=KnowledgeBaseSettings) if knowledge_base is not None else _refuse_knowledge_base_settings
    )
    return wireup.create_async_container(
        injectables=[
            *sections_of(settings),
            knowledge_base_injectable,
            wireup.instance(resolved_origin, as_type=KnowledgeBasePathOrigin),
            wireup.instance(SettingsFile(path=default_settings_path()), as_type=SettingsFile),
            *(registration for module in WIRING_MODULES for registration in module.REGISTRATIONS),
            bus_provider if has_knowledge_base else registry_bus_provider,
            qa_router_for_knowledge_base if has_knowledge_base else qa_router_for_registry,
            resolver_provider,
        ]
    )


def run_container_factory(base_settings_path: Path) -> Callable[[Path], Container]:
    """Build the function that makes one container per refresh pass, over the knowledge base the job names.

    Each container reads the settings file again, so a changed setting counts from the next pass.

    Args:
        base_settings_path: The settings file every container is built from.

    Returns:
        A function from a knowledge base's folder to a container that acts on it.
    """

    def factory(knowledge_base_path: Path) -> Container:
        return create_container(
            fetch_application_settings(json_file=base_settings_path),
            knowledge_base=KnowledgeBaseSettings(path=knowledge_base_path),
        )

    return factory


async def dispose_engines(container: Container, *, close_connections: bool = True) -> None:
    """Dispose the admin and reader engines, each isolated so a half-built container still unwinds.

    Whoever owns a container's end of life calls this before the container's own close.

    Args:
        container: The container whose engines to release.
        close_connections: Pass `False` when disposing on a different event loop from the one the
            work ran on. A pooled connection's transport belongs to the loop that created it, so a
            graceful close from a fresh loop raises into the dead loop's callbacks. `False` releases
            the pools without touching the connections.
    """
    for engine in (await _maybe_engine(container, AsyncEngine), await _maybe_engine(container, ReaderEngine)):
        if engine is not None:
            try:
                await engine.dispose(close=close_connections)
            except Exception as exc:  # noqa: BLE001 (disposal failure must not fail shutdown, the leak is logged)
                _logger.debug("engine disposal failed on shutdown: {reason}", reason=str(exc))


async def dispose_container(container: Container, *, close_connections: bool = True) -> None:
    """Dispose the engines, then close the container's exit stack.

    Args:
        container: The container whose engines and exit stack to release.
        close_connections: Threaded to `dispose_engines`.
    """
    await dispose_engines(container, close_connections=close_connections)
    await container.close()


async def _maybe_engine[E: (AsyncEngine, ReaderEngine)](container: Container, klass: type[E]) -> AsyncEngine | None:
    """Resolve the admin engine or the read-only engine, or `None` when it cannot be resolved."""
    try:
        resolved = await container.get(klass)
    except Exception as exc:  # noqa: BLE001 (a never-resolved engine is nothing to dispose)
        _logger.debug("no {engine} engine to dispose on shutdown: {reason}", engine=klass.__name__, reason=str(exc))
        return None
    return resolved.engine if isinstance(resolved, ReaderEngine) else resolved
