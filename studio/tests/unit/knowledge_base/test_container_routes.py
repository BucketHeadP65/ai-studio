r"""Unit test for the composition root's pure command-route builder (`src/studio/knowledge_base/container.py`).

`build_command_routes` is the side-effect-free heart of the eager `bus_provider`: it
maps each of the fifteen concrete command types to the handler the container resolved,
with no `bus.register` late mutation anywhere. Pinned here Docker-free over lightweight
stub handlers so the declarative `{CommandType: handler}` table is asserted exactly,
without building the real wireup graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.cqrs.contracts import MessageBus
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
from engram.export.contracts import ExportOkf
from engram.indexing.contracts import CitingPagesChanged, IndexKnowledgeBase, IngestPage, PagesLoaded
from engram.sources.contracts import ScanWatchList, SyncMirrors

from studio.knowledge_base.container import build_command_routes, build_event_subscriptions
from studio.knowledge_base.refresh.contracts import RefreshAbandoned, RefreshCompleted, RefreshStarted

if TYPE_CHECKING:
    from core.cqrs.contracts import Command, Event


class _StubHandler:
    """Minimal `CommandHandler`: exposes an `events` collector and is awaitable-callable."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> object:
        return command


class _StubSubscriber:
    """Minimal `EventSubscriber`: a bare awaitable callable over an event."""

    async def __call__(self, event: Event) -> None:
        del event


def test_maps_exactly_the_fifteen_command_types():
    index_handler = _StubHandler()
    ingest_handler = _StubHandler()
    sync_handler = _StubHandler()
    scan_handler = _StubHandler()
    export_handler = _StubHandler()
    generate_handler = _StubHandler()
    tune_handler = _StubHandler()
    recommend_handler = _StubHandler()
    verify_handler = _StubHandler()
    coverage_handler = _StubHandler()
    freshness_handler = _StubHandler()
    score_handler = _StubHandler()
    check_generator_handler = _StubHandler()
    freeze_handler = _StubHandler()
    rollback_handler = _StubHandler()

    routes = build_command_routes(
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
    )

    assert set(routes) == {
        IndexKnowledgeBase,
        IngestPage,
        SyncMirrors,
        ScanWatchList,
        ExportOkf,
        GenerateEvalDataset,
        TuneRetrieval,
        RecommendStructure,
        VerifyRecommendation,
        ComputeCoverage,
        CheckFreshness,
        ScoreDataset,
        CheckGenerator,
        FreezeDataset,
        RollbackPolicy,
    }


def test_each_command_routes_to_its_own_handler():
    index_handler = _StubHandler()
    ingest_handler = _StubHandler()
    sync_handler = _StubHandler()
    scan_handler = _StubHandler()
    export_handler = _StubHandler()
    generate_handler = _StubHandler()
    tune_handler = _StubHandler()
    recommend_handler = _StubHandler()
    verify_handler = _StubHandler()
    coverage_handler = _StubHandler()
    freshness_handler = _StubHandler()
    score_handler = _StubHandler()
    check_generator_handler = _StubHandler()
    freeze_handler = _StubHandler()
    rollback_handler = _StubHandler()

    routes = build_command_routes(
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
    )

    assert routes[IndexKnowledgeBase] is index_handler
    assert routes[IngestPage] is ingest_handler
    assert routes[SyncMirrors] is sync_handler
    assert routes[ScanWatchList] is scan_handler
    assert routes[ExportOkf] is export_handler
    assert routes[GenerateEvalDataset] is generate_handler
    assert routes[TuneRetrieval] is tune_handler
    assert routes[RecommendStructure] is recommend_handler
    assert routes[VerifyRecommendation] is verify_handler
    assert routes[ComputeCoverage] is coverage_handler
    assert routes[CheckFreshness] is freshness_handler
    assert routes[CheckGenerator] is check_generator_handler
    assert routes[FreezeDataset] is freeze_handler
    assert routes[RollbackPolicy] is rollback_handler


def test_event_subscriptions_carry_the_qa_subscriber_on_papers_changed():
    qa_subscriber = _StubSubscriber()
    harness_forward = _StubSubscriber()

    subscriptions = build_event_subscriptions(qa_subscriber=qa_subscriber, harness_forward=harness_forward)

    assert set(subscriptions) == {CitingPagesChanged, PagesLoaded, RefreshStarted, RefreshCompleted, RefreshAbandoned}
    assert subscriptions[CitingPagesChanged] == [("qa_generation", qa_subscriber)]
    assert subscriptions[RefreshCompleted] == [("harness_forward", harness_forward)]
    assert subscriptions[RefreshAbandoned] == [("harness_forward", harness_forward)]
    # declared with no subscriber, so the worker knows the type
    assert [subscriptions[declared] for declared in (PagesLoaded, RefreshStarted)] == [[], []]


def test_the_bus_registers_the_qa_subscriber_under_its_declared_name() -> None:
    qa_subscriber = _StubSubscriber()
    bus = MessageBus(
        build_command_routes(
            index_handler=_StubHandler(),
            ingest_handler=_StubHandler(),
            sync_handler=_StubHandler(),
            scan_handler=_StubHandler(),
            export_handler=_StubHandler(),
            generate_handler=_StubHandler(),
            tune_handler=_StubHandler(),
            recommend_handler=_StubHandler(),
            verify_handler=_StubHandler(),
            coverage_handler=_StubHandler(),
            freshness_handler=_StubHandler(),
            score_handler=_StubHandler(),
            check_generator_handler=_StubHandler(),
            freeze_handler=_StubHandler(),
            rollback_handler=_StubHandler(),
        ),
        {},
    )
    for event_type, subscriptions in build_event_subscriptions(qa_subscriber=qa_subscriber, harness_forward=_StubSubscriber()).items():
        for name, subscriber in subscriptions:
            bus.register_subscriber(event_type, subscriber, name=name)

    assert [s.name for s in bus.subscriptions_for(CitingPagesChanged)] == ["qa_generation"]
