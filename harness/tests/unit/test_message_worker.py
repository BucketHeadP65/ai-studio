"""`message_worker`: a side conversation into a running worker's own memory.

Exercises the tool directly (`RunContext(deps=session, model=TestModel(), usage=RunUsage())`),
the same seam `test_run_stop.py` and `test_evidence.py` use, so the tool runs exactly as the
agent would call it. The worker step itself is run through `prepare(...).registry.get(name).function`,
the same seam `test_worker_memory.py` uses, so its line in `active.conversations` is the real
thing a graph run would have left behind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest

from core.ai.workflows.contracts import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    RetryFeedback,
    ReviewedArtifacts,
    RunNode,
    Shape,
    StepIdentity,
    Workflow,
    performing_step,
    prepare,
)
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.composition import build_environment
from harness.features.running.domain.progress import PendingReview
from harness.features.running.infrastructure.run_log import RunLog
from harness.features.running.tools import message_worker
from harness.session import ActiveRun, NoSessionLog, Runtime, SessionPool, bind_active, run_log_path
from harness.settings import WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}


def _spec(*, node_id: str = "weigh_node", work: str = "weigh") -> Workflow:
    """One worker step, wired behind a node id that is deliberately not the step's own name."""
    return Workflow(
        name="weigh-the-quarter",
        version=1,
        description="One worker step, reached through message_worker rather than a graph rerun.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(
            AgentStep(
                name=work,
                agent={"instructions": "Read the ledger and say whether the quarter looks healthy."},
                inputs={"ledger": Shape(json_schema=LEDGER)},
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(RunNode(id=node_id, work=work, inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node=node_id),
    )


def _ctx(session: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


async def _session_with_a_live_line(model: FunctionModel, tmp_path: Path, *, node_id: str = "weigh_node", work: str = "weigh", pending: bool = True) -> Runtime:
    """A session whose run log and worker memory both agree a worker is live at `node_id`.

    `pending` stands in for the run being waiting at a gate -- `message_worker`'s own precondition
    -- true by default so every test but the one about that precondition can ignore it.
    """
    active = ActiveRun()
    registry = await build_environment(model, engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    spec = _spec(node_id=node_id, work=work)
    ready = prepare(spec, registry)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get(work)
    assert implementation is not None
    with performing_step(StepIdentity(node_id=node_id)):
        await implementation.function(ledger={"net": 400})  # populates active.conversations.conversations[(node_id, "")]

    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", object()), active=active, session_log=NoSessionLog(), state=a_state(tmp_path))
    run_log = RunLog.begin(path=run_log_path(session), run_id="a-run", workflow_id="wf.weigh", spec=spec, params={"ledger": "400"}, started_at="2026-08-22T12:00:00+00:00")
    run_log.started(node_id)  # snapshots the spec to disk, which is what message_worker reads back
    if pending:
        session.active.pending = PendingReview(gate_id="approve", workflow="wf.weigh", reviewing="net: 400", rejection_reruns=node_id)
    return session


def _map_spec(*, node_id: str = "digest_each", work: str = "digest") -> Workflow:
    """One map node, one turn -- a map runs one worker per item, never a single conversation to message."""
    return Workflow(
        name="digest-each-item",
        version=1,
        description="One map node, reached through message_worker, which must refuse it up front.",
        parameters={"items": ParameterSpec(description="items to digest", json_schema={"type": "array"})},
        inline=(
            Briefing(
                name=work,
                instructions="Digest the item into one short line, ready for the next turn to read.",
                inputs={"item": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "string"}),
            ),
        ),
        nodes=(MapNode(id=node_id, turns=(work,), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node=node_id),
    )


async def _session_with_a_live_map(model: FunctionModel, tmp_path: Path, *, node_id: str = "digest_each", work: str = "digest") -> Runtime:
    """A session whose run log names a live map node, waiting at a gate -- no conversation line, on purpose."""
    active = ActiveRun()
    registry = await build_environment(model, engram=fake_engram(), active=active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    spec = _map_spec(node_id=node_id, work=work)
    session = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", object()), active=active, session_log=NoSessionLog(), state=a_state(tmp_path))
    run_log = RunLog.begin(path=run_log_path(session), run_id="a-run", workflow_id="wf.digest", spec=spec, params={"items": "[]"}, started_at="2026-08-22T12:00:00+00:00")
    run_log.started(node_id)  # snapshots the spec to disk, which is what message_worker reads back
    session.active.pending = PendingReview(gate_id="approve", workflow="wf.digest", reviewing="digesting", rejection_reruns=node_id)
    return session


async def _pooled_session_with_a_live_line(model: FunctionModel, tmp_path: Path, *, node_id: str = "weigh_node", work: str = "weigh") -> tuple[Runtime, Runtime]:
    """A default session and a pooled 'runner' session sharing its registry, live at `node_id`.

    Mirrors what `SessionPool._runtime_for` actually builds for a Run-button-launched session:
    the pooled runtime holds the default's own registry -- the identical `composition.build_environment`
    closures -- but its own fresh `ActiveRun`.
    """
    default_active = ActiveRun()
    registry = await build_environment(model, engram=fake_engram(), active=default_active, external_agents=dict, session_log=NoSessionLog(), custom=tmp_path / "custom", records=records_under(tmp_path), web_search=WebSearchSettings(), tiers=tiers_under(tmp_path))
    default = Runtime(registry=registry, library=cast("Any", None), agent=cast("Any", object()), active=default_active, session_log=NoSessionLog(), state=a_state(tmp_path))
    pool = SessionPool(default)
    pooled = await pool.create(None, session_kind="runner")

    spec = _spec(node_id=node_id, work=work)
    ready = prepare(spec, registry)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get(work)
    assert implementation is not None
    with bind_active(pooled.active), performing_step(StepIdentity(node_id=node_id)):
        await implementation.function(ledger={"net": 400})  # populates pooled.active.conversations.conversations[(node_id, "")]

    run_log = RunLog.begin(path=run_log_path(pooled), run_id="a-run", workflow_id="wf.weigh", spec=spec, params={"ledger": "400"}, started_at="2026-08-22T12:00:00+00:00")
    run_log.started(node_id)  # snapshots the spec to disk, which is what message_worker reads back
    pooled.active.pending = PendingReview(gate_id="approve", workflow="wf.weigh", reviewing="net: 400", rejection_reruns=node_id)
    return default, pooled


def _counting_model() -> tuple[FunctionModel, list[int]]:
    """A model that answers immediately and records how many messages it was handed each turn."""
    seen: list[int] = []

    def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        seen.append(len(messages))
        return ModelResponse(parts=[TextPart(content=f"answer {len(messages)}")])

    return streaming_function_model(respond), seen


def _echoing_model() -> tuple[FunctionModel, list[str]]:
    """A model that records the user prompt it was asked, verbatim, and answers something fixed."""
    prompts: list[str] = []

    def respond(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        part = messages[-1].parts[-1]
        prompts.append(str(part.content) if isinstance(part, UserPromptPart) else "")
        return ModelResponse(parts=[TextPart(content="ok")])

    return streaming_function_model(respond), prompts


async def test_message_worker_refuses_an_unknown_node(tmp_path: Path) -> None:
    model, _seen = _counting_model()
    session = await _session_with_a_live_line(model, tmp_path)

    with pytest.raises(ModelRetry, match="no worker named 'ghost_node' holds a conversation in this session's live run"):
        await message_worker(_ctx(session), "ghost_node", "are you there")


async def test_message_worker_refuses_a_foreign_session(tmp_path: Path) -> None:
    """The run's spec is on disk and readable, but this OTHER session's own memory holds no line."""
    model, _seen = _counting_model()
    live = await _session_with_a_live_line(model, tmp_path)
    # `foreign` reads the same run log file (both are unscoped runtimes), so the node resolves to
    # "weigh" just fine -- what is missing is a line for it in `foreign`'s own worker memory.
    foreign = Runtime(registry=live.registry, library=cast("Any", None), agent=cast("Any", object()), session_log=NoSessionLog(), state=a_state(tmp_path))
    foreign.active.pending = PendingReview(gate_id="approve", workflow="wf.weigh", reviewing="net: 400", rejection_reruns="weigh_node")

    with pytest.raises(ModelRetry, match="no worker named 'weigh' holds a conversation in this session's live run"):
        await message_worker(_ctx(foreign), "weigh_node", "are you there")


async def test_message_worker_refuses_a_map_node(tmp_path: Path) -> None:
    """A map runs one worker per item, addressable by no single conversation `message_worker` can name.

    `_live_step_name` would answer its last turn's name, but no conversation line ever sits under
    a bare `(node_id, "")` key for a map, so the old lookup always missed. The fix refuses up
    front, naming the node and why, rather than reporting a false miss.
    """
    model, _seen = _counting_model()
    session = await _session_with_a_live_map(model, tmp_path)

    with pytest.raises(ModelRetry, match="digest_each"):
        await message_worker(_ctx(session), "digest_each", "are you there")


async def test_message_worker_refuses_while_the_run_is_actively_executing(tmp_path: Path) -> None:
    """No pending gate means the graph could be writing to the same `StepConversation` right now."""
    model, _seen = _counting_model()
    session = await _session_with_a_live_line(model, tmp_path, pending=False)

    with pytest.raises(ModelRetry, match="the run is executing right now, message a worker while it waits at a gate"):
        await message_worker(_ctx(session), "weigh_node", "are you there")


async def test_a_conversation_grows_through_message_worker(tmp_path: Path) -> None:
    model, seen = _counting_model()
    session = await _session_with_a_live_line(model, tmp_path)

    await message_worker(_ctx(session), "weigh_node", "double check the total")
    await message_worker(_ctx(session), "weigh_node", "and the costs too")

    assert seen[1] > seen[0], "the second call carried the first call's history forward"
    assert session.active.conversations.conversations[("weigh_node", "")].messages, "the worker's own line kept the exchange"


async def test_message_worker_sends_the_message_text_to_the_worker(tmp_path: Path) -> None:
    model, prompts = _echoing_model()
    session = await _session_with_a_live_line(model, tmp_path)  # already made one call, priming the line

    reply = await message_worker(_ctx(session), "weigh_node", "double check the total")

    assert "double check the total" in prompts[-1]
    assert reply == "ok"


async def test_a_side_message_asks_for_no_repair_and_the_graphs_own_rerun_still_does(tmp_path: Path) -> None:
    """A repair belongs to the graph's own call of the step, never to an ad hoc side message.

    The side message runs against the same line, so the two are told apart by the identity each
    call is performed under: only the graph's own rerun carries the rejection it owes.
    """
    model, prompts = _echoing_model()
    session = await _session_with_a_live_line(model, tmp_path)  # already made one call, priming the line
    refused = ReviewedArtifacts(run_id="a-run")
    owed = RetryFeedback(
        gate_id="approve", source="person", target_node="weigh_node", attempt=1,
        feedback="tighten the second paragraph", reviewed=refused.persist("weigh_node", None, "the draft"),
        requested_repair="tighten the second paragraph",
    )

    await message_worker(_ctx(session), "weigh_node", "quick side question")

    assert "tighten the second paragraph" not in prompts[-1], "a side question is not the repair"

    # The graph's own rerun of the step, same shape `run_agent_step` calls it with.
    run_agent = session.registry.run_agent
    assert run_agent is not None
    line = session.active.conversations.conversations[("weigh_node", "")]
    assert isinstance(line.step, AgentStep)
    with performing_step(StepIdentity(node_id="weigh_node", retry_feedback=(owed,), reviewed_artifacts=refused)):
        await run_agent(line.step, line.output_type, ledger={"net": 400})

    assert "tighten the second paragraph" in prompts[-1], "the rerun reads the rejection it owes a repair to"
    assert "the draft" in prompts[-1], "over the value that was actually refused"


async def test_message_worker_against_a_pooled_sessions_live_run_writes_into_that_sessions_own_line(tmp_path: Path) -> None:
    """The exchange lands on the pooled session's own conversations, never on the default's."""
    model, seen = _counting_model()
    default, pooled = await _pooled_session_with_a_live_line(model, tmp_path)

    await message_worker(_ctx(pooled), "weigh_node", "double check the total")

    assert seen[1] > seen[0], "the exchange carried the pooled session's own first call's history"
    assert pooled.active.conversations.conversations[("weigh_node", "")].messages
    assert default.active.conversations.conversations == {}, "the exchange never lands on the default session's conversations"
