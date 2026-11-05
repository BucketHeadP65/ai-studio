"""The live trial watch reverts an adoption after its first failed or worse run, and closes the live trial after three that count.

The library is a `LibraryCollection` over `FakeCollection`, holding `digest` v1 and v2, the consumer `digest-sources` at
v1, and v2 adopting `digest` v2 on live trial. The evidence log is a real file under the test's own directory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from core.ai.workflows.contracts import (
    Briefing,
    FileEvidenceLog,
    FromNode,
    FromParameter,
    LibraryCollection,
    LiveTrialMarker,
    Outcome,
    ParameterSpec,
    RunEvidence,
    RunNode,
    Shape,
    Workflow,
    adapt,
    spec_identity,
    step_content_address,
)

from harness.features.maintenance.contracts import LiveTrialWatch
from harness.session import SessionPool
from tests.support.fakes.library_collection import FakeCollection
from tests.support.fakes.owner_notifier import FakeOwnerNotifier
from tests.support.fakes.sessions import APool
from tests.support.runtime import a_runtime

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import OutcomeStatus

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
TEXT = Shape(json_schema={"type": "string"})
DIGEST = {
    1: Briefing(name="digest", instructions="Read the whole source and write one digest page.", inputs={"text": TEXT}, output=TEXT),
    2: Briefing(name="digest", instructions="Read the whole source and write one digest page that quotes each claim.", inputs={"text": TEXT}, output=TEXT),
}
CONSUMER = Workflow(
    name="digest-sources",
    version=1,
    description="Digest one source.",
    parameters={"source": ParameterSpec(description="the source", json_schema={"type": "string"})},
    inline=(DIGEST[1],),
    nodes=(
        RunNode(id="fetch", work="fetch", inputs={"source": FromParameter(parameter="source")}),
        RunNode(id="digest", work="digest", inputs={"text": FromNode(node="fetch")}),
    ),
    output=FromNode(node="digest"),
)
TRIAL_ID = "wf.digest-sources.v2"
PARENT_ID = "wf.digest-sources.v1"
FLAG_ID = "mf.reverted.digest-sources.digest"


class World:
    """The library, its pages, the evidence log, the owner's notifications and the watch over all of them."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = FakeCollection(prune=True)
        self.library = LibraryCollection(self.store, self.store.ingest, collection=self.store.collection)
        self.log = FileEvidenceLog(tmp_path / "evidence.jsonl")
        self.notifier = FakeOwnerNotifier()
        self.watch = LiveTrialWatch(APool(SessionPool(a_runtime(library=self.library))), self.log, self.notifier)

    async def adopt(self) -> Workflow:
        """Save both step versions, the consumer at v1, and v2 adopting `digest` v2 on live trial."""
        for version, step in DIGEST.items():
            await self.library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
        await self.library.put(CONSUMER, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
        child, lineage = adapt(CONSUMER, ops=[{"op": "replace_inline_step", "name": "digest", "with": DIGEST[2].model_dump(mode="json")}])
        marker = LiveTrialMarker(previous=PARENT_ID, step="digest", step_hash=step_content_address(DIGEST[2]))
        await self.library.put(
            child, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "agent:step-maintenance"}, lineage=lineage, live_trial=marker
        )
        return child

    async def baseline(self, *statuses: OutcomeStatus) -> None:
        """Record completed runs of the consumer's v1, the oldest first, each a day before the trial's runs."""
        for index, status in enumerate(statuses):
            await self.log.append(_ran(CONSUMER, PARENT_ID, status=status, at=T0 - timedelta(days=len(statuses) - index)))

    async def ended(self, run: RunEvidence) -> None:
        """Write the run's ending to the evidence log, then tell the watch, as the observed log does."""
        await self.log.append(run)
        await self.watch.run_ended(run)


def _ran(
    workflow: Workflow,
    workflow_id: str,
    *,
    outcome: str = "completed",
    status: OutcomeStatus = "succeeded",
    produced: Mapping[str, str] | None = None,
    at: datetime = T0,
) -> RunEvidence:
    return RunEvidence.model_validate({
        "workflow_id": workflow_id,
        "spec_hash": spec_identity(workflow),
        "workflow_version": workflow.version,
        "outcome": outcome,
        "result": Outcome(status=status, summary="what the run did"),
        "gates_auto": (),
        "gates_orchestrated": (),
        "produced": dict(produced if produced is not None else {"fetch": "str", "digest": "str"}),
        "runner": "app:schedule:7",
        "at": at,
    })


def _failed_at_the_step(trial: Workflow, *, at: datetime = T0, workflow_id: str = TRIAL_ID) -> RunEvidence:
    """A failed run of the trial version where `fetch` produced and `digest`, which runs the adopted step, did not."""
    return _ran(trial, workflow_id, outcome="failed", status="failed", produced={"fetch": "str"}, at=at)


