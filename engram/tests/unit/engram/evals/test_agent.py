r"""Unit specs for the evals harness (`src/engram/evals/harness/agent.py`).

The harness builds the **agent-under-test** — a pydantic-ai :class:`~pydantic_ai.Agent` =
(model x instructions x retrieval toolset) returning structured
:class:`~engram.evals.shared.domain.RetrievalAnswer` — and wraps it as the pydantic-evals task
function (the callable a :class:`~pydantic_evals.Dataset.evaluate` drives per case).

These specs pin that contract without a network or API key, over `TestModel` (which
synthesizes a valid `RetrievalAnswer` from the agent's output schema):

* :func:`~engram.evals.harness.agent.build_agent` returns an :class:`~pydantic_ai.Agent` whose
  ``output_type`` is :class:`RetrievalAnswer` and whose tool surface is exactly the toolset's
  four retrieval tools (asserted via `TestModel.last_model_request_parameters.function_tools`);
* :func:`~engram.evals.harness.agent.make_task` returns an async ``run_case`` that runs the agent
  on the case's `question` and returns its structured `RetrievalAnswer` output (asserted on
  the returned *type/shape*, not the synthesized content).

Fakes over mocks: the four retrieval handlers are scriptable stubs (mirroring
``tests/unit/serve/test_tools.py``) bundled into a real :class:`RetrievalHandlers`, so the
harness binds a real :func:`build_retrieval_toolset` toolset without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from core.ai.retrieval.contracts import GraphPathsResult, NextSteps, ResultWindow
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.test import TestModel

from engram.evals.harness.agent import build_agent, make_task
from engram.evals.shared.domain import QaInput, RetrievalAnswer
from engram.retrieval.domain import Fetch, GraphExpand, GraphPaths, GraphResult, Search, SqlResult
from engram.retrieval.tools import TOOL_NAMES, RetrievalHandlers, build_retrieval_toolset

if TYPE_CHECKING:
    from engram.retrieval.domain import GraphPath
    from engram.retrieval.tools import RetrievalToolset


_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""


@dataclass
class _DummyCapability(AbstractCapability):
    """A no-op `AbstractCapability` stand-in — the spec only checks it lands in `agent.capabilities`."""


# -- scriptable fakes for the four handlers (mirror tests/unit/serve/test_tools.py) ----------


@dataclass
class FakeSearchHandler:
    """Records the :class:`Search` it was called with; returns the scripted :class:`SqlResult`."""

    result: SqlResult = field(default_factory=SqlResult)
    calls: list[Search] = field(default_factory=list)

    async def __call__(self, query: Search) -> SqlResult:
        self.calls.append(query)
        return self.result


@dataclass
class FakeFetchHandler:
    """Records the :class:`Fetch` it was called with; returns the scripted :class:`SqlResult`."""

    result: SqlResult = field(default_factory=SqlResult)
    calls: list[Fetch] = field(default_factory=list)

    async def __call__(self, query: Fetch) -> SqlResult:
        self.calls.append(query)
        return self.result


@dataclass
class FakeGraphExpandHandler:
    """Records the :class:`GraphExpand` it was called with; returns the scripted :class:`GraphResult`."""

    result: GraphResult = field(default_factory=GraphResult)
    calls: list[GraphExpand] = field(default_factory=list)

    async def __call__(self, query: GraphExpand, *, tools: frozenset[str] = TOOL_NAMES) -> GraphResult:
        del tools
        self.calls.append(query)
        return self.result


@dataclass
class FakeFindPathsHandler:
    """Records the :class:`GraphPaths` it was called with; returns the scripted routes as the typed result."""

    result: tuple[GraphPath, ...] = ()
    calls: list[GraphPaths] = field(default_factory=list)

    async def __call__(self, query: GraphPaths, *, tools: frozenset[str] = TOOL_NAMES) -> GraphPathsResult:
        del tools
        self.calls.append(query)
        return GraphPathsResult(
            paths=self.result,
            next_steps=NextSteps(summary=f"{len(self.result)} route(s)."),
            window=ResultWindow(returned_items=len(self.result)),
        )


def _toolset() -> RetrievalToolset:
    """Build a real retrieval toolset over scriptable handler fakes (no database)."""
    handlers = RetrievalHandlers(
        search=FakeSearchHandler(), fetch=FakeFetchHandler(), graph_expand=FakeGraphExpandHandler(), find_paths=FakeFindPathsHandler()
    )
    return build_retrieval_toolset(handlers, collection=_COLLECTION)


def _model() -> TestModel:
    """A `TestModel` that synthesizes the structured output without calling the tools.

    `call_tools=[]` keeps it from blindly invoking every offered tool — in particular `fetch`,
    which raises :class:`~pydantic_ai.ModelRetry` on the fakes' empty result and would exhaust its
    retries. The tools stay *offered* (`last_model_request_parameters.function_tools` still lists
    all four), so the tool-surface assertion is decoupled from whether the model elects to call them.
    """
    return TestModel(call_tools=[])


class TestBuildAgent:
    def test_output_type_is_retrieval_answer(self):
        agent = build_agent(_model(), instructions="be precise", toolset=_toolset())

        assert agent.output_type is RetrievalAnswer

    async def test_exposes_the_four_retrieval_tools(self):
        model = _model()
        agent = build_agent(model, instructions="be precise", toolset=_toolset())

        await agent.run("any question")

        params = model.last_model_request_parameters
        assert params is not None  # populated by the run above (None only before the first request)
        tool_names = {tool.name for tool in params.function_tools}
        assert tool_names == {"search", "fetch", "graph_expand", "find_paths"}

    async def test_run_returns_a_retrieval_answer(self):
        agent = build_agent(_model(), instructions="be precise", toolset=_toolset())

        result = await agent.run("What is multi-head attention?")

        assert isinstance(result.output, RetrievalAnswer)

    async def test_code_mode_collapses_the_tools_into_run_code(self):
        # With code_mode the retrieval tools are wrapped into a single `run_code` tool the model drives
        # by writing Python; the discrete search/fetch/graph tools are no longer offered at top level.
        model = _model()
        agent = build_agent(model, instructions="be precise", toolset=_toolset(), code_mode=True)

        await agent.run("any question")

        params = model.last_model_request_parameters
        assert params is not None
        tool_names = {tool.name for tool in params.function_tools}
        assert tool_names == {"run_code"}

    def test_extra_capabilities_land_in_the_agent(self):
        # `extra_capabilities` is appended after the built-in Instrumentation (and any CodeMode), so
        # a caller's capability rides alongside them on the agent's combined root capability.
        dummy = _DummyCapability()
        agent = build_agent(_model(), instructions="be precise", toolset=_toolset(), extra_capabilities=[dummy])

        assert dummy in agent._root_capability.capabilities

    def test_no_extra_capabilities_is_the_default(self):
        # Omitting extra_capabilities builds exactly the pre-Task-11 agent, no dummy leaks in.
        agent = build_agent(_model(), instructions="be precise", toolset=_toolset())

        assert not any(isinstance(capability, _DummyCapability) for capability in agent._root_capability.capabilities)


class TestMakeTask:
    async def test_run_case_returns_the_agent_output(self):
        agent = build_agent(_model(), instructions="be precise", toolset=_toolset())
        run_case = make_task(agent)

        answer = await run_case(QaInput(question="What is multi-head attention?"))

        assert isinstance(answer, RetrievalAnswer)

    async def test_run_case_drives_the_agent_with_the_question(self):
        # TestModel synthesizes from the output schema, so assert the produced shape — not the
        # answer text — and that the question (not the task hint) is what the agent is run on.
        agent = build_agent(_model(), instructions="be precise", toolset=_toolset())
        run_case = make_task(agent)

        answer = await run_case(QaInput(question="q", task="compare"))

        assert isinstance(answer.answer, str)
        assert isinstance(answer.cited_source_ids, list)
