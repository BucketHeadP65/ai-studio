"""Boot relaunches every pooled session's run that a process death left in a run log.

A killed process is played by copying its state home while a run is in flight. Every run log write
is atomic, so the copy is the disk a SIGKILL at that moment leaves behind.
"""

from __future__ import annotations

import asyncio
import json
import shutil

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    CURRENT_STEP,
    Briefing,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    PersonGate,
    RunNode,
    RunStopped,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowArguments,
)
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.composition import restore_waiting_run, restore_waiting_runs, run_log_folders
from harness.domain.events import SessionDeleted, SessionStarted
from harness.features.running import controller, service
from harness.features.running.api_models import RunRequest
from harness.features.running.contracts import SCHEDULE_RUNNER_PREFIX
from harness.features.running.domain.progress import GateVerdict
from harness.features.running.infrastructure import run_events
from harness.features.running.infrastructure.run_log import load_waiting
from harness.features.running.records import RunCollaborators
from harness.features.running.tools import run_workflow, stop_run
from harness.session import WAITING_RUN_SUFFIX, Runtime, SessionPool, run_log_path
from harness.settings import RunSettings, StateSettings, aifactory_state
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.projects import FakeProjectIndex
from tests.support.fakes.sessions import APool
from tests.support.running import a_running_controller
from tests.support.runtime import EmptyLibrary, a_runtime
from tests.support.state import a_session_log, a_state, a_transcript, records_under

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from harness.domain.contracts import GateMode

_WORD = ParameterSpec(description="a word", json_schema={"type": "string"})
BRANCH_ARGS: WorkflowArguments = {"left": "left", "right": "right"}
MAP_ARGS: WorkflowArguments = {"text": "a b c"}


class _Steps:
    """Every step the shapes use. Each call is logged, and the call named by `hold` waits until released."""

    def __init__(self, *, hold: str = "") -> None:
        self.calls: list[str] = []
        self.hold = hold
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def _call(self, name: str) -> None:
        self.calls.append(name)
        if name == self.hold:
            self.entered.set()
            await self.release.wait()

    def registry(self) -> StepRegistry:
        registry = StepRegistry()

        async def shout(word: str) -> str:
            await self._call(f"shout:{word}")
            return word.upper()

        async def excite(word: str) -> str:
            await self._call(f"excite:{word}")
            return f"{word}!"

        async def pair(left: str, right: str) -> list[str]:
            await self._call("pair")
            return [left, right]

        async def split(text: str) -> list[str]:
            await self._call("split")
            return text.split()

        async def join(words: list[str]) -> str:
            await self._call("join")
            return " ".join(sorted(words))

        for step in (shout, excite, pair, split, join):
            registry.register(step)
        return registry


def _branches() -> Workflow:
    """Two branches of two steps each, paired, then a person gate."""
    return Workflow(
        name="branches",
        version=1,
        description="Shout and excite two words on two branches, pair them, and ask a person.",
        parameters={"left": _WORD, "right": _WORD},
        nodes=(
            RunNode(id="l1", work="shout", inputs={"word": FromParameter(parameter="left")}),
            RunNode(id="l2", work="excite", inputs={"word": FromNode(node="l1")}),
            RunNode(id="r1", work="shout", inputs={"word": FromParameter(parameter="right")}),
            RunNode(id="r2", work="excite", inputs={"word": FromNode(node="r1")}),
            RunNode(id="both", work="pair", inputs={"left": FromNode(node="l2"), "right": FromNode(node="r2")}),
            PersonGate(id="approve", reviews=FromNode(node="both"), rejected_to="both"),
        ),
        output=FromNode(node="approve"),
    )


def _mapped() -> Workflow:
    """Split a sentence, shout every word, join them, then a person gate."""
    return Workflow(
        name="mapped",
        version=1,
        description="Split a sentence, shout every word, join them, and ask a person.",
        parameters={"text": ParameterSpec(description="a sentence", json_schema={"type": "string"})},
        nodes=(
            RunNode(id="words", work="split", inputs={"text": FromParameter(parameter="text")}),
            MapNode(id="each", turns=("shout",), over=FromNode(node="words"), each="word"),
            RunNode(id="joined", work="join", inputs={"words": FromNode(node="each")}),
            PersonGate(id="approve", reviews=FromNode(node="joined"), rejected_to="joined"),
        ),
        output=FromNode(node="approve"),
    )


