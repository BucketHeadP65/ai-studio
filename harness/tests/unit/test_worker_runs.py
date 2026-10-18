"""A worker actually run: the document an artifact brought, built by the host that pays for it.

The other two brought kinds have each had this test in their own currency. A briefing runs under
`run_defined_step` and a program under `code_runner`, and both prove the same thing -- that what an
artifact carries is a proposal, and the host decides what it costs and what it touches. A worker
makes the sharpest version of that claim, because what it carries is neither prose nor arithmetic
but a whole declared agent, with its own model, its own instructions and its own capabilities
written down inside it.

So this file runs one. `Agent.from_spec` merges the document with the host's keyword arguments, and
every one of those arguments is a ruling: the model is the host's, the output type comes from the
step's own `output` slot, the tools are the reads the step named and nothing wider, and the
capabilities are the guards this app gives every step. What the artifact wrote is read, and then
out-voted.

`test_the_hosts_model_runs_the_worker_whatever_the_document_named` is the argument in one test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    AgentStep,
    FromNode,
    FromParameter,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    prepare,
    validate,
)
from core.ai.workflows.domain.validator import IssueType
from pydantic import BaseModel, JsonValue
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.test import TestModel

from tests.support.environment import BOOT, CALLED, SEEN, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.models.function import AgentInfo

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}

VERDICT = {"type": "object", "title": "Verdict", "properties": {"healthy": {"type": "boolean"}}, "required": ["healthy"]}


@pytest.fixture
async def environment() -> StepRegistry:
    """An environment over the stand-in surface, whose model answers every request the same trivial way."""
    CALLED.clear()
    SEEN.clear()
    BOOT.clear()
    return await world_over(TestModel())


def _weighing(agent: Mapping[str, JsonValue], *, tools: tuple[str, ...] = ()) -> Workflow:
    """One worker step, fed by one parameter, and nothing else in the graph."""
    return Workflow(
        name="weigh-the-quarter",
        version=1,
        description="A worker reads a ledger and says whether the quarter looks healthy.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(
            AgentStep(
                name="weigh", agent=agent, inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=VERDICT), tools=tools
            ),
        ),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node="weigh"),
    )


def _dumped(produced: object) -> dict[str, object]:
    """A dynamically built output model, as plain data. Pyright cannot know its attributes."""
    assert isinstance(produced, BaseModel)
    return produced.model_dump()


async def _weighed(environment: StepRegistry, agent: Mapping[str, JsonValue], *, tools: tuple[str, ...] = ()) -> object:
    """Build this environment's step for one worker document and run it, exactly as the indexer would."""
    ready = prepare(_weighing(agent, tools=tools), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("weigh")
    assert implementation is not None
    return await implementation.function(ledger={"net": 400})


# --- the host can build one -----------------------------------------------------------------


async def test_this_world_can_run_a_worker_and_grants_it_no_capability(environment: StepRegistry) -> None:
    """Both halves of the seam, asserted where they are wired rather than where they are declared.

    The runner is real, so a spec bringing a worker is executable here. The capability grant is
    empty, so every capability such a document declares is still refused -- the refusal surface is
    live and the list behind it is a decision a host makes on purpose, one name at a time.
    """
    assert environment.runs_agents, "a spec bringing a worker can run here"
    assert environment.capability_grants == (), "and may hold no pydantic-ai capability of its own"


async def test_a_declared_capability_is_refused_cold_by_this_world(environment: StepRegistry) -> None:
    """`capability_grants=()` is not a formality: the shell is grantable to a briefing and not to a worker."""
    report = validate(_weighing({"instructions": BRIEFING, "capabilities": ["shell"]}), environment)
    assert not report.ok
    assert any("not granted" in issue.detail for issue in report.issues)


def test_a_run_with_no_agent_runner_refuses_at_prepare_and_builds_no_step() -> None:
    """The cold half, at the seam the indexer uses: a registry with no runner produces no worker.

    `validate` reports it; this asserts the consequence -- `prepare` returns a registry
    that does not contain the step at all, so nothing downstream can reach a worker this host
    never agreed to run.
    """
    ready = prepare(_weighing({"instructions": BRIEFING}), StepRegistry())
    assert not ready.report.ok
    assert ready.registry.get("weigh") is None, "a step the host cannot run is never built"
    assert any("cannot run a worker" in issue.detail for issue in ready.report.issues)
    assert any(issue.kind is IssueType.BAD_TASK_DEFINITION for issue in ready.report.issues)


# --- what the host holds on to ---------------------------------------------------------------


async def test_a_worker_runs_to_completion_and_its_answer_lands_typed(environment: StepRegistry) -> None:
    """End to end: a document with nothing but instructions, run, and validated against the step's own shape.

    "Done" means the answer validated as the class the `output` slot names -- the same bar a
    briefing and a program are held to, which is what makes a worker an ordinary step downstream.
    """
    produced = await _weighed(environment, {"instructions": BRIEFING})

    assert isinstance(produced, BaseModel)
    assert type(produced).__name__ == "Verdict"
    assert isinstance(_dumped(produced)["healthy"], bool)
    assert CALLED == [], "a worker that asked for nothing reaches nothing"


async def test_the_hosts_model_runs_the_worker_whatever_the_document_named() -> None:
    """The reach claim, at its sharpest: a document naming a provider names a preference, not a bill.

    `Agent.from_spec` takes the host's `model=` over the document's own, so the run pays for the
    model it configured. Asserted by the run *completing* -- the named model has no credentials
    here, so reaching it at all would be the failure rather than a different answer.
    """
    answered: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        answered.append("host")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"healthy": True})])

    CALLED.clear()
    environment = await world_over(streaming_function_model(respond))
    produced = await _weighed(environment, {"model": "openai:gpt-5", "instructions": BRIEFING})

    assert answered == ["host"], "the document's model was never dialled"
    assert _dumped(produced) == {"healthy": True}


async def test_a_worker_granted_one_read_holds_exactly_that_read() -> None:
    """The grant is a filter, and a worker holds the reads it named -- no sandbox, no shell, nothing wider.

    The difference from a briefing is visible right here: a briefing's grants arrive as Python
    functions inside `CodeMode`'s one `run_code` tool, and a worker's arrive as tools it calls
    itself. So `run_sql` is on the wire, and `search`, `graph_expand` and `describe_schema` -- all
    offered by the same server, none of them asked for -- are not.
    """

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        SEEN.append(",".join(sorted(tool.name for tool in info.function_tools)))
        ran = any(isinstance(part, ToolReturnPart) and part.tool_name == "run_sql" for message in messages for part in message.parts)
        if not ran:
            return ModelResponse(parts=[ToolCallPart("run_sql", {"sql": "select * from api.cards limit 3"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"healthy": True})])

    CALLED.clear()
    SEEN.clear()
    environment = await world_over(streaming_function_model(respond))
    produced = await _weighed(environment, {"instructions": BRIEFING}, tools=("run_sql",))

    # `read_tool_result` sits beside it for the reason it sits beside a briefing's `run_code`: it
    # is the spill guard's own tool, a way back to a payload the model was denied, and never
    # something a step asked for. Nothing else is here -- no ungranted read, and no sandbox.
    assert SEEN[0] == "read_tool_result,run_sql"
    held = set(SEEN[0].split(","))
    assert not held & {"search", "graph_expand", "describe_schema"}, "an ungranted read on the wire means the filter leaked"
    assert "run_code" not in held, "a worker holds no sandbox: CodeMode is a briefing's capability"
    assert CALLED == ["run_sql"], "and the grant really reaches the surface"
    assert _dumped(produced) == {"healthy": True}