async def test_the_first_failed_run_reverts_declines_flags_and_tells_the_owner(tmp_path: Path) -> None:
    world = World(tmp_path)
    trial = await world.adopt()
    await world.baseline("succeeded", "succeeded", "succeeded")
    run = _failed_at_the_step(trial)

    await world.ended(run)

    assert "archived_at" in world.store.pages[TRIAL_ID]["frontmatter"]
    assert not world.store.pages[PARENT_ID]["superseded"]
    assert await world.library.current(TRIAL_ID) == PARENT_ID
    (entry,) = await world.library.fetch_consumer_record(PARENT_ID)
    assert (entry.declined, entry.declined_because) == (step_content_address(DIGEST[2]), "reverted")
    flag = await world.library.fetch_flag(FLAG_ID)
    assert flag is not None
    assert (flag.kind, flag.step, flag.step_id, flag.step_hash) == ("live_trial_reverted", "digest", "st.digest.v2", step_content_address(DIGEST[2]))
    assert (flag.workflow, flag.raised_by, flag.raised_at, flag.status) == (TRIAL_ID, "agent:step-maintenance", run.at, "open")
    assert run.at.isoformat() in flag.evidence
    assert "digest, which runs the adopted step, produced nothing" in flag.evidence
    assert PARENT_ID in flag.evidence
    ((title, message),) = world.notifier.sent
    assert "digest" in title
    assert TRIAL_ID in message
    assert PARENT_ID in message


async def test_a_worse_run_reverts_too(tmp_path: Path) -> None:
    world = World(tmp_path)
    trial = await world.adopt()
    await world.baseline("succeeded", "succeeded", "succeeded")

    await world.ended(_ran(trial, TRIAL_ID, status="partial"))

    assert await world.library.current(TRIAL_ID) == PARENT_ID
    flag = await world.library.fetch_flag(FLAG_ID)
    assert flag is not None
    assert "partial" in flag.evidence


async def test_three_runs_that_count_write_nothing_and_the_next_run_is_not_read_as_a_live_trial(tmp_path: Path) -> None:
    world = World(tmp_path)
    trial = await world.adopt()
    await world.baseline("succeeded", "succeeded", "succeeded")
    written = len(world.store.payloads)

    for hour in range(3):
        await world.ended(_ran(trial, TRIAL_ID, at=T0 + timedelta(hours=hour)))
    assert len(world.store.payloads) == written

    await world.ended(_failed_at_the_step(trial, at=T0 + timedelta(hours=3)))

    assert len(world.store.payloads) == written, "the live trial is over, so a failed run reverts nothing"
    assert await world.library.current(PARENT_ID) == TRIAL_ID
    assert world.notifier.sent == []


async def test_a_run_that_says_nothing_about_the_step_does_not_bring_the_end_closer(tmp_path: Path) -> None:
    world = World(tmp_path)
    trial = await world.adopt()
    await world.baseline("succeeded", "succeeded", "succeeded")

    await world.ended(_ran(trial, TRIAL_ID, at=T0))
    await world.ended(_ran(trial, TRIAL_ID, at=T0 + timedelta(hours=1)))
    await world.ended(_ran(trial, TRIAL_ID, outcome="failed", status="failed", produced={}, at=T0 + timedelta(hours=2)))
    await world.ended(_failed_at_the_step(trial, at=T0 + timedelta(hours=3)))

    assert await world.library.current(TRIAL_ID) == PARENT_ID, "a run that failed before the step ran was ignored, not counted"


async def test_a_failed_run_of_a_version_a_person_saved_past_reverts_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    trial = await world.adopt()
    await world.baseline("succeeded", "succeeded", "succeeded")
    person, lineage = adapt(trial, ops=[{"op": "set_description", "to": "Digest one source, briefly."}])
    await world.library.put(person, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:studio"}, lineage=lineage)
    written = len(world.store.payloads)

    await world.ended(_failed_at_the_step(trial))

    assert len(world.store.payloads) == written
    assert await world.library.current(PARENT_ID) == "wf.digest-sources.v3"
    assert "archived_at" not in world.store.pages[TRIAL_ID]["frontmatter"]
    assert await world.library.fetch_flag(FLAG_ID) is None
    assert world.notifier.sent == []


async def test_a_run_of_a_version_with_no_live_trial_marker_does_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.adopt()
    other = CONSUMER.model_copy(update={"name": "digest-notes"})
    await world.library.put(other, title="Digest notes", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    written = len(world.store.payloads)

    await world.ended(_ran(other, "wf.digest-notes.v1", outcome="failed", status="failed", produced={"fetch": "str"}))
    await world.ended(_ran(other, "draft", outcome="failed", status="failed", produced={}))

    assert len(world.store.payloads) == written
    assert world.notifier.sent == []


async def test_a_run_of_the_previous_version_launched_by_name_is_not_read_as_the_live_trial(tmp_path: Path) -> None:
    world = World(tmp_path)
    await world.adopt()
    await world.baseline("succeeded")
    written = len(world.store.payloads)

    await world.ended(_ran(CONSUMER, "digest-sources", outcome="failed", status="failed", produced={"fetch": "str"}))

    assert len(world.store.payloads) == written, "the name reads the version on live trial, and the run's content address is another's"
    assert world.notifier.sent == []


async def test_a_run_launched_by_the_workflows_name_is_read_as_the_version_that_ran(tmp_path: Path) -> None:
    world = World(tmp_path)
    trial = await world.adopt()
    await world.baseline("succeeded")

    await world.ended(_failed_at_the_step(trial, workflow_id="digest-sources"))

    assert await world.library.current(TRIAL_ID) == PARENT_ID