def _pool(home: Path, registry: StepRegistry, *, sessions: dict[str, Path] | None = None) -> SessionPool:
    """A pool as one process holds it: its session log, transcripts and run logs all under `home`.

    `sessions` maps a session id to the project root its state lives in, as the project index does.
    """
    runtime = a_runtime(registry=registry, session_log=a_session_log(home), store=a_transcript(home), state=a_state(home))
    return SessionPool(runtime, projects=FakeProjectIndex({}, sessions) if sessions else None)


async def _until(predicate: Callable[[], bool], *, what: str) -> None:
    """Poll until a background run reaches a state."""
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(what)


def _logged(runtime: Runtime) -> dict[str, object]:
    """The node outputs the session's run log holds now, empty when there is none."""
    waiting = load_waiting(run_log_path(runtime))
    return waiting.values if waiting is not None else {}


def _owes_a_repair(runtime: Runtime) -> bool:
    """Whether the session's run log holds a repair its run still owes."""
    waiting = load_waiting(run_log_path(runtime))
    return waiting is not None and bool(waiting.retry_feedback)


def _gate_is_open(runtime: Runtime) -> bool:
    """Whether the session's run waits at a gate nobody has answered yet."""
    decision = runtime.active.decision
    return decision is not None and not decision.done()


async def _decide(runtime: Runtime, verdict: GateVerdict) -> None:
    """Answer the gate the session's run waits at now."""
    await _until(lambda: _gate_is_open(runtime), what="the run never reached a gate with a decision outstanding")
    decision = runtime.active.decision
    assert decision is not None
    decision.set_result(verdict)


async def _finish(runtime: Runtime) -> None:
    """Approve the waiting gate and wait for the run to end, so no task outlives the test."""
    await _decide(runtime, GateVerdict(approve=True))
    task = runtime.active.task
    assert task is not None
    await task


async def _killed(
    tmp_path: Path,
    spec: Workflow,
    params: WorkflowArguments,
    *,
    hold: str,
    logged: frozenset[str],
    in_project: bool = False,
    gate_mode: GateMode = "wait",
    gate_timeout: float | None = 900.0,
    runner: str = "app:harness",
    session_kind: str = "runner",
) -> tuple[Path, str]:
    """Run `spec` in a pooled session until `hold` is in flight and `logged` is in its run log, then copy the disk.

    `hold` is a step call such as `"excite:RIGHT"`, or `"gate"` to stop at the person gate. The first
    process's run is finished afterwards. The session is a runner session unless `session_kind` says otherwise. With `in_project` the session lives in the project `before / "project"`.
    Returns the copied state home and the session's id.
    """
    before = tmp_path / "before"
    steps = _Steps(hold=hold)
    project = before / "project"
    if in_project:
        project.mkdir(parents=True)
    runtime = await _pool(before, steps.registry()).create(project if in_project else None, session_kind=session_kind)
    service.launch(
        runtime,
        records=records_under(before),
        topics=FakeBroadcaster(),
        spec=spec,
        workflow_id=f"wf.{spec.name}",
        params=params,
        gate_mode=gate_mode,
        gate_timeout=gate_timeout,
        runner=runner,
    )
    if hold == "gate":
        await _until(lambda: _gate_is_open(runtime), what="the run never reached its gate")
    else:
        await asyncio.wait_for(steps.entered.wait(), timeout=5)
    await _until(lambda: logged <= _logged(runtime).keys(), what=f"the run log never held {sorted(logged)}")
    after = tmp_path / "after"
    shutil.copytree(before, after)
    steps.release.set()
    if gate_mode == "wait":
        await _finish(runtime)
    else:
        assert runtime.active.task is not None
        await runtime.active.task
    assert runtime.session_id is not None
    return after, runtime.session_id


async def _restored(after: Path, session_id: str) -> tuple[SessionPool, Runtime, _Steps]:
    """Boot a fresh pool over the copied disk and relaunch every pooled run log in it."""
    steps = _Steps()
    pool = _pool(after, steps.registry())
    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    return pool, await pool.get(session_id), steps


async def _at_gate(runtime: Runtime) -> dict[str, object]:
    """The node outputs the relaunched run holds once it waits at its gate again."""
    await _until(lambda: _gate_is_open(runtime), what="the relaunched run never reached its gate")
    return _logged(runtime)


async def test_a_run_killed_at_its_gate_waits_there_again_with_no_step_run_twice(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}))

    pool, runtime, steps = await _restored(after, session_id)
    values = await _at_gate(runtime)
    pool.evict_idle(older_than_minutes=0)

    assert steps.calls == [], "every step was in the run log"
    assert values["both"] == ["LEFT!", "RIGHT!"]
    assert runtime in pool.held_sessions(), "a relaunched run waiting at its gate keeps its session in the pool"
    await _finish(runtime)


