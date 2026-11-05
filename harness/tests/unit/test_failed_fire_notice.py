"""The owner hears once about each scheduled run that ended failed, and about nothing else."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.ai.workflows.contracts import Outcome, RunEvidence

from harness.features.maintenance.contracts import FailedFireNotice
from tests.support.fakes.owner_notifier import FakeOwnerNotifier


def _ended(*, runner: str, outcome: str = "failed", status: str = "failed") -> RunEvidence:
    return RunEvidence.model_validate({
        "workflow_id": "wf.digest-sources.v4",
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
    notice = FailedFireNotice("app:schedule:", notifier)

    await notice.run_ended(_ended(runner="app:schedule:7"))

    ((title, message),) = notifier.sent
    assert title == "A scheduled run failed"
    assert message.startswith("wf.digest-sources.v4, fired by schedule 7, ended failed.")
    assert "The digest step stopped with an error." in message


async def test_a_completed_run_whose_checked_status_is_failed_tells_the_owner_too() -> None:
    notifier = FakeOwnerNotifier()

    await FailedFireNotice("app:schedule:", notifier).run_ended(_ended(runner="app:schedule:7", outcome="completed"))

    assert len(notifier.sent) == 1


async def test_a_completed_scheduled_run_and_a_failed_manual_run_tell_nobody() -> None:
    notifier = FakeOwnerNotifier()
    notice = FailedFireNotice("app:schedule:", notifier)

    await notice.run_ended(_ended(runner="app:schedule:7", outcome="completed", status="succeeded"))
    await notice.run_ended(_ended(runner="app:harness"))
    await notice.run_ended(_ended(runner="app:chat"))

    assert notifier.sent == []


def test_an_empty_prefix_is_refused() -> None:
    with pytest.raises(ValueError, match="prefix"):
        FailedFireNotice("", FakeOwnerNotifier())
