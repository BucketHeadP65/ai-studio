"""A custom agent, once activated, resolves as a real `AgentStep` worker -- by name, exactly like inline.

`write_custom_agent`'s whole point is composing REGISTERED behavior a workflow step can wire to by
reference -- this proves the closure end to end: pending, then activated, a stored document
resolves through `FileCustomAgents` at the `core.ai.workflows` `AgentResolver` seam `environment.build_environment`
wires in, a spec naming it by name meets the same cold checks an inline document does, and it
runs to the same answer an equivalent inline document would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import AgentStep, FromNode, FromParameter, ParameterSpec, RunNode, Shape, Workflow, prepare, validate
from core.ai.workflows.domain.validator import IssueType
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from tests.support.environment import world_over
from tests.support.selfauthoring import FakeActivationLog
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry
    from pydantic import JsonValue
    from pydantic_ai.models.function import AgentInfo

BRIEFING = "Read the ledger and say whether the quarter looks healthy."

LEDGER = {"type": "object", "title": "LedgerIn", "properties": {"net": {"type": "integer"}}, "required": ["net"]}

VERDICT = {"type": "object", "title": "Verdict", "properties": {"healthy": {"type": "boolean"}}, "required": ["healthy"]}

_DOCUMENT = f"instructions: {BRIEFING!r}\n"



def _agent_store(tmp_path: Path) -> FileCustomAgents:
    return FileCustomAgents(tmp_path / "custom" / "agents", activations=FakeActivationLog())


def _weighing(agent: Mapping[str, JsonValue] | str) -> Workflow:
    """One worker step, fed by one parameter, and nothing else in the graph."""
    return Workflow(
        name="weigh-the-quarter",
        version=1,
        description="A worker reads a ledger and says whether the quarter looks healthy.",
        parameters={"ledger": ParameterSpec(description="the ledger", json_schema=LEDGER)},
        inline=(AgentStep(name="weigh", agent=agent, inputs={"ledger": Shape(json_schema=LEDGER)}, output=Shape(json_schema=VERDICT)),),
        nodes=(RunNode(id="weigh", work="weigh", inputs={"ledger": FromParameter(parameter="ledger")}),),
        output=FromNode(node="weigh"),
    )


async def _weighed(environment: StepRegistry, agent: Mapping[str, JsonValue] | str) -> object:
    ready = prepare(_weighing(agent), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("weigh")
    assert implementation is not None
    return await implementation.function(ledger={"net": 400})


def _dumped(produced: object) -> dict[str, object]:
    assert isinstance(produced, BaseModel)
    return produced.model_dump()


def _answers_healthy(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    del messages
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"healthy": True})])


# --- the pending gate, at the resolution seam ------------------------------------------------


async def test_a_pending_agent_is_not_resolvable_by_name(tmp_path: Path) -> None:
    """Written but never activated -- a spec naming it by name refuses cold, same as any bad wiring."""
    _agent_store(tmp_path).write("weigher", _DOCUMENT)
    environment = await world_over(streaming_function_model(_answers_healthy), state=tmp_path)

    report = validate(_weighing("weigher"), environment)

    assert not report.ok
    assert any(issue.kind is IssueType.BAD_TASK_DEFINITION for issue in report.issues)
    assert any("harness activate agent weigher" in issue.detail for issue in report.issues)


async def test_an_unknown_stored_name_is_refused_naming_it(tmp_path: Path) -> None:
    environment = await world_over(streaming_function_model(_answers_healthy), state=tmp_path)

    report = validate(_weighing("nobody"), environment)

    assert not report.ok
    assert any("No custom agent named 'nobody'." in issue.detail for issue in report.issues)


async def test_activation_makes_the_stored_name_resolve(tmp_path: Path) -> None:
    store = _agent_store(tmp_path)
    store.write("weigher", _DOCUMENT)
    store.activate("weigher")
    environment = await world_over(streaming_function_model(_answers_healthy), state=tmp_path)

    report = validate(_weighing("weigher"), environment)

    assert report.ok, report.issues


# --- name ref vs inline document equivalence --------------------------------------------------


async def test_a_stored_reference_runs_to_completion_and_its_answer_lands_typed(tmp_path: Path) -> None:
    store = _agent_store(tmp_path)
    store.write("weigher", _DOCUMENT)
    store.activate("weigher")
    environment = await world_over(streaming_function_model(_answers_healthy), state=tmp_path)

    produced = await _weighed(environment, "weigher")

    assert isinstance(produced, BaseModel)
    assert type(produced).__name__ == "Verdict"
    assert _dumped(produced)["healthy"] is True


async def test_a_stored_reference_and_its_equivalent_inline_document_run_to_the_same_answer(tmp_path: Path) -> None:
    store = _agent_store(tmp_path)
    store.write("weigher", _DOCUMENT)
    store.activate("weigher")
    environment = await world_over(streaming_function_model(_answers_healthy), state=tmp_path)

    by_name = await _weighed(environment, "weigher")
    inline = await _weighed(environment, {"instructions": BRIEFING})

    assert _dumped(by_name) == _dumped(inline)


async def test_the_hosts_model_runs_a_stored_reference_whatever_the_document_named(tmp_path: Path) -> None:
    """Same reach claim `test_worker_runs.py` makes for an inline document, over a stored one."""
    answered: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        answered.append("host")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"healthy": True})])

    store = _agent_store(tmp_path)
    store.write("weigher", f"model: openai:gpt-5\ninstructions: {BRIEFING!r}\n")
    store.activate("weigher")
    environment = await world_over(streaming_function_model(respond), state=tmp_path)

    produced = await _weighed(environment, "weigher")

    assert answered == ["host"], "the document's model was never dialled"
    assert _dumped(produced) == {"healthy": True}


async def test_deactivating_a_stored_agent_stops_it_resolving(tmp_path: Path) -> None:
    store = _agent_store(tmp_path)
    store.write("weigher", _DOCUMENT)
    store.activate("weigher")
    environment = await world_over(streaming_function_model(_answers_healthy), state=tmp_path)
    assert validate(_weighing("weigher"), environment).ok, "sanity: it resolved while active"

    store.deactivate("weigher")

    report = validate(_weighing("weigher"), environment)
    assert not report.ok
    assert any("harness activate agent weigher" in issue.detail for issue in report.issues)