async def test_a_run_killed_during_a_step_runs_only_that_step_again(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="pair", logged=frozenset({"l2", "r2"}))

    _pool_after, runtime, steps = await _restored(after, session_id)
    values = await _at_gate(runtime)

    assert steps.calls == ["pair"]
    assert values["both"] == ["LEFT!", "RIGHT!"]
    await _finish(runtime)


async def test_a_branch_killed_mid_flight_never_takes_the_other_branchs_result(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="excite:LEFT", logged=frozenset({"l1", "r1"}))

    _pool_after, runtime, steps = await _restored(after, session_id)
    values = await _at_gate(runtime)

    assert steps.calls == ["excite:LEFT", "pair"], "only the step in flight and what follows it ran again"
    assert values["both"] == ["LEFT!", "RIGHT!"], "each branch kept its own result"
    await _finish(runtime)


async def test_a_map_killed_mid_flight_runs_every_item_again(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _mapped(), MAP_ARGS, hold="shout:b", logged=frozenset({"words"}))

    _pool_after, runtime, steps = await _restored(after, session_id)
    values = await _at_gate(runtime)

    assert sorted(steps.calls) == ["join", "shout:a", "shout:b", "shout:c"], "an unfinished map runs all its items again"
    assert values["joined"] == "A B C"
    await _finish(runtime)


async def test_a_project_sessions_run_log_is_relaunched(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}), in_project=True)
    steps = _Steps()
    pool = _pool(after, steps.registry(), sessions={session_id: after / "project"})
    folders = run_log_folders(after / "checkout", a_state(after), [after / "project"])

    await restore_waiting_runs(pool, folders, records_under(after), FakeBroadcaster())
    runtime = await pool.get(session_id)
    values = await _at_gate(runtime)

    assert folders != (a_state(after).sessions,), "the run log is in the project, not the checkout"
    assert steps.calls == []
    assert values["both"] == ["LEFT!", "RIGHT!"]
    await _finish(runtime)


async def test_a_run_log_whose_session_was_deleted_is_dropped_and_nothing_runs(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}))
    a_session_log(after).record(SessionDeleted(session_id=session_id))
    steps = _Steps()

    await restore_waiting_runs(_pool(after, steps.registry()), (a_state(after).sessions,), records_under(after), FakeBroadcaster())

    assert not (a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}").exists()
    assert steps.calls == []


async def test_a_damaged_run_log_does_not_stop_the_next_one(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="pair", logged=frozenset({"l2", "r2"}))
    damaged_id = "11111111-1111-1111-1111-111111111111"
    a_session_log(after).record(SessionStarted(session_id=damaged_id, session_kind="chat"))
    damaged = a_state(after).sessions / f"{damaged_id}{WAITING_RUN_SUFFIX}"
    damaged.write_text("{not json", encoding="utf-8")

    _pool_after, runtime, steps = await _restored(after, session_id)
    await _at_gate(runtime)

    assert steps.calls == ["pair"]
    assert damaged.exists(), "a damaged run log is left where it is"
    await _finish(runtime)


async def test_a_missing_folder_relaunches_nothing(tmp_path: Path) -> None:
    steps = _Steps()

    await restore_waiting_runs(_pool(tmp_path, steps.registry()), (tmp_path / "missing",), records_under(tmp_path), FakeBroadcaster())

    assert not (tmp_path / "missing").exists()
    assert steps.calls == []


