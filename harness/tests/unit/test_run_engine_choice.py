"""One setting decides which engine runs a workflow, and the durable one is what you get by default.

The dispatch is the whole risk: a deployment that meant to run durably but silently ran
in-process would look identical from the browser until the process died, and the run it was
supposed to survive would be gone. These pin the choice itself, not the engines.
"""

from __future__ import annotations

import asyncio

from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import pytest

from core.ai.history.contracts import ContinuationAttempt, ContinuationUnsafeReplayError
from core.ai.workflows.contracts import DEFAULT_TASK_QUEUE, StepConversationContinuation, spec_identity

from harness.adapters.temporal_server import ServerOutcome
from harness.adapters.wiring import REGISTRATIONS, durable_turns
from harness.composition import serve_durable_runs
from harness.features.running import service
from harness.features.running.exceptions import AutomodeUnavailableError, RunEngineUnavailableError
from harness.features.running.infrastructure import durable
from harness.features.running.infrastructure.durable import DurableTurns
from harness.session import NoSessionLog, Runtime
from harness.settings import HarnessSettings, RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.step_store import FakeContinuationCheckpoints, FakeStepStore
from tests.support.state import a_state, records_under

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry, Workflow


@pytest.fixture(autouse=True)
def _the_engine_answers_unless_a_test_says_otherwise() -> Iterator[None]:
    """The reachability flag is process state the composition root installs. Reset it around each test."""
    service.install_engine_reachable(reachable=True)
    yield
    service.install_engine_reachable(reachable=True)


@pytest.fixture
def session(registry: StepRegistry, tmp_path: Path) -> Runtime:
    """A session with no store and no agent, as `test_attestation` builds one: dispatch never reaches them."""
    return Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))


def test_the_default_engine_is_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare boot runs workflows in this process, and temporal is the opt-in.

    Asked with nothing exported, so the answer is the committed defaults file's own.
    """
    monkeypatch.delenv("APP_RUN__RUN_ENGINE", raising=False)
    assert HarnessSettings().run.run_engine == "in_process"
    assert HarnessSettings().run.temporal_task_queue == DEFAULT_TASK_QUEUE


def test_the_engine_is_selected_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_RUN__RUN_ENGINE", "temporal")
    monkeypatch.setenv("APP_RUN__TEMPORAL_ADDRESS", "temporal.internal:7233")
    settings = HarnessSettings().run
    assert settings.run_engine == "temporal"
    assert settings.temporal_address == "temporal.internal:7233"


def test_an_unknown_engine_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently fall back to the engine the deployment did not choose."""
    monkeypatch.setenv("APP_RUN__RUN_ENGINE", "temporalio")
    with pytest.raises(ValueError, match="run_engine"):
        HarnessSettings()


