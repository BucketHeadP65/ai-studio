"""Every agent that runs tools holds the workspace it needs, and a command sees only the environment the harness lists."""

from __future__ import annotations

import fnmatch

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import NoneType
from typing import TYPE_CHECKING

import pytest

from core.ai.format_maps.contracts import FormatMaps, MappedTool
from core.ai.history.conversation_record import messages_of
from core.ai.workflows.contracts import AgentStep, Shape, StepRegistry, WorkflowContract
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability, LocalWorkspace
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, WorkspaceRef
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness import DynamicWorkflow
from pydantic_ai_harness.pydantic_ai_docs import PydanticAIDocs
from pydantic_ai_harness.spend import Budget, SpendLimits
from pydantic_core import to_jsonable_python

from harness.adapters.environment import COMMAND_ENV_NAMES, content_cache, step_shell, step_workspace, workspace_env
from harness.adapters.model_list import ModelList
from harness.adapters.model_tiers import ModelTiers
from harness.composition import build_worker_agent
from harness.features.authoring import workers
from harness.features.authoring.agent import build_agent
from harness.features.authoring.process import WorkflowProcess
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.features.sessions.project_config import ProjectConfig
from harness.plugins import Switched
from harness.session import Runtime, SessionPool
from harness.settings import CHECKOUT, CONSOLE_CLOSED_ENV, WORKSPACE, RunSettings, WebSearchSettings
from tests.support.environment import fake_engram
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.runtime import a_runtime
from tests.support.state import a_state, a_transcript, records_under
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.thinking import Thinking
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models import Model
    from pydantic_ai.models.function import AgentInfo, FunctionModel

PROVIDER_KEY = "sk-test-provider-key-0000"
LISTED = {"TMPDIR": "/tmp/listed-tmp", "SSH_AUTH_SOCK": "/tmp/listed.sock", "USER": "listed-user", "LOGNAME": "listed-login"}  # noqa: S108 (inert strings the tests pass in, never opened)


class TestWorkspaceEnv:
    def test_the_listed_names_are_taken_when_set(self) -> None:
        assert workspace_env({**LISTED, "OPENAI_API_KEY": PROVIDER_KEY, "UNLISTED": "x"}) == LISTED

    def test_a_listed_name_that_is_not_set_is_left_out(self) -> None:
        assert workspace_env({"USER": "someone"}) == {"USER": "someone"}

    def test_nothing_else_is_taken(self) -> None:
        assert set(COMMAND_ENV_NAMES) == set(LISTED)
        assert workspace_env({"PATH": "/bin", "HOME": "/home/x", "OPENAI_API_KEY": PROVIDER_KEY}) == {}


@dataclass
class _NoReads(AbstractCapability[Runtime]):
    """Fills the knowledge base read slot."""


def _chat_agent(
    tmp_path: Path, model: Model, *, environ: Mapping[str, str] | None = None, tiers: ModelTiers | None = None
) -> Agent[Runtime, object]:
    port = WorkflowContract[Runtime](
        source=folder_toolset(tmp_path / "workflows"), vocabulary=FOLDER, expose=(), registry=StepRegistry(), saved_by="test"
    )
    return build_agent(
        model,
        WorkflowProcess(
            port=port,
            records=records_under(tmp_path),
            reporter=SpendReporter(FakeTelemetry()),
            scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
            sessions=APool(SessionPool(a_runtime())),
            topics=FakeBroadcaster(),
            run=RunSettings(),
        ),
        _NoReads(),
        state=a_state(tmp_path),
        custom=tmp_path / "custom",
        web_search=WebSearchSettings(),
        store=a_transcript(tmp_path),
        parameters=FakeRequestParametersSink(),
        external_agents=dict,
        project=ProjectConfig(),
        tiers=tiers,
        environ=environ,
    )


def _calls_then_reports(calls: Sequence[ToolCallPart]) -> FunctionModel:
    """A model that makes `calls` one at a time, then answers with every tool result it was given, in order."""

    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        results: list[str] = []
        for message in messages:
            if isinstance(message, ModelRequest):
                results.extend(str(part.content) for part in message.parts if isinstance(part, ToolReturnPart))
        if len(results) < len(calls):
            call = calls[len(results)]
            return ModelResponse(parts=[ToolCallPart(call.tool_name, call.args, tool_call_id=f"call-{len(results)}")])
        return ModelResponse(parts=[TextPart("\n=====\n".join(results))])

    return streaming_function_model(function)


