"""The engram units of work record events and deliveries, and expose every repository both index paths drive."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from core.cqrs.contracts import Event, UnitOfWork
from core.testing.fakes import FakeUnitOfWork

from engram.indexing.internal_contracts import KnowledgeBaseUnitOfWork
from engram.testing.fakes.uow import FakeKnowledgeBaseUnitOfWork

if TYPE_CHECKING:
    from engram.indexing.contracts import IndexUnitOfWork
    from engram.indexing.internal_contracts import IncrementalUnitOfWork


class _Happened(Event):
    pass


def test_both_fakes_satisfy_the_port_and_buffer_records() -> None:
    for uow in (FakeUnitOfWork(), FakeKnowledgeBaseUnitOfWork()):
        assert isinstance(uow, UnitOfWork)
        event = _Happened()
        uow.record(event)
        uow.record_delivery(uuid4(), "qa_generation")
        assert uow.coordination.events == [event]
        assert len(uow.coordination.deliveries) == 1


async def test_both_fakes_drop_recorded_events_on_rollback_and_keep_them_on_commit() -> None:
    for uow in (FakeUnitOfWork(), FakeKnowledgeBaseUnitOfWork()):
        async with uow:
            uow.record(_Happened())
        assert uow.coordination.events == []

    for uow in (FakeUnitOfWork(), FakeKnowledgeBaseUnitOfWork()):
        async with uow:
            uow.record(_Happened())
            await uow.commit()
        assert len(uow.coordination.events) == 1


def test_the_knowledge_base_fake_carries_every_repository_both_index_paths_drive() -> None:
    """One UoW serves the full rebuild and the incremental load, disclosure summaries included."""
    fake = FakeKnowledgeBaseUnitOfWork()
    index_uow: IndexUnitOfWork = fake
    incremental_uow: IncrementalUnitOfWork = fake

    assert isinstance(fake, KnowledgeBaseUnitOfWork)
    assert index_uow.summaries is incremental_uow.summaries is fake.summaries
