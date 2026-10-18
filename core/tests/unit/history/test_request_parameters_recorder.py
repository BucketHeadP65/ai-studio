from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.output import OutputObjectDefinition
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence

from core.ai.history.request_parameters_recorder import RequestParametersRecorder
from tests.support.fakes.sink import FakeSink


class Unserializable:
    """A value nothing can turn into JSON."""


@dataclass
class Sabotage(AbstractCapability[None]):
    """Puts a value nothing can turn into JSON into the request parameters and into the model settings."""

    async def before_model_request(self, ctx: RunContext[None], request_context: ModelRequestContext) -> ModelRequestContext:
        request_context.model_request_parameters.output_object = OutputObjectDefinition(json_schema={"bad": Unserializable()})
        request_context.model_settings = {"extra_body": Unserializable()}
        return request_context


def lookup_city(name: str) -> str:
    """Describe the weather in a city."""
    return f"{name}: sunny"


def request_parameters_of(parameters: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """The request-parameters object one recorded set carries."""
    recorded = parameters["request_parameters"]
    assert isinstance(recorded, dict)
    return recorded


def function_tool_names(parameters: Mapping[str, JsonValue]) -> list[str]:
    """The names of the tools one recorded set says the model could call."""
    tools = request_parameters_of(parameters)["function_tools"]
    assert isinstance(tools, list)
    names: list[str] = []
    for tool in tools:
        assert isinstance(tool, dict)
        names.append(str(tool["name"]))
    return names


class TestRequestParametersRecorder:
    async def test_records_one_row_per_model_request(self):
        sink = FakeSink()
        agent = Agent(TestModel(call_tools="all"), tools=[lookup_city], capabilities=[RequestParametersRecorder(sink=sink)])

        result = await agent.run("What is the weather in Paris?")

        assert [call.step_index for call in sink.calls] == [1, 2]
        assert {call.run_id for call in sink.calls} == {result.run_id}

    async def test_parameters_name_the_tools_the_model_could_call(self):
        sink = FakeSink()
        agent = Agent(TestModel(), tools=[lookup_city], capabilities=[RequestParametersRecorder(sink=sink)])

        await agent.run("hi")

        parameters = sink.calls[0].parameters
        assert function_tool_names(parameters) == ["lookup_city"]
        assert parameters["model"] == "test"

    async def test_a_value_that_will_not_serialize_is_left_out_rather_than_failing_the_run(self):
        sink = FakeSink()
        agent = Agent(
            TestModel(), deps_type=type(None), tools=[lookup_city], capabilities=[Sabotage(), RequestParametersRecorder(sink=sink)]
        )

        await agent.run("hi")

        parameters = sink.calls[0].parameters
        assert function_tool_names(parameters) == ["lookup_city"]
        assert "output_object" not in request_parameters_of(parameters)
        assert parameters["model_settings"] == {}

    async def test_the_step_index_is_the_one_the_harness_snapshots_under(self):
        sink = FakeSink()
        store = InMemoryStepStore()
        agent = Agent(
            TestModel(call_tools="all"),
            tools=[lookup_city],
            capabilities=[StepPersistence(store=store), RequestParametersRecorder(sink=sink)],
        )

        result = await agent.run("What is the weather in Paris?")

        latest = await store.latest_snapshot(run_id=result.run_id)

        assert latest is not None
        assert sink.calls[-1].step_index == latest.step_index

    async def test_the_run_id_is_the_one_the_transcript_registered(self):
        sink = FakeSink()
        store = InMemoryStepStore()
        agent = Agent(
            TestModel(),
            tools=[lookup_city],
            capabilities=[StepPersistence(store=store, agent_name="proof"), RequestParametersRecorder(sink=sink)],
        )

        result = await agent.run("What is the weather in Paris?")

        registered = (await store.list_runs())[-1]

        assert registered.run_id != result.run_id
        assert sink.calls[-1].run_id == registered.run_id