class TestChatAgentWorkspace:
    async def test_a_command_starts_in_the_workspace_and_sees_only_the_listed_environment(self, tmp_path: Path) -> None:
        environ = {**LISTED, "OPENAI_API_KEY": PROVIDER_KEY, "UNLISTED_SETTING": "unlisted-value"}
        model = _calls_then_reports([ToolCallPart("run_command", {"command": "pwd; printenv"})])
        agent = _chat_agent(tmp_path, model, environ=environ)

        result = await agent.run("look around", deps=a_runtime(state=a_state(tmp_path)))

        output = str(result.output)
        assert str(WORKSPACE.resolve()) in output.splitlines()
        for name, value in LISTED.items():
            assert f"{name}={value}" in output
        assert PROVIDER_KEY not in output
        assert "UNLISTED_SETTING" not in output

    async def test_a_file_is_read_through_the_workspace(self, tmp_path: Path) -> None:
        relative = (CHECKOUT / "pyproject.toml").relative_to(WORKSPACE).as_posix()
        model = _calls_then_reports([ToolCallPart("read_file", {"path": relative})])
        agent = _chat_agent(tmp_path, model)

        result = await agent.run("read it", deps=a_runtime(state=a_state(tmp_path)))

        assert "[project]" in str(result.output)


class _RecordingBuild:
    """The model-building seam: every tier answers with one scripted model."""

    def __init__(self, model: Model) -> None:
        self.model = model

    def __call__(self, model_id: str, *, thinking: Thinking) -> Model:
        del model_id, thinking
        return self.model


def _parent(tmp_path: Path, workspace: Path, worker_model: Model, *, checkout: Path | None = None) -> Agent[Runtime, str]:
    """A parent holding only its own workspace and the delegate door, so the worker is the one under test."""
    state = a_state(tmp_path)
    spend = SpendLimits[Runtime](budgets=[Budget(usd=Decimal(5), window="run")])
    tiers = ModelTiers(state=state, models=ModelList(listing=None), build=_RecordingBuild(worker_model))
    delegate, _ = workers.delegation_plugins(
        tiers=tiers,
        reads=None,
        spend=spend,
        state=state,
        workspace=workspace,
        checkout=checkout if checkout is not None else workspace / "checkout",
    )

    def parent_function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        returned = [
            part for message in messages if isinstance(message, ModelRequest) for part in message.parts if isinstance(part, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("delegate_task", {"agent_name": "worker", "task": "go"})])
        return ModelResponse(parts=[TextPart(str(returned[0].content))])

    return Agent(streaming_function_model(parent_function), deps_type=Runtime, capabilities=[LocalWorkspace(workspace), delegate])


def _collect[T, DepsT, OutputT](agent: Agent[DepsT, OutputT], kind: type[T]) -> list[T]:
    found: list[T] = []
    agent._root_capability.apply(lambda cap: found.append(cap) if isinstance(cap, kind) else None)
    return found