def test_run_log_folders_map_each_root_to_its_sessions_folder_without_creating_it(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    checkout_state = a_state(tmp_path / "checkout-state")

    folders = run_log_folders(checkout, checkout_state, [checkout, project, tmp_path / "gone"])

    assert folders == (checkout_state.sessions, StateSettings(home=aifactory_state(project)).sessions)
    assert not (project / ".aifactory").exists(), "finding the folders creates nothing"


class _Reruns:
    """The brought step. Each call records the repairs it owed and the refused values behind them."""

    def __init__(self, *, hold_calls: frozenset[int] = frozenset()) -> None:
        self.owed: list[tuple[str, ...]] = []
        self.reviewed: list[tuple[object, ...]] = []
        self.hold_calls = hold_calls
        self.held = 0
        self.release = asyncio.Event()

    async def perform(self, task: Briefing, output_type: type, /, **inputs: object) -> object:
        """Record this call's owed repairs, reading every refused value back by its own address."""
        del task, output_type, inputs
        identity = CURRENT_STEP.get()
        assert identity is not None
        reader = identity.reviewed_artifacts
        assert reader is not None
        self.owed.append(tuple(item.feedback for item in identity.retry_feedback))
        self.reviewed.append(tuple(reader.read(item.reviewed) for item in identity.retry_feedback))
        call = len(self.owed)
        if call in self.hold_calls:
            self.held += 1
            await self.release.wait()
        return f"draft {call}"


def _repairable() -> Workflow:
    """One brought step and a person gate that sends it back."""
    return Workflow(
        name="repairable-draft",
        version=1,
        description="Draft one line, have a person send it back, and draft it again.",
        inline=(
            Briefing(
                name="draft_task",
                instructions="Draft one line for a person to approve or send back.",
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(
            RunNode(id="draft", work="draft_task"),
            PersonGate(id="approve", reviews=FromNode(node="draft"), rejected_to="draft", max_attempts=3),
        ),
        output=FromNode(node="approve"),
    )


async def test_two_sessions_in_one_folder_each_keep_the_value_they_owe_a_repair_over(tmp_path: Path) -> None:
    before = tmp_path / "before"
    reruns = _Reruns(hold_calls=frozenset({2, 4}))
    pool = _pool(before, StepRegistry(perform=reruns.perform))
    first, second = await pool.create(None, session_kind="runner"), await pool.create(None, session_kind="runner")
    for held, (runtime, words) in enumerate(((first, "tighten the first"), (second, "tighten the second")), start=1):
        service.launch(
            runtime, records=records_under(before), topics=FakeBroadcaster(), spec=_repairable(), workflow_id="wf.repair", params={}
        )
        await _decide(runtime, GateVerdict(approve=False, feedback=words))
        await _until(lambda held=held: reruns.held == held, what="the rerun that owes the repair never started")
        await _until(lambda runtime=runtime: _owes_a_repair(runtime), what="the run log never held the repair")
    after = tmp_path / "after"
    shutil.copytree(before, after)
    reruns.release.set()
    await _finish(first)
    await _finish(second)

    restarted = _Reruns()
    pool_after = _pool(after, StepRegistry(perform=restarted.perform))
    reviewed_root = a_state(after).sessions / "reviewed"
    orphan = reviewed_root / "dead-run" / "value.json"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("{}", encoding="utf-8")
    await restore_waiting_runs(pool_after, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    refiled = sorted(folder for folder in reviewed_root.iterdir() if any(folder.glob("**/*.json")))
    assert not orphan.exists(), "the sweep removed the subtree no run log names"
    assert len(refiled) == 2, "each relaunched run's re-filed values are on disk the moment boot returns"
    relaunched = [await pool_after.get(runtime.session_id or "") for runtime in (first, second)]
    for runtime in relaunched:
        await _at_gate(runtime)
        await _finish(runtime)

    assert sorted(restarted.owed) == [("tighten the first",), ("tighten the second",)], (
        "each relaunched run reran the step at once, owing only the repair its death interrupted"
    )
    assert sorted(restarted.reviewed) == [("draft 1",), ("draft 3",)], "each relaunched run read back the value it was refused over"


async def test_the_last_started_session_relaunches_in_the_process_own_session(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}))
    steps = _Steps()
    pool = _pool(after, steps.registry())
    pool.default.session_id = session_id
    pooled_file = a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}"

    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    runtime = await pool.get(session_id)
    values = await _at_gate(runtime)

    assert runtime is pool.default
    assert steps.calls == []
    assert values["both"] == ["LEFT!", "RIGHT!"]
    assert not pooled_file.exists(), "the pooled file moved to the process's own run log"
    assert run_log_path(pool.default).exists()
    await _finish(runtime)


async def test_a_session_with_a_live_run_is_not_relaunched_over_it(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}))
    steps = _Steps()
    pool = _pool(after, steps.registry())
    runtime = await pool.get(session_id)
    service.launch(
        runtime, records=records_under(after), topics=FakeBroadcaster(), spec=_branches(), workflow_id="wf.branches", params=BRANCH_ARGS
    )
    await _until(lambda: _gate_is_open(runtime), what="the fresh run never reached its gate")
    task = runtime.active.task

    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())

    assert runtime.active.task is task, "a live run is never replaced by a relaunch"
    await _finish(runtime)


