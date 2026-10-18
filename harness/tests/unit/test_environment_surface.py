"""What a step can actually reach.

These tests exist because the surface was once wrong in a way nothing caught. The step agents held
three hand-written queries while the retrieval engine's real tools -- full text by id, read-only
SQL, the code sandbox -- sat one filter away, unused. Nothing failed. The steps just answered worse
than they could, and the only visible symptom was an investigator instructed to report
contradictions while holding no tool able to find one.

The stand-in (`tests/support/environment.py`) is a plain `FunctionToolset` wearing `RetrievalSurface`'s
two methods rather than a live engram, and the seams that broke are all still real: the filter,
the per-step grant, the derived offer, and the cold refusal. What it does not exercise is the MCP
transport, which was never the thing at fault -- that was verified against the running server
instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contract_port import ADAPT_TOOL, FETCH_STEP_TOOL, FETCH_TOOL, SAVE_TOOL, SEARCH_STEPS_TOOL, SEARCH_TOOL
from core.ai.workflows.contracts import (
    AgentStep,
    FromNode,
    FromParameter,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    prepare,
    run_workflow,
    validate,
)
from core.ai.workflows.infrastructure.library_collection import WORKFLOW_COLLECTION
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from harness import rendering
from harness.adapters.environment import ACT_OUTPUT_LIMIT, step_guards
from harness.adapters.websearch import WEB_SEARCH_TOOL
from harness.domain.ports import NOISE_TOOLS
from harness.session import Runtime
from tests.support.environment import BOOT, CALLED, SEEN, defined_step, spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pydantic_ai.models.function import AgentInfo


@pytest.fixture
async def environment() -> StepRegistry:
    """An environment whose retrieval surface is the stand-in and whose model calls every tool it is given.

    Nothing dials a database: the one hand-written knowledge base tool is gone along with the six steps it
    served, so an environment is now the retrieval surface plus one way to run a declared step.
    """
    CALLED.clear()
    SEEN.clear()
    BOOT.clear()
    return await world_over(TestModel())


async def test_nothing_is_registered(environment: StepRegistry) -> None:
    """The headline invariant, and the one worth failing loudly on.

    Six steps used to live in `build_environment` -- two retrieval agents, a merge, three publishers -- and
    every one was a special case of something general: the publishers were one function written three
    times, the agents were `run_defined_step` with the briefing in Python instead of in data. A
    workflow declares what it needs now, including what it needs in order to *act*: writing is the
    shell, granted by name, not a registered verb per effect.

    So a name appearing here is a domain -- or a bespoke primitive -- leaking back in. `notify_owner`
    and `run_external_agent` are the two host steps: a notification and a spawned external agent
    are both effects no workflow step can express as a tool grant, so they stay registered verbs.
    """
    assert list(environment.names) == ["notify_owner", "run_external_agent"], "a registered step here is a domain leaking back in"


async def test_the_offer_is_read_off_the_surface(environment: StepRegistry) -> None:
    """What a declared step may ask for: the server's reads, derived -- plus the shell it acts with.

    `run_command` is the one name here that is not read off a server, and it is the answer to how a
    workflow does anything at all. A step that must write a file asks for the shell and writes it;
    there is no `write_document`, because a shell is what an agent writes files with. What makes that
    safe is the graph, not the grant: the step is a node in a spec a person read, and wiring it from
    a gate makes the approval structural.
    """
    assert set(environment.grants) == {
        "search",
        "fetch",
        "run_sql",
        "graph_expand",
        "describe_schema",
        "run_command",
        "list_undigested",
        "write_note",
        "request_source",
        "read_source",
        "list_recent_digests",
        "digest_skeleton",
        "resolve_entities",
        "read_page",
        "source_queue_policy",
        "write_policy",
        "source_queue_rows",
        "source_queue_decide",
        "decide_page",
        WEB_SEARCH_TOOL,
    }


async def test_sql_and_the_sandbox_are_not_filtered_out(environment: StepRegistry) -> None:
    """The regression this file is named for: `run_sql` is grantable, so a step can ask for it."""
    assert "run_sql" in environment.grants


async def test_noise_is_not_on_offer(environment: StepRegistry) -> None:
    """`submit_feedback` is real and irrelevant. It is not grantable, so it cannot be asked for."""
    assert all(tool not in environment.grants for tool in NOISE_TOOLS)


async def test_a_step_asking_for_noise_is_refused_cold(environment: StepRegistry) -> None:
    """Because the offer excludes it, the refusal lands at validation rather than mid-run."""
    report = validate(spec_for(defined_step(tools=("submit_feedback",))), environment)
    assert not report.ok
    assert any("submit_feedback" in issue.detail for issue in report.issues)


async def test_a_granted_step_holds_exactly_what_it_asked_for() -> None:
    """The point of the whole change: a step that asks for SQL can run SQL, and nothing else.

    Asserted with a `FunctionModel` rather than `TestModel` because the step now holds `CodeMode`:
    the granted tool is a Python function inside `run_code`, so a model that sprays every tool it
    is offered would only ever call `run_code` with generated nonsense (Monty type-checks it and
    the retry budget dies -- which is how this test first told me the capability was live). Writing
    the program by hand asserts the real thing instead: what the model was offered, and what came
    out the other end of the sandbox.
    """
    program = "rows = await run_sql(sql='select * from api.contradictions limit 3')\nrows"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        SEEN.append(",".join(sorted(tool.name for tool in info.function_tools)))
        ran = any(isinstance(part, ToolReturnPart) and part.tool_name == "run_code" for message in messages for part in message.parts)
        if not ran:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": program})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "read the contradictions view"})])

    CALLED.clear()
    SEEN.clear()
    BOOT.clear()
    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(defined_step(tools=("run_sql",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    produced = await implementation.function(question="what do the sources disagree about?")

    # The grant is INSIDE the sandbox, not beside it: no `run_sql` on the wire.
    #
    # `read_tool_result` sits beside `run_code` on purpose, and it is the one tool that must. It
    # recovers a payload the MODEL was denied when a program's digest overflowed, so a program
    # calling it is a program reading its own earlier output through a second sandbox — watched
    # live costing three `run_code` calls to recover one digest. It is a guard's tool, never a
    # grant: nothing a step asked for appears here.
    assert SEEN[0] == "read_tool_result,run_code"
    assert "run_sql" not in SEEN[0], "a granted tool on the wire means the sandbox filter leaked"
    # And the grant really reaches the host: `search` here would mean the filter leaked.
    assert CALLED == ["run_sql"]
    assert _dumped(produced) == {"note": "read the contradictions view"}


def _dumped(produced: object) -> object:
    """A dynamically built output model, as plain data. Pyright cannot know its attributes."""
    assert isinstance(produced, BaseModel)
    return produced.model_dump()


async def test_a_step_that_asked_for_the_shell_can_act() -> None:
    """How a workflow does anything: it asks for the shell and uses it.

    Asserted on what the step actually HELD rather than on a file appearing, because the point under
    test is the grant. A `write_document` step used to sit in the registry for this, which was the
    wrong shape twice over -- it was a bespoke verb for one effect, and it meant a step could not do
    the obvious neighbouring things (run a formatter, check what it wrote, move a file) without
    another verb each.
    """
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        SEEN.append(",".join(sorted(tool.name for tool in info.function_tools)))
        ran = any(isinstance(part, ToolReturnPart) and part.tool_name == "run_command" for message in messages for part in message.parts)
        if not ran:
            return ModelResponse(parts=[ToolCallPart("run_command", {"command": "printf hi > /dev/null && echo done"})])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "wrote it"})])

    SEEN.clear()
    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(defined_step(tools=("run_command",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    produced = await implementation.function(question="write something and tell me it worked")

    # The shell's command tools sit BESIDE the sandbox, unlike data grants: their argument is
    # itself a program, and folding one code surface into another makes the model quote a shell
    # command inside a generated Python string. Asking for the shell grants the shell surface —
    # both command tools on the wire, the passive check/stop pair folded like data.
    assert SEEN[0] == "read_tool_result,run_code,run_command,start_command"
    assert _dumped(produced) == {"note": "wrote it"}


async def test_a_step_that_asked_for_nothing_holds_nothing(environment: StepRegistry) -> None:
    """A briefing with no tools works from its inputs alone, and the surface is never reached."""
    ready = prepare(spec_for(defined_step(tools=())), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None
    await implementation.function(question="answer from the briefing alone")
    assert CALLED == []


async def test_the_world_is_not_rendered_for_a_prompt_at_all(environment: StepRegistry) -> None:
    """A `Environment` describes what the RUNTIME can execute. Nothing here is prompt material.

    It used to be both: `Runtime.steps` rendered this environment as six signatures and every request
    carried them. The orchestrator never needed that -- running a stored workflow takes an id and its
    parameters, and authoring learns the vocabulary from the validator's refusal the one time it
    guesses wrong. So the renderer is gone, and this asserts nothing quietly grows back: a `Environment`
    that can format itself for a system prompt is a `Environment` somebody will put in one.
    """
    assert not hasattr(Runtime, "steps"), "no prompt renderer on the session"
    assert not hasattr(environment, "step_lines"), "nor on the environment"
    assert not hasattr(rendering, "step_lines"), "nor left behind in the renderer for someone to find"
    assert not hasattr(environment, "steps")


async def test_nothing_is_read_from_the_server_at_boot(environment: StepRegistry) -> None:
    """Building an environment calls no tool. The schema is a tool a step asks for, not an injection.

    It was the other way around: `describe_schema`'s 13,000 characters were read here once and
    injected into every step's instructions, on the ruling that static environment knowledge belongs in the
    prompt. That is gone -- a schema nobody asked for is 13,000 characters paid for on every request
    of every step, including the ones that never write SQL. It is grantable instead, and the
    capability's instructions tell a step to read it before writing SQL.
    """
    assert BOOT == [], "building an environment reads nothing from the server"
    assert "describe_schema" in environment.grants, "a step that writes SQL can ask for the columns"


async def test_a_workers_direct_reads_are_spilled_not_carried() -> None:
    """A worker holds its reads as a plain toolset -- no CodeMode consumes their returns.

    Without a band on those tools, one whole-page fetch rides the worker's own history and is
    re-sent on every later request of its run. A briefing's reads live inside the sandbox where
    the program consumes them, so the default guard set covers only the program digest.
    """
    worker_guards = next(g for g in step_guards(24, direct_reads=("run_sql", "fetch")) if isinstance(g, ToolOutputLimits))
    assert isinstance(worker_guards.tool_filter, tuple), "the filter is names here, not a selector function"
    assert "run_sql" in worker_guards.tool_filter
    assert "fetch" in worker_guards.tool_filter
    assert worker_guards.per_tool["run_sql"][0].over == ACT_OUTPUT_LIMIT, "the act band, not the program digest's"

    briefing_guards = next(g for g in step_guards(24) if isinstance(g, ToolOutputLimits))
    assert briefing_guards.tool_filter == ("run_code",), "a briefing's reads are the sandbox's business"


async def test_a_worker_granted_the_shell_holds_it() -> None:
    """A worker that named the shell gets one, because the orchestrator delegating work asked for it.

    The shell it gets closes the console, so it can act on files while the `harness` verbs that
    decide what every later session holds still refuse it. A worker granted something this run
    genuinely cannot serve is still refused loudly, which the test below pins.
    """
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        SEEN.append(",".join(sorted(tool.name for tool in info.function_tools)))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "ran the formatter"})])

    CALLED.clear()
    SEEN.clear()
    BOOT.clear()
    environment = await world_over(streaming_function_model(respond))
    spec = Workflow(
        name="format-once",
        version=1,
        description="One brought worker holding the shell the orchestrator granted it.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(
            AgentStep(
                name="dig",
                agent={"instructions": "Run the formatter over the answer and report what changed."},
                inputs={"question": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
                tools=("run_command",),
            ),
        ),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="dig"),
    )
    assert validate(spec, environment).ok, "run_command is grantable, and a worker may now hold it"
    await run_workflow(spec, environment, params={"question": "does the quarter look healthy?"}, approval=_NoGates())

    # What the worker was actually OFFERED, not merely that the run survived. A worker whose grant
    # was dropped on the floor would run to completion just the same, which is the failure this test
    # exists to catch rather than the crash.
    assert any("run_command" in offered for offered in SEEN), f"the worker was never offered the shell it named: {SEEN}"


async def test_a_defined_step_granted_web_search_gets_it_folded_into_run_code() -> None:
    """`web_search` behaves like any other read for a briefing: it rides inside `run_code`.

    Never actually called -- the `FunctionModel` answers with the output tool on its first turn,
    so this proves the offer, never a real search.
    """
    captured: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(defined_step(tools=(WEB_SEARCH_TOOL,))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the literature say")

    run_code = next(tool for tool in captured["info"].function_tools if tool.name == "run_code")
    assert WEB_SEARCH_TOOL in (run_code.description or "")


async def test_a_defined_step_not_granted_web_search_does_not_get_it() -> None:
    captured: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        captured["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond))
    ready = prepare(spec_for(defined_step(tools=("run_sql",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="what does the literature say")

    run_code = next(tool for tool in captured["info"].function_tools if tool.name == "run_code")
    assert WEB_SEARCH_TOOL not in (run_code.description or "")


class _NoGates:
    async def review(self, gate_id: str, value: object, *, note: str = "") -> bool:  # pragma: no cover - never reached
        del value, note
        raise AssertionError(gate_id)


async def test_a_worker_can_hold_web_search_directly_unlike_the_shell() -> None:
    """Unlike `run_command`, `web_search` behaves as a read for a worker: grantable and reachable.

    The model answers immediately with the output tool -- never actually calling `web_search` --
    so this proves the offer (it is on the wire, unfolded, unlike a briefing's) without a real
    search.
    """
    answered: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        answered["info"] = info
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "not called, only offered"})])

    environment = await world_over(streaming_function_model(respond))
    spec = Workflow(
        name="search-once",
        version=1,
        description="One worker granted web_search only.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(
            AgentStep(
                name="dig",
                agent={"instructions": "Search the web and report what you found."},
                inputs={"question": Shape(json_schema={"type": "string"})},
                output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
                tools=(WEB_SEARCH_TOOL,),
            ),
        ),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="dig"),
    )
    assert validate(spec, environment).ok, "the cold check passes -- web_search behaves like a read here"

    await run_workflow(spec, environment, params={"question": "does the literature agree?"}, approval=_NoGates())

    assert WEB_SEARCH_TOOL in {tool.name for tool in answered["info"].function_tools}


async def test_the_authoring_ports_toolset_offers_the_shelf_tools() -> None:
    """The authoring port's own surface, wired with the vocabulary `composition.py` uses.

    `WORKFLOW_COLLECTION` names a library (`search_steps_tool`, `fetch_step_tool`), and that alone
    is what puts `search_steps` and `fetch_step` beside the four contract verbs -- nothing in the
    harness's own composition has to mount them separately. `expose=()` means nothing from
    `source` reaches the model, so what `get_tools` returns is exactly the port's own surface.
    """
    port = WorkflowContract[None](
        source=FunctionToolset[object](), vocabulary=WORKFLOW_COLLECTION, expose=(), registry=StepRegistry(), saved_by="test"
    )
    toolset = port.get_toolset()
    assert toolset is not None
    ctx: RunContext[None] = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    assert set(tools) == {SEARCH_TOOL, FETCH_TOOL, SAVE_TOOL, ADAPT_TOOL, SEARCH_STEPS_TOOL, FETCH_STEP_TOOL}
