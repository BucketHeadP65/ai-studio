"""The owner hears once about each scheduled run that ended failed, and about nothing else, with what became of its changed steps."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.ai.workflows.contracts import Outcome, RunEvidence

from harness.domain.contracts import FailedFireSteps
from harness.features.maintenance.contracts import FailedFireNotice
from tests.support.fakes.owner_notifier import FakeOwnerNotifier
from tests.support.fakes.step_schedules import FakeStepSchedules


def _ended(*, runner: str, outcome: str = "failed", status: str = "failed", workflow_id: str = "wf.digest-sources.v4") -> RunEvidence:
    return RunEvidence.model_validate({
        "workflow_id": workflow_id,
        "spec_hash": "abc",
        "workflow_version": 4,
        "outcome": outcome,
        "result": Outcome.model_validate({"status": status, "summary": "The digest step stopped with an error."}),
        "gates_auto": (),
        "gates_orchestrated": (),
        "runner": runner,
        "at": datetime(2026, 11, 5, 9, 0, tzinfo=UTC),
    })


async def test_a_failed_scheduled_run_tells_the_owner_once() -> None:
    notifier = FakeOwnerNotifier()
    notice = FailedFireNotice("app:schedule:", notifier, FakeStepSchedules())

    await notice.run_ended(_ended(runner="app:schedule:7"))

    ((title, message),) = notifier.sent
    assert title == "A scheduled run failed"
    assert message.startswith("wf.digest-sources.v4, fired by schedule 7, ended failed.")
    assert "The digest step stopped with an error." in message


async def test_a_completed_run_whose_checked_status_is_failed_tells_the_owner_too() -> None:
    notifier = FakeOwnerNotifier()

    await FailedFireNotice("app:schedule:", notifier, FakeStepSchedules()).run_ended(_ended(runner="app:schedule:7", outcome="completed"))

    assert len(notifier.sent) == 1


async def test_a_completed_scheduled_run_and_a_failed_manual_run_tell_nobody() -> None:
    notifier = FakeOwnerNotifier()
    notice = FailedFireNotice("app:schedule:", notifier, FakeStepSchedules())

    await notice.run_ended(_ended(runner="app:schedule:7", outcome="completed", status="succeeded"))
    await notice.run_ended(_ended(runner="app:harness"))
    await notice.run_ended(_ended(runner="app:chat"))

    assert notifier.sent == []


def test_an_empty_prefix_is_refused() -> None:
    with pytest.raises(ValueError, match="prefix"):
        FailedFireNotice("", FakeOwnerNotifier(), FakeStepSchedules())


async def test_the_notice_says_the_changed_steps_went_back_on_the_schedule() -> None:
    notifier = FakeOwnerNotifier()
    schedules = FakeStepSchedules(answer=FailedFireSteps(put_back=("st.a.v2", "st.b.v2"), by_hand=(), why=None))
    ended = _ended(runner="app:schedule:7", workflow_id="wf.step-maintenance.v1")

    await FailedFireNotice("app:schedule:", notifier, schedules).run_ended(ended)

    assert schedules.asked == [ended]
    ((_, message),) = notifier.sent
    assert message == (
        "wf.step-maintenance.v1, fired by schedule 7, ended failed. The digest step stopped with an error.\n"
        "It stopped with a provider error, so st.a.v2, st.b.v2 went back on the schedule, which fires again after its quiet period."
    )


@pytest.mark.parametrize(
    ("why", "because"),
    [
        ("other_failure", "it did not stop with a provider error"),
        ("put_back_once", "a failed fire put them back once already"),
        ("schedule_paused", "the schedule is paused"),
    ],
)
async def test_the_notice_names_the_steps_that_need_a_run_by_hand_and_the_run_to_start(why: str, because: str) -> None:
    notifier = FakeOwnerNotifier()
    answer = FailedFireSteps.model_validate({"put_back": (), "by_hand": ("st.a.v2", "st.b.v2"), "why": why})

    await FailedFireNotice("app:schedule:", notifier, FakeStepSchedules(answer=answer)).run_ended(
        _ended(runner="app:schedule:7", workflow_id="wf.step-maintenance.v1")
    )

    ((_, message),) = notifier.sent
    assert message.endswith(
        f"\nNot put back, because {because}: st.a.v2, st.b.v2. "
        "Run wf.step-maintenance.v1 by hand with changed_step_ids st.a.v2,st.b.v2 to plan them."
    )


async def test_a_fire_that_put_some_steps_back_names_both_kinds() -> None:
    notifier = FakeOwnerNotifier()
    answer = FailedFireSteps(put_back=("st.c.v1",), by_hand=("st.a.v2",), why="put_back_once")

    await FailedFireNotice("app:schedule:", notifier, FakeStepSchedules(answer=answer)).run_ended(
        _ended(runner="app:schedule:7", workflow_id="wf.step-maintenance.v1")
    )

    ((_, message),) = notifier.sent
    assert message.splitlines()[1:] == [
        "It stopped with a provider error, so st.c.v1 went back on the schedule, which fires again after its quiet period.",
        "Not put back, because a failed fire put them back once already: st.a.v2. Run wf.step-maintenance.v1 by hand with changed_step_ids st.a.v2 to plan them.",
    ]


async def test_a_put_back_that_raises_still_tells_the_owner_and_names_the_steps_to_run_by_hand() -> None:
    notifier = FakeOwnerNotifier()
    schedules = FakeStepSchedules(failure=RuntimeError("the schedule store is down"))
    ended = _ended(runner="app:schedule:7", workflow_id="wf.step-maintenance.v1").model_copy(update={"params": {"changed_step_ids": "st.a.v2"}})

    await FailedFireNotice("app:schedule:", notifier, schedules).run_ended(ended)

    ((_, message),) = notifier.sent
    assert message.endswith(
        "\nPutting its changed steps back failed: the schedule store is down. "
        "Run wf.step-maintenance.v1 by hand with the changed_step_ids it was fired with: st.a.v2."
    )


async def test_a_fire_with_no_changed_steps_keeps_the_notice_as_it_was() -> None:
    notifier = FakeOwnerNotifier()

    await FailedFireNotice("app:schedule:", notifier, FakeStepSchedules(answer=None)).run_ended(_ended(runner="app:schedule:7"))

    ((_, message),) = notifier.sent
    assert message == "wf.digest-sources.v4, fired by schedule 7, ended failed. The digest step stopped with an error."


async def test_a_run_that_did_not_fail_or_was_started_by_hand_asks_no_schedule() -> None:
    schedules = FakeStepSchedules()
    notice = FailedFireNotice("app:schedule:", FakeOwnerNotifier(), schedules)

    await notice.run_ended(_ended(runner="app:schedule:7", outcome="completed", status="succeeded"))
    await notice.run_ended(_ended(runner="app:harness"))

    assert schedules.asked == []


async def test_a_fire_no_on_step_change_schedule_takes_back_names_its_steps_for_a_run_by_hand() -> None:
    notifier = FakeOwnerNotifier()
    ended = _ended(runner="app:schedule:7", workflow_id="wf.step-maintenance.v1").model_copy(
        update={"params": {"changed_step_ids": "st.a.v2,st.b.v2"}}
    )

    await FailedFireNotice("app:schedule:", notifier, FakeStepSchedules(answer=None)).run_ended(ended)

    ((_, message),) = notifier.sent
    assert message.splitlines()[1:] == [
        (
            "Not put back, because no on-step-change schedule takes them back: st.a.v2, st.b.v2. "
            "Run wf.step-maintenance.v1 by hand with changed_step_ids st.a.v2,st.b.v2 to plan them."
        )
    ]


async def test_a_raising_put_back_for_a_fire_with_no_changed_steps_keeps_the_notice_as_it_was() -> None:
    notifier = FakeOwnerNotifier()
    schedules = FakeStepSchedules(failure=RuntimeError("the schedule store is down"))

    await FailedFireNotice("app:schedule:", notifier, schedules).run_ended(_ended(runner="app:schedule:7"))

    ((_, message),) = notifier.sent
    assert message == "wf.digest-sources.v4, fired by schedule 7, ended failed. The digest step stopped with an error."