async def test_a_relaunched_run_keeps_its_gate_mode(tmp_path: Path) -> None:
    after, session_id = await _killed(
        tmp_path,
        _branches(),
        BRANCH_ARGS,
        hold="pair",
        logged=frozenset({"l2", "r2"}),
        gate_mode="auto",
        gate_timeout=123.0,
        runner="schedule:nightly",
    )
    steps = _Steps(hold="pair")
    pool = _pool(after, steps.registry())

    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    runtime = await pool.get(session_id)
    await asyncio.wait_for(steps.entered.wait(), timeout=5)
    waiting = load_waiting(run_log_path(runtime))
    steps.release.set()
    task = runtime.active.task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert waiting is not None
    assert (waiting.gate_mode, waiting.gate_timeout, waiting.runner) == ("auto", 123.0, "schedule:nightly"), "the relaunched run's own log"
    assert steps.calls == ["pair"]
    assert not _gate_is_open(runtime), "the person gate passed itself"
    assert not run_log_path(runtime).exists(), "the run finished with no decision"


class _OneSessionRaises(SessionPool):
    """A pool whose `get` fails with an `OSError` for one session id."""

    def __init__(self, inner: SessionPool, *, failing: str) -> None:
        self._inner = inner
        self._failing = failing

    async def get(self, session_id: str) -> Runtime:
        if session_id == self._failing:
            msg = "the disk failed"
            raise OSError(msg)
        return await self._inner.get(session_id)


async def test_one_log_that_raises_does_not_stop_the_others(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}))
    failing = "00000000-0000-0000-0000-000000000000"
    planted = a_state(after).sessions / f"{failing}{WAITING_RUN_SUFFIX}"
    planted.write_text("{}", encoding="utf-8")
    steps = _Steps()
    inner = _pool(after, steps.registry())

    await restore_waiting_runs(
        _OneSessionRaises(inner, failing=failing), (a_state(after).sessions,), records_under(after), FakeBroadcaster()
    )
    runtime = await inner.get(session_id)
    values = await _at_gate(runtime)

    assert values["both"] == ["LEFT!", "RIGHT!"]
    assert planted.exists(), "the log that raised is left in place"
    await _finish(runtime)


async def test_the_process_own_session_adopting_a_log_keeps_the_value_it_owes_a_repair_over(tmp_path: Path) -> None:
    before = tmp_path / "before"
    reruns = _Reruns(hold_calls=frozenset({2}))
    pool = _pool(before, StepRegistry(perform=reruns.perform))
    runtime = await pool.create(None, session_kind="runner")
    service.launch(runtime, records=records_under(before), topics=FakeBroadcaster(), spec=_repairable(), workflow_id="wf.repair", params={})
    await _decide(runtime, GateVerdict(approve=False, feedback="tighten it"))
    await _until(lambda: reruns.held == 1, what="the rerun that owes the repair never started")
    await _until(lambda: _owes_a_repair(runtime), what="the run log never held the repair")
    after = tmp_path / "after"
    shutil.copytree(before, after)
    reruns.release.set()
    await _finish(runtime)
    session_id = runtime.session_id or ""

    restarted = _Reruns()
    pool_after = _pool(after, StepRegistry(perform=restarted.perform))
    pool_after.default.session_id = session_id
    await restore_waiting_runs(pool_after, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    adopted = await pool_after.get(session_id)
    assert adopted is pool_after.default
    await _at_gate(adopted)
    await _finish(adopted)

    assert restarted.owed == [("tighten it",)], "the adopted run reran the step at once, owing only the repair its death interrupted"
    assert restarted.reviewed == [("draft 1",)], "the refused value was read back after the reviewed subtree moved"


async def test_a_chat_sessions_run_is_paused_not_relaunched(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}), session_kind="chat")

    pool, runtime, steps = await _restored(after, session_id)
    pool.evict_idle(older_than_minutes=0)

    assert steps.calls == []
    assert load_waiting(run_log_path(runtime)) is not None, "the run log stays on disk"
    assert service.run_phase(runtime) == "paused"
    assert runtime.active.workflow_id == "wf.branches"
    assert runtime in pool.held_sessions(), "a paused run keeps its session in the pool"


class _BranchesLibrary(EmptyLibrary):
    """A library that keeps the branches workflow under the id the runs are launched with."""

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        if workflow_id != "wf.branches":
            raise KeyError(workflow_id)
        return _branches()


async def test_a_paused_chat_run_resumes_when_the_person_asks(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}), session_kind="chat")
    _pool_after, runtime, steps = await _restored(after, session_id)
    runtime.library = _BranchesLibrary()

    answer = await controller.run_resume(runtime, None, records_under(after), FakeBroadcaster())
    values = await _at_gate(runtime)

    assert answer.status_code == 200
    assert values["both"] == ["LEFT!", "RIGHT!"]
    assert steps.calls == []
    assert not runtime.active.paused_by_restart
    await _finish(runtime)


