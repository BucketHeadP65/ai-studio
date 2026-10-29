"""The harness index forward: each index's changed pages are posted to the harness, and delivered again until it answers."""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import httpx
import pytest

from core.cqrs.contracts import ImmediateWake, MemoryJobs, MemoryOutbox, MessageBus, Worker
from engram.indexing.contracts import PagesLoaded

from studio.knowledge_base.indexing.exceptions import IndexForwardFailedError
from studio.knowledge_base.indexing.infrastructure.harness_forward import HarnessIndexForward, registered_knowledge_base_named

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

HARNESS = "http://harness.test"


def _loaded(page_ids: tuple[str, ...] = ("a.one", "b.two"), collection: str = "docs-collection") -> PagesLoaded:
    return PagesLoaded(collection=collection, pages=2, sections=4, claims=0, edges=1, pruned=0, page_ids=page_ids)


def _named(collection: str) -> str | None:
    return {"docs-collection": "docs"}.get(collection)


def _answering(status: int, seen: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"armed": []})

    return httpx.MockTransport(answer)


def _forward(transport: httpx.AsyncBaseTransport) -> HarnessIndexForward:
    return HarnessIndexForward(HARNESS, knowledge_base_named=_named, transport=transport)


def _knowledge_base(root: Path, collection: str) -> Path:
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    payload: dict[str, JsonValue] = {
        "slug": "notes",
        "page_types": [{"page_type": "concept", "id_prefix": "c-"}],
        "retrieval": {"collection": collection},
    }
    (root / "knowledge-base-schema.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


async def test_an_index_with_changed_pages_is_posted_under_its_registered_name() -> None:
    seen: list[httpx.Request] = []
    event = _loaded()

    await _forward(_answering(200, seen))(event)

    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", "http://harness.test/api/schedules/index-runs")
    assert json.loads(request.content) == {"event_id": str(event.event_id), "knowledge_base": "docs", "page_ids": ["a.one", "b.two"]}


async def test_an_index_with_no_changed_pages_is_not_posted() -> None:
    seen: list[httpx.Request] = []

    await _forward(_answering(200, seen))(_loaded(page_ids=()))

    assert seen == []


async def test_a_collection_no_registered_knowledge_base_declares_is_not_posted() -> None:
    seen: list[httpx.Request] = []

    await _forward(_answering(200, seen))(_loaded(collection="elsewhere"))

    assert seen == []


@pytest.mark.parametrize("status", [404, 500, 503])
async def test_a_harness_without_the_route_or_failing_raises_so_the_event_is_delivered_again(status: int) -> None:
    with pytest.raises(IndexForwardFailedError, match=str(status)):
        await _forward(_answering(status))(_loaded())


async def test_an_unreachable_harness_raises() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    with pytest.raises(IndexForwardFailedError, match="could not be reached"):
        await _forward(httpx.MockTransport(refuse))(_loaded())


async def test_a_body_the_harness_refused_counts_as_delivered() -> None:
    await _forward(_answering(422))(_loaded())


async def test_the_worker_delivers_again_until_the_harness_answers() -> None:
    answers = [httpx.Response(503), httpx.Response(200, json={"armed": []})]

    def answer(request: httpx.Request) -> httpx.Response:
        del request
        return answers.pop(0)

    outbox = MemoryOutbox()
    bus = MessageBus({}, {PagesLoaded: []}, outbox=outbox)
    bus.register_subscriber(PagesLoaded, _forward(httpx.MockTransport(answer)), name="harness_index_forward")
    worker = Worker(jobs=MemoryJobs(), outbox=outbox, bus=bus, wake=ImmediateWake(), name="test")
    await outbox.append([_loaded()])

    assert await worker.deliver_once() == 0, "a failed delivery leaves the event in the outbox"
    assert await worker.deliver_once() == 1
    assert answers == []


def test_registered_knowledge_base_named_reads_the_collection_each_schema_declares(tmp_path: Path) -> None:
    alpha = _knowledge_base(tmp_path / "alpha", "alpha-collection")
    beta = _knowledge_base(tmp_path / "beta", "beta-collection")
    settings_path = tmp_path / "application_settings.json"
    rows: dict[str, JsonValue] = {name: {"path": str(root), "description": ""} for name, root in {"alpha": alpha, "beta": beta}.items()}
    settings_path.write_text(json.dumps({"knowledge_bases": rows}), encoding="utf-8")

    assert registered_knowledge_base_named(settings_path, "alpha-collection") == "alpha"
    assert registered_knowledge_base_named(settings_path, "beta-collection") == "beta"
    assert registered_knowledge_base_named(settings_path, "gamma-collection") is None
