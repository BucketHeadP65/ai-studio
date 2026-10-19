"""QA-generation routing across the knowledge bases one studio process serves.

One worker delivers whatever any registered knowledge base indexed, so the subscriber registered under
`qa_generation` cannot be bound to a single queue. These drive the router the way production does:
the event goes through the outbox, `Worker.deliver_once` finds it on `bus.subscriptions_for`, and
the ids land on the queue of the knowledge base whose collection the event names.
"""

from __future__ import annotations

import json
import logging

from pathlib import Path
from typing import TYPE_CHECKING

from core.cqrs.contracts import ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, Worker
from engram.evals.generation.contracts import PendingPagesQueue
from engram.indexing.contracts import CitingPagesChanged, PagesLoaded

from studio.knowledge_base.container import build_event_subscriptions
from studio.knowledge_base.qa_routing import KnowledgeBaseQaRouter, pending_pages_queues

if TYPE_CHECKING:
    from core.cqrs.contracts import Event
    from pydantic import JsonValue

_DATASETS = "evals/datasets"


def _knowledge_base(root: Path, collection: str) -> Path:
    """A knowledge base tree carrying its own spec, indexing into `collection`."""
    for sub in ("Units", "_meta"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    payload: dict[str, JsonValue] = {
        "slug": collection,
        "page_types": [{"page_type": "concept", "id_prefix": "c-"}],
        "retrieval": {"collection": collection},
    }
    (root / "knowledge-base-schema.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def _registry(settings_path: Path, knowledge_bases: dict[str, Path]) -> Path:
    """Write a settings JSON registering each knowledge base under its name, and answer its path."""
    rows: dict[str, JsonValue] = {name: {"path": str(path), "description": ""} for name, path in knowledge_bases.items()}
    settings_path.write_text(json.dumps({"knowledge_bases": rows}), encoding="utf-8")
    return settings_path


def _router(tmp_path: Path, settings_path: Path, active: Path) -> KnowledgeBaseQaRouter:
    """The router over per-collection queue files under `tmp_path`."""
    return KnowledgeBaseQaRouter(
        settings_path=settings_path, active_knowledge_base=active, queue_for=pending_pages_queues(tmp_path / _DATASETS), autogen="queue"
    )


async def _no_forward(event: Event) -> None:
    """A harness forward that does nothing, because these tests deliver `CitingPagesChanged` only."""
    del event


def _bus(router: KnowledgeBaseQaRouter, outbox: MemoryOutbox) -> MessageBus:
    """The bus the composition root builds, with the router as the `qa_generation` subscriber."""
    subscriptions = build_event_subscriptions(qa_subscriber=router, harness_forward=_no_forward)
    bus = MessageBus({}, {event_type: [] for event_type in subscriptions}, outbox=outbox)
    for event_type, declared in subscriptions.items():
        for name, subscriber in declared:
            bus.register_subscriber(event_type, subscriber, name=name)
    return bus


async def _deliver(bus: MessageBus, outbox: MemoryOutbox) -> None:
    """One delivery pass, the pass the knowledge base worker makes."""
    await Worker(jobs=MemoryJobs(), outbox=outbox, bus=bus, wake=ImmediateWake(), name="test").deliver_once()


def _pending(tmp_path: Path, collection: str) -> list[str]:
    """What the named collection's queue file currently holds."""
    return PendingPagesQueue(tmp_path / "evals" / f"pending_pages.{collection}.json").pending()


class TestKnowledgeBaseQaRouter:
    async def test_a_relative_datasets_dir_anchors_each_queue_to_its_own_knowledge_base_root(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        beta = _knowledge_base(tmp_path / "beta", "beta")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha, "beta": beta})
        router = KnowledgeBaseQaRouter(
            settings_path=settings_path, active_knowledge_base=alpha, queue_for=pending_pages_queues(Path(_DATASETS)), autogen="queue"
        )
        outbox = MemoryOutbox()
        bus = _bus(router, outbox)
        await outbox.append(
            [
                CitingPagesChanged(collection="alpha", page_ids=("paper.a",)),
                CitingPagesChanged(collection="beta", page_ids=("paper.b",)),
            ]
        )

        await _deliver(bus, outbox)

        assert PendingPagesQueue(alpha / "evals" / "pending_pages.alpha.json").pending() == ["paper.a"]
        assert PendingPagesQueue(beta / "evals" / "pending_pages.beta.json").pending() == ["paper.b"]

    async def test_an_event_lands_on_the_queue_of_the_knowledge_base_that_indexed_it(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        beta = _knowledge_base(tmp_path / "beta", "beta")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha, "beta": beta})
        outbox = MemoryOutbox()
        bus = _bus(_router(tmp_path, settings_path, alpha), outbox)
        await outbox.append([CitingPagesChanged(collection="beta", page_ids=("paper.b",))])

        await _deliver(bus, outbox)

        assert _pending(tmp_path, "beta") == ["paper.b"]
        assert _pending(tmp_path, "alpha") == []

    async def test_the_active_knowledge_base_is_routed_even_with_an_empty_registry(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        settings_path = _registry(tmp_path / "application_settings.json", {})
        outbox = MemoryOutbox()
        bus = _bus(_router(tmp_path, settings_path, alpha), outbox)
        await outbox.append([CitingPagesChanged(collection="alpha", page_ids=("paper.a",))])

        await _deliver(bus, outbox)

        assert _pending(tmp_path, "alpha") == ["paper.a"]

    async def test_a_collection_no_knowledge_base_here_indexes_is_dropped_with_a_log_line(self, tmp_path: Path, caplog):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha})
        outbox = MemoryOutbox()
        bus = _bus(_router(tmp_path, settings_path, alpha), outbox)
        event = CitingPagesChanged(collection="elsewhere", page_ids=("paper.x",))
        await outbox.append([event])

        with caplog.at_level(logging.INFO, logger="studio.knowledge_base.qa_routing"):
            await _deliver(bus, outbox)

        assert _pending(tmp_path, "elsewhere") == []
        assert "elsewhere" in caplog.text
        assert outbox.closed == {event.event_id}  # dropped, not retried forever

    async def test_a_registered_root_whose_schema_does_not_load_is_skipped_and_every_other_knowledge_base_still_routes(
        self, tmp_path: Path, caplog
    ):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        unconfigured = tmp_path / "unconfigured"
        unconfigured.mkdir(parents=True)
        (unconfigured / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")  # passes registry-row validation, never loads
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha, "unconfigured": unconfigured})
        outbox = MemoryOutbox()
        bus = _bus(_router(tmp_path, settings_path, alpha), outbox)
        event = CitingPagesChanged(collection="alpha", page_ids=("paper.a",))
        await outbox.append([event])

        with caplog.at_level(logging.WARNING, logger="studio.knowledge_base.qa_routing"):
            await _deliver(bus, outbox)

        assert _pending(tmp_path, "alpha") == ["paper.a"]  # the broken root does not stop routing for a healthy one
        assert str(unconfigured) in caplog.text

    async def test_autogen_off_never_enqueues(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha})
        router = KnowledgeBaseQaRouter(
            settings_path=settings_path,
            active_knowledge_base=alpha,
            queue_for=pending_pages_queues(tmp_path / _DATASETS),
            autogen="off",
        )
        outbox = MemoryOutbox()
        bus = _bus(router, outbox)
        await outbox.append([CitingPagesChanged(collection="alpha", page_ids=("paper.a",))])

        await _deliver(bus, outbox)

        assert _pending(tmp_path, "alpha") == []

    async def test_routes_by_the_registry_alone_when_there_is_no_active_knowledge_base(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha})
        router = KnowledgeBaseQaRouter(
            settings_path=settings_path, active_knowledge_base=None, queue_for=pending_pages_queues(tmp_path / _DATASETS), autogen="queue"
        )
        outbox = MemoryOutbox()
        bus = _bus(router, outbox)
        await outbox.append([CitingPagesChanged(collection="alpha", page_ids=("paper.a",))])

        await _deliver(bus, outbox)

        assert _pending(tmp_path, "alpha") == ["paper.a"]

    async def test_a_knowledge_base_registered_after_the_first_event_is_routed(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        beta = _knowledge_base(tmp_path / "beta", "beta")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha})
        outbox = MemoryOutbox()
        bus = _bus(_router(tmp_path, settings_path, alpha), outbox)
        await outbox.append([CitingPagesChanged(collection="beta", page_ids=("paper.b",))])
        await _deliver(bus, outbox)
        _registry(settings_path, {"alpha": alpha, "beta": beta})
        await outbox.append([CitingPagesChanged(collection="beta", page_ids=("paper.b2",))])

        await _deliver(bus, outbox)

        assert _pending(tmp_path, "beta") == ["paper.b2"]


class TestDeclaredEventTypes:
    def test_the_bus_knows_pages_loaded_even_though_nothing_subscribes_to_it(self, tmp_path: Path):
        alpha = _knowledge_base(tmp_path / "alpha", "alpha")
        settings_path = _registry(tmp_path / "application_settings.json", {"alpha": alpha})
        bus = _bus(_router(tmp_path, settings_path, alpha), MemoryOutbox())

        assert "PagesLoaded" in bus.event_types()
        assert bus.subscriptions_for(PagesLoaded) == ()