async def test_a_scheduled_run_relaunches_whatever_its_session(tmp_path: Path) -> None:
    after, session_id = await _killed(
        tmp_path,
        _branches(),
        BRANCH_ARGS,
        hold="pair",
        logged=frozenset({"l2", "r2"}),
        runner=f"{SCHEDULE_RUNNER_PREFIX}nightly",
        session_kind="chat",
    )

    _pool_after, runtime, steps = await _restored(after, session_id)
    values = await _at_gate(runtime)

    assert steps.calls == ["pair"]
    assert values["both"] == ["LEFT!", "RIGHT!"]
    assert not runtime.active.paused_by_restart
    await _finish(runtime)


async def test_the_process_own_session_pauses_its_chat_run(tmp_path: Path) -> None:
    before = tmp_path / "before"
    steps = _Steps()
    first = _pool(before, steps.registry())
    service.launch(
        first.default,
        records=records_under(before),
        topics=FakeBroadcaster(),
        spec=_branches(),
        workflow_id="wf.branches",
        params=BRANCH_ARGS,
    )
    await _until(lambda: _gate_is_open(first.default), what="the run never reached its gate")
    await _until(lambda: "both" in _logged(first.default), what="the run log never held both")
    after = tmp_path / "after"
    shutil.copytree(before, after)
    await _finish(first.default)
    second_steps = _Steps()
    second = _pool(after, second_steps.registry())

    restore_waiting_run(second.default, records_under(after), FakeBroadcaster())

    assert second.default.session_kind == "chat"
    assert second_steps.calls == []
    assert service.run_phase(second.default) == "paused"
    assert load_waiting(run_log_path(second.default)) is not None


def _chat_launch_context(runtime: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=runtime, model=TestModel(), usage=RunUsage())


async def _killed_chat_run(tmp_path: Path) -> tuple[Path, str]:
    return await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}), session_kind="chat")