class TestDelegateWorkspace:
    """A delegate starts in its parent's workspace, and its file tools reach only the checkout inside it."""

    async def test_a_delegate_runs_in_its_parents_workspace(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        worker_model = _calls_then_reports([ToolCallPart("run_command", {"command": "pwd"})])

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert str(workspace.resolve()) in result.output.splitlines()

    async def test_a_delegate_reads_and_writes_a_file_inside_the_checkout(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        (workspace / "checkout" / "notes.txt").write_text("inside-the-checkout", encoding="utf-8")
        worker_model = _calls_then_reports(
            [
                ToolCallPart("read_file", {"path": "checkout/notes.txt"}),
                ToolCallPart("write_file", {"path": "checkout/new.txt", "content": "written"}),
            ]
        )

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert "inside-the-checkout" in result.output
        assert (workspace / "checkout" / "new.txt").read_text(encoding="utf-8") == "written"

    async def test_a_delegate_is_refused_a_file_in_a_sibling_folder_of_the_checkout(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        (workspace / "sibling").mkdir()
        (workspace / "sibling" / "other.txt").write_text("in-the-sibling", encoding="utf-8")
        worker_model = _calls_then_reports([ToolCallPart("read_file", {"path": "sibling/other.txt"})])

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert "does not match any allowed pattern" in result.output
        assert "in-the-sibling" not in result.output

    async def test_a_denied_file_inside_the_checkout_is_still_refused(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        (workspace / "checkout" / "a.md.threads.json").write_text("{}", encoding="utf-8")
        worker_model = _calls_then_reports([ToolCallPart("read_file", {"path": "checkout/a.md.threads.json"})])

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert "denied by pattern '**/*.threads.json'" in result.output

    async def test_a_delegate_command_naming_a_denied_path_relative_to_its_start_is_refused(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout" / "sub").mkdir(parents=True)
        (workspace / "checkout" / "sub" / "a.md.threads.json").write_text("{}", encoding="utf-8")
        worker_model = _calls_then_reports([ToolCallPart("run_command", {"command": "cat checkout/sub/*.json"})])

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert "denied by pattern '**/*.threads.json'" in result.output

    def test_a_checkout_outside_the_workspace_leaves_the_delegate_no_files_plugin(self, tmp_path: Path) -> None:
        state = a_state(tmp_path)
        spend = SpendLimits[Runtime](budgets=[Budget(usd=Decimal(5), window="run")])

        worker = workers.worker_agent(
            "worker",
            model=TestModel(),
            settings=None,
            description="d",
            reads=None,
            spend=spend,
            state=state,
            workspace=tmp_path / "workspace",
            checkout=tmp_path / "elsewhere",
        )

        assert {switched.plugin for switched in _collect(worker, Switched)} == {"shell"}
        text = workers.worker_instructions(workspace=tmp_path / "workspace", checkout=tmp_path / "elsewhere", files=False)
        assert "file tools" not in text
        assert "`cd " in text

    def test_a_delegate_tells_the_model_where_its_commands_start_and_where_its_file_tools_reach(self) -> None:
        text = workers.worker_instructions(workspace=WORKSPACE, checkout=CHECKOUT)

        assert str(WORKSPACE) in text
        assert f"`cd {CHECKOUT}`" in text
        assert "file tools" in text


class TestDelegateWithoutAWorkspace:
    async def test_a_worker_run_alone_fails_at_run_start(self, tmp_path: Path) -> None:
        state = a_state(tmp_path)
        spend = SpendLimits[Runtime](budgets=[Budget(usd=Decimal(5), window="run")])
        worker = workers.worker_agent("worker", model=TestModel(), settings=None, description="d", reads=None, spend=spend, state=state)

        with pytest.raises(UserError, match="needs a workspace"):
            await worker.run("go", deps=a_runtime(state=state))


class TestCapabilityCreationInTheChatWorkspace:
    async def test_the_chat_agent_runs_with_capability_creation_mounted_and_names_its_tool(self, tmp_path: Path) -> None:
        instructions: list[str] = []

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del messages
            instructions.append(info.instructions or "")
            return ModelResponse(parts=[TextPart("done")])

        agent = _chat_agent(tmp_path, streaming_function_model(function))

        result = await agent.run("hello", deps=a_runtime(state=a_state(tmp_path)))

        assert result.output == "done"
        assert "write_custom_capability" in instructions[-1]


class TestStepWorkspaces:
    """A step's agent holds a read-only workspace for its skills, and a writable one when it also holds a shell."""

    def test_a_skills_only_step_is_read_only(self) -> None:
        workspace = step_workspace(writable=False)

        assert workspace.read_only is True
        assert Path(workspace.working_dir) == CHECKOUT

    def test_a_step_with_a_shell_is_writable(self) -> None:
        assert step_workspace(writable=True).read_only is False


class TestDocsThroughFormatMaps:
    """`read_pyai_docs` reads its checkout through the run's workspace, and `FormatMaps` still maps and slices it."""

    async def test_a_large_docs_page_answers_with_its_map_and_a_part_is_sliced_from_the_cache(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "tools.md").write_text("# Tools\n\n## Alpha\n\nalpha body\n\n## Beta\n\nbeta body\n", encoding="utf-8")
        seen: list[str] = []

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            returned = [
                str(part.content)
                for message in messages
                if isinstance(message, ModelRequest)
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            seen[:] = returned
            if not returned:
                return ModelResponse(parts=[ToolCallPart("read_pyai_docs", {"topic": "tools"})])
            if len(returned) == 1:
                return ModelResponse(parts=[ToolCallPart("read_pyai_docs", {"topic": "tools", "part": "Beta"})])
            return ModelResponse(parts=[TextPart("done")])

        agent = Agent(
            streaming_function_model(function),
            deps_type=Runtime,
            capabilities=[
                LocalWorkspace(tmp_path),
                PydanticAIDocs[Runtime](local_docs_path=docs),
                FormatMaps[Runtime](tools={"read_pyai_docs": MappedTool(format="markdown", over=10)}, cache=content_cache()),
            ],
        )

        await agent.run("read the docs", deps=a_runtime(state=a_state(tmp_path)))

        map_answer, part_answer = seen
        assert "'part': 'Alpha'" in map_answer
        assert "'part': 'Beta'" in map_answer
        assert "beta body" in part_answer
        assert "alpha body" not in part_answer


def _scripts_parent(tmp_path: Path, workspace: Path, worker_model: Model, *, environ: Mapping[str, str]) -> Agent[Runtime, str]:
    """A parent holding its own workspace and the agent-scripts door, which starts every tier worker without a workspace."""
    state = a_state(tmp_path)
    spend = SpendLimits[Runtime](budgets=[Budget(usd=Decimal(5), window="run")])
    tiers = ModelTiers(state=state, models=ModelList(listing=None), build=_RecordingBuild(worker_model))
    _, scripts = workers.delegation_plugins(
        tiers=tiers, reads=None, spend=spend, state=state, workspace=workspace, checkout=workspace / "checkout", environ=environ
    )

    def parent_function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        returned = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returned:
            return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "agent-scripts"})])
        if len(returned) == 1:
            return ModelResponse(parts=[ToolCallPart(workers.SCRIPT_TOOL, {"code": "r = await quick(task='go')\nr"})])
        return ModelResponse(parts=[TextPart(str(returned[-1].content))])

    return Agent(streaming_function_model(parent_function), deps_type=Runtime, capabilities=[LocalWorkspace(workspace), scripts])


class TestAgentScriptWorkers:
    """A tier worker an agent script starts gets no workspace from its caller, so it holds one of its own."""

    async def test_a_tier_worker_runs_a_command_and_reads_a_checkout_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        (workspace / "checkout" / "notes.txt").write_text("inside-the-checkout", encoding="utf-8")
        (workspace / "sibling").mkdir()
        (workspace / "sibling" / "other.txt").write_text("in-the-sibling", encoding="utf-8")
        worker_model = _calls_then_reports(
            [
                ToolCallPart("run_command", {"command": "pwd; printenv"}),
                ToolCallPart("read_file", {"path": "checkout/notes.txt"}),
                ToolCallPart("read_file", {"path": "sibling/other.txt"}),
            ]
        )
        environ = {**LISTED, "OPENAI_API_KEY": PROVIDER_KEY}

        result = await _scripts_parent(tmp_path, workspace, worker_model, environ=environ).run(
            "script it", deps=a_runtime(state=a_state(tmp_path))
        )

        assert str(workspace.resolve()) in result.output
        assert "USER=listed-user" in result.output
        assert PROVIDER_KEY not in result.output
        assert "inside-the-checkout" in result.output
        assert "in-the-sibling" not in result.output
        assert "does not match any allowed pattern" in result.output


class TestTierWorkersOfTheChatAgent:
    def test_a_tier_worker_takes_its_command_environment_from_the_environment_the_session_was_built_with(self, tmp_path: Path) -> None:
        environ = {"TMPDIR": "/tmp/from-the-mapping", "USER": "mapping-user"}  # noqa: S108 (inert strings the test passes in, never opened)
        state = a_state(tmp_path)
        tiers = ModelTiers(state=state, models=ModelList(listing=None), build=_RecordingBuild(TestModel()))
        agent = _chat_agent(tmp_path, TestModel(), environ=environ, tiers=tiers)

        scripts = next(switched.wrapped for switched in _collect(agent, Switched) if switched.plugin == "agent-scripts")
        assert isinstance(scripts, DynamicWorkflow)
        workspaces = [_collect(worker, LocalWorkspace)[0] for worker in scripts.agents if isinstance(worker, Agent)]

        assert len(workspaces) == len(workers.TIERS)
        assert [workspace.env for workspace in workspaces] == [workspace_env(environ)] * len(workers.TIERS)


class TestWorkerCommandEnvironment:
    async def test_a_delegates_command_finds_the_console_closed(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        worker_model = _calls_then_reports([ToolCallPart("run_command", {"command": "printenv"})])

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert f"{CONSOLE_CLOSED_ENV}=1" in result.output.splitlines()

    async def test_a_tier_workers_command_finds_the_console_closed(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        worker_model = _calls_then_reports([ToolCallPart("run_command", {"command": "printenv"})])

        result = await _scripts_parent(tmp_path, workspace, worker_model, environ=LISTED).run(
            "script it", deps=a_runtime(state=a_state(tmp_path))
        )

        assert f"{CONSOLE_CLOSED_ENV}=1" in result.output
        assert "USER=listed-user" in result.output


class TestStepCommandEnvironment:
    async def test_a_step_command_sees_the_listed_names_and_the_console_marker_and_nothing_else(self, tmp_path: Path) -> None:
        environ = {**LISTED, "OPENAI_API_KEY": PROVIDER_KEY, "LOGFIRE_TOKEN": "tok-secret", "UNLISTED_SETTING": "unlisted-value"}
        model = _calls_then_reports([ToolCallPart("run_command", {"command": "printenv"})])
        agent: Agent[None, str] = Agent(
            model, deps_type=NoneType, capabilities=[step_workspace(writable=True, environ=environ), step_shell()]
        )

        result = await agent.run("look")

        for name, value in LISTED.items():
            assert f"{name}={value}" in result.output
        assert f"{CONSOLE_CLOSED_ENV}=1" in result.output
        for hidden in (PROVIDER_KEY, "tok-secret", "unlisted-value"):
            assert hidden not in result.output


class TestStepSkills:
    async def test_an_agent_step_sees_a_harness_skill_and_an_engram_skill(self) -> None:
        seen: list[tuple[str, list[str]]] = []

        def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
            del messages
            seen.append((info.instructions or "", [tool.name for tool in info.function_tools]))
            return ModelResponse(parts=[TextPart("done")])

        step = AgentStep(
            name="weigh", agent={"instructions": "x", "skills": ["agent-ux", "documenting"]}, output=Shape(json_schema={"type": "string"})
        )
        agent = build_worker_agent(
            step,
            str,
            model=streaming_function_model(function),
            engram=fake_engram(),
            reads=(),
            budget=10,
            promoted=(),
            resolve_agent=lambda name: {},
            web_search=WebSearchSettings(),
        )

        await agent.run("go")

        instructions, tools = seen[-1]
        assert "load_capability" in tools
        assert "agent-ux" in instructions
        assert "documenting" in instructions, "an engram skill lives outside the checkout and must still load"


class TestCheckoutPatterns:
    def test_the_checkout_and_everything_under_it_are_allowed(self, tmp_path: Path) -> None:
        assert workers._checkout_patterns(tmp_path, tmp_path / "checkout") == ["checkout", "checkout/**"]

    def test_a_folder_named_with_glob_characters_matches_only_itself(self, tmp_path: Path) -> None:
        patterns = workers._checkout_patterns(tmp_path, tmp_path / "ck[1]")

        assert patterns is not None
        assert fnmatch.fnmatch("ck[1]/a.txt", patterns[1])
        assert not fnmatch.fnmatch("ck1/a.txt", patterns[1])

    async def test_a_delegate_lists_the_workspace_root_and_sees_its_checkout_only(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        (workspace / "checkout").mkdir(parents=True)
        (workspace / "sibling").mkdir()
        worker_model = _calls_then_reports([ToolCallPart("list_directory", {"path": "."})])

        result = await _parent(tmp_path, workspace, worker_model).run("delegate", deps=a_runtime(state=a_state(tmp_path)))

        assert "checkout" in result.output
        assert "sibling" not in result.output


async def test_a_conversation_from_the_record_continues_in_a_workspace_that_moved(tmp_path: Path) -> None:
    earlier = [
        ModelRequest.user_text_prompt("first"),
        ModelResponse(parts=[TextPart(content="done")], workspace_ref=WorkspaceRef(provider="local", id="/somewhere/else")),
    ]
    agent = Agent(TestModel(), capabilities=[LocalWorkspace(tmp_path)])

    with pytest.raises(UserError, match="continues in workspace"):
        await agent.run("second", message_history=earlier)

    result = await agent.run("second", message_history=messages_of(to_jsonable_python(earlier)))

    assert result.output