async def test_start_runs_in_process_when_that_engine_is_selected(session: Runtime, quarterly_report: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    records = records_under(tmp_path)
    in_process = RunSettings(run_engine="in_process")
    launched: list[str] = []
    monkeypatch.setattr(service, "launch", lambda *_args, **kwargs: launched.append(str(kwargs["workflow_id"])))
    await service.start(
        session, records=records, topics=FakeBroadcaster(), run=in_process, spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"}
    )
    assert launched == ["wf.report"]


async def test_start_hands_the_run_to_temporal_when_selected(session: Runtime, quarterly_report: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The setting is the only thing that decides; nothing about the spec or the registry does."""
    records = records_under(tmp_path)
    on_temporal = RunSettings(run_engine="temporal")
    started: list[str] = []

    async def _fake_launch(_session: Runtime, **kwargs: object) -> None:
        started.append(str(kwargs["workflow_id"]))

    monkeypatch.setattr(durable, "launch", _fake_launch)
    monkeypatch.setattr(service, "launch", lambda *_args, **_kwargs: pytest.fail("the in-process engine must not run"))
    await service.start(
        session, records=records, topics=FakeBroadcaster(), run=on_temporal, spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"}
    )
    assert started == ["wf.report"]


async def test_the_gate_mode_reaches_the_durable_engine_as_it_was_asked(session: Runtime, quarterly_report: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The same launch on either engine: the run itself decides its gates, and the ledger says so."""
    records = records_under(tmp_path)
    on_temporal = RunSettings(run_engine="temporal")
    modes: list[object] = []

    async def _fake_launch(_session: Runtime, **kwargs: object) -> None:
        modes.append(kwargs["gate_mode"])

    monkeypatch.setattr(durable, "launch", _fake_launch)
    await service.start(
        session,
        records=records,
        topics=FakeBroadcaster(),
        run=on_temporal,
        spec=quarterly_report,
        workflow_id="wf.report",
        params={"quarter": "Q2"},
        gate_mode="auto",
    )
    await service.start(
        session, records=records, topics=FakeBroadcaster(), run=on_temporal, spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"}
    )
    assert modes == ["auto", "wait"]


async def test_a_durable_launch_leaves_the_session_in_the_mode_it_started_with(session: Runtime, quarterly_report: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both paths leave the same session fields behind, so `/api/run/state` reads the mode either way.

    The run's identity comes with them: the server's own execution id, which is what an activity
    of this run reads back out of its own context, and what its spend ledger is opened under.
    """
    records = records_under(tmp_path)
    on_temporal = RunSettings(run_engine="temporal")

    class _FakeHandle:
        """A started workflow's handle, as far as a launch reads one: its two run ids."""

        first_execution_run_id = "run-abc"
        result_run_id = "run-abc"

    class _FakeClient:
        async def start_workflow(self, *_args: object, **_kwargs: object) -> _FakeHandle:
            return _FakeHandle()

    async def _fake_client(_run: RunSettings) -> _FakeClient:
        return _FakeClient()

    async def _fake_follow(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(durable, "_client", _fake_client)
    monkeypatch.setattr(durable, "_follow", _fake_follow)
    session.active.paused_by_restart = True
    await service.start(
        session,
        records=records,
        topics=FakeBroadcaster(),
        run=on_temporal,
        spec=quarterly_report,
        workflow_id="wf.report",
        params={"quarter": "Q2"},
        gate_mode="auto",
    )

    assert session.active.gate_mode == "auto"
    assert not session.active.paused_by_restart
    assert session.active.run_id == "run-abc"
    spend = records.spend.read("run-abc", "wf.report")
    assert spend is not None
    assert spend.runner == "app:harness"

    task = session.active.task
    assert task is not None
    await task


async def test_an_orchestrated_gate_on_the_durable_engine_is_refused_in_words(session: Runtime, quarterly_report: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An orchestrated gate is this run's own agent deciding, which the workflow has no seam for yet."""
    records = records_under(tmp_path)
    on_temporal = RunSettings(run_engine="temporal")
    monkeypatch.setattr(durable, "launch", lambda *_args, **_kwargs: pytest.fail("orchestrate must be refused before Temporal"))

    with pytest.raises(AutomodeUnavailableError, match="orchestrated"):
        await service.start(
            session,
            records=records,
            topics=FakeBroadcaster(),
            run=on_temporal,
            spec=quarterly_report,
            workflow_id="wf.report",
            params={"quarter": "Q2"},
            gate_mode="orchestrate",
        )


async def test_a_durable_run_is_refused_when_nothing_serves_temporal(session: Runtime, quarterly_report: Workflow, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Falling back to in-process would answer with a run that dies with this process. Refusing says so."""
    records = records_under(tmp_path)
    on_temporal = RunSettings(run_engine="temporal")
    service.install_engine_reachable(reachable=False)
    monkeypatch.setattr(durable, "launch", lambda *_args, **_kwargs: pytest.fail("nothing may be started on an engine nobody serves"))
    monkeypatch.setattr(service, "launch", lambda *_args, **_kwargs: pytest.fail("the in-process engine is never a fallback"))

    with pytest.raises(RunEngineUnavailableError, match="Temporal"):
        await service.start(
        session, records=records, topics=FakeBroadcaster(), run=on_temporal, spec=quarterly_report, workflow_id="wf.report", params={"quarter": "Q2"}
    )


async def test_an_unreachable_engine_is_not_served(registry: StepRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker is skipped rather than left to fail: port 1 answers nothing, so reaching a client would show up here."""
    on_temporal = RunSettings(run_engine="temporal", temporal_address="127.0.0.1:1")

    async with AsyncExitStack() as stack, asyncio.timeout(5):
        served = await serve_durable_runs(
            stack, registry, ServerOutcome(address="127.0.0.1:1", started=False, reachable=False), on_temporal
        )
    assert served is False


A_LINE = UUID("44444444-4444-4444-8444-444444444444")


def _a_first_turn() -> StepConversationContinuation:
    """A line's first turn: nothing committed before it, so it has no predecessor to resume."""
    return StepConversationContinuation(
        attempt=ContinuationAttempt(conversation_id=A_LINE, attempt_id="a" * 64, expected_revision=0, run_id="run-one"),
        given=None,
    )


async def test_a_lines_first_turn_resumes_nothing_and_reads_no_checkpoint() -> None:
    """A line begins empty: there is no earlier step to address, so none is read."""
    checkpoints = FakeContinuationCheckpoints()
    turns = DurableTurns(checkpoints=checkpoints, record=FakeStepStore())

    resumed = await turns.begin(_a_first_turn())

    assert resumed.settled is None
    assert resumed.messages == []
    assert checkpoints.checkpoints == {}, "nothing was scripted, and nothing was asked for"


async def test_a_turn_whose_run_wrote_no_settled_step_is_refused_rather_than_committed() -> None:
    """A commit must point at a step the record actually holds, or the ref it hands on names nothing."""
    checkpoints = FakeContinuationCheckpoints()
    checkpoints.unwritten.add("run-one")
    turns = DurableTurns(checkpoints=checkpoints, record=FakeStepStore())
    resumed = await turns.begin(_a_first_turn())

    with pytest.raises(ContinuationUnsafeReplayError, match="run-one"):
        await turns.settle(resumed, {"text": "a short draft"})


def test_the_process_that_serves_durable_runs_also_holds_the_ledger_their_lines_live_in() -> None:
    """Choosing the durable engine is choosing to carry a multi-turn line in the record rather than in this process."""
    assert durable_turns in REGISTRATIONS


class _SilentHandle:
    """A workflow handle whose queries go unanswered, as a closed workflow's do."""

    async def query(self, *_args: object, **_kwargs: object) -> object:
        msg = "no worker answers"
        raise RuntimeError(msg)


async def test_a_durable_runs_evidence_carries_the_content_address_of_the_workflow_it_ran(
    session: Runtime, quarterly_report: Workflow, tmp_path: Path
) -> None:
    """A trust lookup keys on the content address, so a durable run's record must carry it like any other run's."""
    records = records_under(tmp_path)
    session.active.workflow_id = "wf.report"

    await durable._record_evidence(
        session,
        cast("Any", _SilentHandle()),
        records=records,
        ran=durable.DurableRunOutcome(spec=quarterly_report, params={"quarter": "Q2"}, outcome="completed"),
    )

    (written,) = await records.evidence.events_for("wf.report")
    assert written.spec_hash == spec_identity(quarterly_report)