async def test_a_chat_launch_over_a_restart_paused_run_is_refused_until_it_is_stopped(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    pool, runtime, _steps = await _restored(after, session_id)
    runtime.conversation.last_workflow = _branches()
    collaborators = RunCollaborators(
        records=records_under(after), topics=FakeBroadcaster(), run_settings=RunSettings(), sessions=APool(pool)
    )
    logged = load_waiting(run_log_path(runtime))

    with pytest.raises(ModelRetry, match="paused"):
        await run_workflow(_chat_launch_context(runtime), "draft", BRANCH_ARGS, run=collaborators)
    assert load_waiting(run_log_path(runtime)) == logged, "the paused run's log is untouched"
    assert runtime.active.paused_by_restart

    assert await service.stop(runtime, records=records_under(after))
    await run_workflow(_chat_launch_context(runtime), "draft", BRANCH_ARGS, run=collaborators)
    assert not runtime.active.paused_by_restart
    await _until(lambda: _gate_is_open(runtime), what="the chat run never reached its gate")
    assert await service.stop(runtime, records=records_under(after))

    assert service.run_phase(runtime) is None
    pool.evict_idle(older_than_minutes=0)
    assert runtime not in pool.held_sessions()


async def test_a_run_route_launch_over_a_paused_run_answers_409(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    pool, runtime, _steps = await _restored(after, session_id)
    runtime.library = _BranchesLibrary()
    controller_over_pool = a_running_controller(pool, after)

    response = await controller_over_pool.session_start_run(session_id, RunRequest(workflow_id="wf.branches", arguments=BRANCH_ARGS))

    assert response.status_code == 409
    assert "paused" in json.loads(bytes(response.body))["message"]
    assert service.run_phase(runtime) == "paused"


async def test_a_person_paused_run_survives_eviction_and_refuses_a_launch(tmp_path: Path) -> None:
    before = tmp_path / "before"
    steps = _Steps(hold="pair")
    pool = _pool(before, steps.registry())
    runtime = await pool.create(None, session_kind="chat")
    service.launch(
        runtime, records=records_under(before), topics=FakeBroadcaster(), spec=_branches(), workflow_id="wf.branches", params=BRANCH_ARGS
    )
    await asyncio.wait_for(steps.entered.wait(), timeout=5)
    assert await service.pause(runtime)

    pool.evict_idle(older_than_minutes=0)

    assert service.run_phase(runtime) == "paused"
    assert runtime in pool.held_sessions(), "a person-paused session keeps its card and its gate mode"
    assert "paused" in (runtime.active.paused_run_refusal() or ""), "a launch is refused while the run waits"
    steps.release.set()


async def test_a_person_paused_run_can_be_stopped_and_the_session_launches_again(tmp_path: Path) -> None:
    before = tmp_path / "before"
    steps = _Steps(hold="pair")
    pool = _pool(before, steps.registry())
    runtime = await pool.create(None, session_kind="chat")
    records = records_under(before)
    service.launch(runtime, records=records, topics=FakeBroadcaster(), spec=_branches(), workflow_id="wf.branches", params=BRANCH_ARGS)
    await asyncio.wait_for(steps.entered.wait(), timeout=5)
    assert await service.pause(runtime)
    waiting = load_waiting(run_log_path(runtime))
    assert waiting is not None
    subtree = run_log_path(runtime).parent / "reviewed" / waiting.run_id

    answer = await controller.run_stop(runtime, RunSettings(), records)

    assert answer.status_code == 200
    assert not run_log_path(runtime).exists()
    assert not subtree.exists()
    assert [record.run_id for record in records.archive.recent(10)] == [waiting.run_id]
    assert [entry.event for entry in run_events.events_for(runtime.state, waiting.run_id)][-1:] == [
        RunStopped(run_id=waiting.run_id, workflow_id="wf.branches")
    ]
    assert service.run_phase(runtime) is None
    assert runtime.active.paused_run_refusal() is None, "the session accepts a launch again"


async def test_the_stop_run_tool_ends_a_paused_run_on_the_durable_setting(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    pool, runtime, _steps = await _restored(after, session_id)

    answer = await stop_run(
        _chat_launch_context(runtime), records=records_under(after), sessions=APool(pool), run=RunSettings(run_engine="temporal")
    )

    assert answer.startswith("Stopped")
    assert service.run_phase(runtime) is None


async def test_an_adopted_runner_run_still_relaunches_by_itself_at_the_next_restart(tmp_path: Path) -> None:
    after, session_id = await _killed(tmp_path, _branches(), BRANCH_ARGS, hold="gate", logged=frozenset({"both"}))
    pool = _pool(after, _Steps().registry())
    pool.default.session_id = session_id

    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    adopted = await pool.get(session_id)
    await _at_gate(adopted)
    relogged = load_waiting(run_log_path(adopted))

    assert relogged is not None
    assert relogged.session_kind == "runner", "the run stays a runner's run in the process's own session"
    await _finish(adopted)


async def test_the_pause_shows_the_logged_gate_mode_the_start_time_and_the_finished_steps(tmp_path: Path) -> None:
    after, session_id = await _killed(
        tmp_path,
        _branches(),
        BRANCH_ARGS,
        hold="pair",
        logged=frozenset({"l2", "r2"}),
        gate_mode="auto",
        gate_timeout=None,
        session_kind="chat",
    )
    logged = load_waiting(a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}")
    assert logged is not None
    assert logged.started_at, "the run log carries the launch's own start time"

    _pool_after, runtime, _steps = await _restored(after, session_id)

    assert runtime.active.started_at == logged.started_at
    assert (runtime.active.gate_mode, runtime.active.auto_approve) == ("auto", True)
    assert {"l1", "l2", "r1", "r2"} <= set(runtime.active.progress.done)


async def test_a_run_log_without_a_start_time_falls_back_to_its_run_id(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    path = a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("started_at", None)
    path.write_text(json.dumps(raw), encoding="utf-8")

    _pool_after, runtime, _steps = await _restored(after, session_id)

    stamp = str(raw["run_id"]).split("-")[0]
    assert runtime.active.started_at.startswith(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}")


async def test_a_restart_paused_run_can_be_stopped_on_the_durable_setting(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    _pool_after, runtime, _steps = await _restored(after, session_id)

    answer = await controller.run_stop(runtime, RunSettings(run_engine="temporal"), records_under(after))

    assert answer.status_code == 200
    assert service.run_phase(runtime) is None


async def test_resuming_a_restart_paused_run_keeps_its_logged_gate_mode_timeout_and_runner(tmp_path: Path) -> None:
    after, session_id = await _killed(
        tmp_path,
        _branches(),
        BRANCH_ARGS,
        hold="pair",
        logged=frozenset({"l2", "r2"}),
        gate_mode="auto",
        gate_timeout=None,
        runner="app:chat",
        session_kind="chat",
    )
    pool = _pool(after, _Steps().registry())
    runtime = await pool.get(session_id)
    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())
    runtime.library = _BranchesLibrary()
    assert service.run_phase(runtime) == "paused"

    answer = await controller.run_resume(runtime, None, records_under(after), FakeBroadcaster())
    waiting = load_waiting(run_log_path(runtime))
    task = runtime.active.task
    assert task is not None
    await asyncio.wait_for(task, timeout=5)

    assert answer.status_code == 200
    assert waiting is not None
    assert (waiting.gate_mode, waiting.gate_timeout, waiting.runner) == ("auto", None, "app:chat")


async def test_a_restart_paused_session_resumes_its_logged_workflow_not_the_draft(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    _pool_after, runtime, steps = await _restored(after, session_id)
    runtime.conversation.draft.spec_yaml = _mapped().model_dump_json()

    answer = await controller.run_resume(runtime, None, records_under(after), FakeBroadcaster())
    values = await _at_gate(runtime)

    assert answer.status_code == 200
    assert values["both"] == ["LEFT!", "RIGHT!"]
    assert steps.calls == []
    await _finish(runtime)


async def test_stopping_a_restart_paused_run_ends_it_for_good(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    pool, runtime, _steps = await _restored(after, session_id)
    records = records_under(after)
    waiting = load_waiting(run_log_path(runtime))
    assert waiting is not None
    logged_run_id = waiting.run_id

    answer = await controller.run_stop(runtime, RunSettings(), records)

    assert answer.status_code == 200
    assert not run_log_path(runtime).exists()
    filed = records.archive.recent(10)
    assert [record.run_id for record in filed] == [logged_run_id], "the archive holds the ended run under its own id"
    assert [entry.event for entry in run_events.events_for(runtime.state, logged_run_id)][-1:] == [
        RunStopped(run_id=logged_run_id, workflow_id="wf.branches")
    ]
    assert service.run_phase(runtime) is None
    assert not runtime.active.paused_by_restart
    later_pool, later, _later_steps = await _restored(after, session_id)
    assert service.run_phase(later) is None, "a later boot finds nothing to pause"
    del pool, later_pool


async def test_adoption_never_overwrites_a_restart_paused_standing_log(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    pool = _pool(after, _Steps().registry())
    standing = run_log_path(pool.default)
    standing.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}", standing)
    restore_waiting_run(pool.default, records_under(after), FakeBroadcaster())
    assert pool.default.active.paused_by_restart
    pooled_file = a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}"
    pool.default.session_id = session_id
    before = standing.read_text(encoding="utf-8")

    await restore_waiting_runs(pool, (a_state(after).sessions,), records_under(after), FakeBroadcaster())

    assert standing.read_text(encoding="utf-8") == before, "the standing log was not overwritten"
    assert pooled_file.exists(), "the pooled log was left where it was, not moved over the standing one"


async def test_a_restart_paused_run_reports_when_it_started(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    _pool_after, runtime, _steps = await _restored(after, session_id)
    waiting = load_waiting(run_log_path(runtime))
    assert waiting is not None

    stamp = waiting.run_id.split("-")[0]
    assert runtime.active.started_at.startswith(f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}")


async def test_a_person_paused_run_resumes_under_the_live_gate_mode_not_the_logged_one(tmp_path: Path) -> None:
    before = tmp_path / "before"
    steps = _Steps(hold="pair")
    runtime = await _pool(before, steps.registry()).create(None, session_kind="chat")
    records = records_under(before)
    service.launch(
        runtime,
        records=records,
        topics=FakeBroadcaster(),
        spec=_branches(),
        workflow_id="wf.branches",
        params=BRANCH_ARGS,
        gate_mode="auto",
    )
    await asyncio.wait_for(steps.entered.wait(), timeout=5)
    runtime.active.gate_mode = "wait"
    runtime.active.auto_approve = False
    assert await service.pause(runtime)
    steps.release.set()
    runtime.library = _BranchesLibrary()

    answer = await controller.run_resume(runtime, None, records, FakeBroadcaster())
    await _until(lambda: _gate_is_open(runtime), what="the resumed run passed its gate by itself")

    assert answer.status_code == 200
    await _finish(runtime)


async def test_a_restart_paused_run_whose_step_is_gone_is_refused_on_resume(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    _pool_after, runtime, _steps = await _restored(after, session_id)
    runtime.registry = StepRegistry()

    answer = await controller.run_resume(runtime, None, records_under(after), FakeBroadcaster())

    assert answer.status_code == 400
    assert service.run_phase(runtime) == "paused"


async def test_the_pause_marks_finished_only_the_steps_a_resume_would_replay(tmp_path: Path) -> None:
    after, session_id = await _killed_chat_run(tmp_path)
    path = a_state(after).sessions / f"{session_id}{WAITING_RUN_SUFFIX}"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["values"]["both"] = 12345
    path.write_text(json.dumps(raw), encoding="utf-8")

    _pool_after, runtime, _steps = await _restored(after, session_id)

    assert "l1" in runtime.active.progress.done
    assert "both" not in runtime.active.progress.done, "a value that no longer types is run again on resume"
