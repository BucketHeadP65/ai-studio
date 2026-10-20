"""Unit tests for `MultiConnectorSync` (`src/engram/sources/service/sync.py`): the multi-provider fold.

Every handler runs with the same `SyncMirrors` command. The merged `SyncReport` sums each
counter, concatenates `errors`/`unavailable`/`planned` in handler-declaration order, and carries
the command's own `dry_run` flag through untouched. A handler that raises contributes one
`SyncError` naming its provider instead of aborting the run, so a later provider still runs. Each
handler's own events move onto the composite as it finishes, since the bus drains only the handler
it dispatched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import MessageBus

from engram.export.contracts import ExportOkf
from engram.sources.contracts import MultiConnectorSync, SyncError, SyncMirrors, SyncReport
from engram.sources.domain.sync import PlannedAction, SyncAction, SyncCompleted, SyncStarted

if TYPE_CHECKING:
    from core.cqrs.contracts import Event


class FakeSync:
    """A `SyncHandler` answering one scripted report, raising the events it was given as it runs."""

    def __init__(self, report: SyncReport, raised: tuple[Event, ...] = ()) -> None:
        self.report = report
        self.raised = raised
        self.commands: list[SyncMirrors] = []
        self.events: list[Event] = []

    async def handle(self, command: SyncMirrors) -> SyncReport:
        self.commands.append(command)
        self.events.extend(self.raised)
        return self.report


class FakeRaisingSync:
    """A `SyncHandler` that always raises, to prove one provider's failure does not abort the others."""

    def __init__(self, error: Exception, raised: tuple[Event, ...] = ()) -> None:
        self.error = error
        self.raised = raised
        self.events: list[Event] = []

    async def handle(self, command: SyncMirrors) -> SyncReport:
        del command
        self.events.extend(self.raised)
        raise self.error


async def test_reports_merge_across_providers() -> None:
    zotero = FakeSync(SyncReport(created=2, updated=1, errors=(SyncError(key="k1", message="boom"),)))
    intake = FakeSync(SyncReport(created=1, archived=1, unavailable=("u1",)))
    sync = MultiConnectorSync({"zotero": zotero, "intake": intake})

    report = await sync.handle(SyncMirrors(limit=5))

    assert (report.created, report.updated, report.archived, report.skipped) == (3, 1, 1, 0)
    assert report.errors == (SyncError(key="k1", message="boom"),)
    assert report.unavailable == ("u1",)
    assert zotero.commands == [SyncMirrors(limit=5)]
    assert intake.commands == [SyncMirrors(limit=5)]


async def test_the_items_another_mirror_holds_merge_across_providers() -> None:
    zotero = FakeSync(SyncReport(already_mirrored={"LIBRARY1": "arxiv-2401.00001"}))
    intake = FakeSync(SyncReport(already_mirrored={"arxiv-2401.00002": "LIBRARY2"}))

    report = await MultiConnectorSync({"zotero": zotero, "intake": intake}).handle(SyncMirrors())

    assert report.already_mirrored == {"LIBRARY1": "arxiv-2401.00001", "arxiv-2401.00002": "LIBRARY2"}


async def test_a_dry_run_concatenates_the_plans() -> None:
    a = FakeSync(SyncReport(dry_run=True, planned=(PlannedAction(key="a", title="A", action=SyncAction.CREATE),)))
    b = FakeSync(SyncReport(dry_run=True, planned=(PlannedAction(key="b", title="B", action=SyncAction.SKIP),)))
    report = await MultiConnectorSync({"a": a, "b": b}).handle(SyncMirrors(dry_run=True))
    assert report.dry_run is True
    assert [row.key for row in report.planned] == ["a", "b"]


async def test_one_providers_failure_does_not_abort_the_others() -> None:
    intake = FakeRaisingSync(RuntimeError("boom"))
    zotero = FakeSync(SyncReport(created=2, updated=1, archived=1))

    report = await MultiConnectorSync({"intake": intake, "zotero": zotero}).handle(SyncMirrors())

    assert (report.created, report.updated, report.archived) == (2, 1, 1)
    assert len(report.errors) == 1
    assert report.errors[0].key == "intake"
    assert "intake" in report.errors[0].message
    assert "boom" in report.errors[0].message


async def test_every_providers_events_move_onto_the_composite_and_leave_the_handler() -> None:
    """The bus drains the composite, so a provider's events reach the subscribers only by moving here."""
    zotero_done = SyncCompleted(report=SyncReport(created=2))
    intake_done = SyncCompleted(report=SyncReport(created=1))
    zotero = FakeSync(zotero_done.report, raised=(SyncStarted(), zotero_done))
    intake = FakeSync(intake_done.report, raised=(SyncStarted(), intake_done))
    sync = MultiConnectorSync({"zotero": zotero, "intake": intake})

    await sync.handle(SyncMirrors())

    assert sync.events == [SyncStarted(), zotero_done, SyncStarted(), intake_done]
    assert zotero.events == []
    assert intake.events == []


async def test_a_failing_providers_events_still_reach_the_composite() -> None:
    intake = FakeRaisingSync(RuntimeError("boom"), raised=(SyncStarted(),))

    sync = MultiConnectorSync({"intake": intake})
    await sync.handle(SyncMirrors())

    assert sync.events == [SyncStarted()]
    assert intake.events == []


class TestBusDispatch:
    """`MultiConnectorSync` as a `CommandHandler`: routable on the shared bus like every other handler."""

    async def test_dispatched_through_the_bus_it_merges_and_leaves_the_events_collector_empty(self) -> None:
        zotero = FakeSync(SyncReport(created=2))
        sync = MultiConnectorSync({"zotero": zotero})
        bus = MessageBus({SyncMirrors: sync}, {})

        report = await bus.handle(SyncMirrors(dry_run=True))

        assert isinstance(report, SyncReport)
        assert report.created == 2
        assert sync.events == []

    async def test_call_refuses_a_command_that_is_not_sync_mirrors(self) -> None:
        sync = MultiConnectorSync({})

        with pytest.raises(TypeError, match="ExportOkf"):
            await sync(ExportOkf(collection="research", out_dir="okf"))


async def test_the_merge_keeps_what_each_connector_created_by_name() -> None:
    first = FakeSync(SyncReport(created=2, updated=1))
    second = FakeSync(SyncReport(updated=3))
    sync = MultiConnectorSync({"zotero": first, "queue": second})

    report = await sync.handle(SyncMirrors())

    assert report.created == 2
    assert report.created_by_connector == {"zotero": 2}, "a connector that created nothing is absent"


async def test_a_connector_that_raises_is_absent_from_the_split() -> None:
    working = FakeSync(SyncReport(created=1))
    broken = FakeRaisingSync(RuntimeError("offline"))
    sync = MultiConnectorSync({"zotero": working, "broken": broken})

    report = await sync.handle(SyncMirrors())

    assert report.created_by_connector == {"zotero": 1}
