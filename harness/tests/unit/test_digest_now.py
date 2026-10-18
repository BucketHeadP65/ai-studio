"""`digest_now`: land a source, sync and index, then start the current digest workflow.

`_FakeStore` mirrors `test_schedule_ticker.py`'s `FakeWorkflowStore`, with a working `locate` --
the one verb that fake steps stubbed, and the one `_latest_workflow_id` walks. HTTP goes through
`httpx.MockTransport`, never real network: a handler answers by exact path, and a path this test
did not prepare raises loudly, so an unexpected call (a source-queue POST when none was expected, say)
fails the test instead of silently succeeding against nothing.
"""

from __future__ import annotations

import asyncio

from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx
import pytest

from core.ai.workflows.contracts import FromNode, FromParameter, LibraryError, ParameterSpec, RunNode, Workflow, WorkflowArguments
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from harness.features.authoring.digest_now import _latest_workflow_id, _resolve_and_launch, digest_now_tool
from harness.features.authoring.infrastructure.studio import StudioKnowledgeBaseRefresher
from harness.features.running import service as running_service
from harness.session import ActiveRun, NoSessionLog, Runtime
from harness.settings import RunSettings
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.state import a_throwaway_state, records_under

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import Lineage, SavedWorkflow, StepRegistry, WorkflowFingerprint, WorkflowPresentation

    from harness.features.authoring.internal_contracts import LaunchWorkflow
    from harness.features.running.records import RunRecords


@dataclass
class _FakeStore:
    """`WorkflowLibrary`'s minimal double for `digest_now`: `locate` and `fetch_workflow` are the only verbs it calls."""

    specs: dict[str, Workflow] = field(default_factory=dict)

    async def search(self, task: str, *, limit: int = 5) -> Sequence[SavedWorkflow]:
        del task, limit
        return ()

    async def fetch_workflow(self, workflow_id: str) -> Workflow:
        try:
            return self.specs[workflow_id]
        except KeyError:
            msg = f"no workflow {workflow_id!r} here"
            raise LibraryError(msg) from None

    async def put(self, workflow: Workflow, **_kwargs: object) -> str:
        del workflow
        msg = "_FakeStore does not save"
        raise NotImplementedError(msg)

    async def redescribe(
        self, workflow_id: str, *, title: str, summary: str, applicability: str, presentation: WorkflowPresentation | None = None
    ) -> None:
        del workflow_id, title, summary, applicability, presentation
        msg = "_FakeStore does not redescribe"
        raise NotImplementedError(msg)

    async def set_archived(self, workflow_id: str, *, archived: bool) -> None:
        del workflow_id, archived
        msg = "_FakeStore does not archive"
        raise NotImplementedError(msg)

    async def archived(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return ()

    async def fetch_fingerprint(self, workflow_id: str) -> WorkflowFingerprint | None:
        del workflow_id
        return None

    async def fetch_lineage(self, workflow_id: str) -> Lineage | None:
        del workflow_id
        return None

    async def fetch_card(self, workflow_id: str) -> SavedWorkflow | None:
        del workflow_id
        return None

    async def catalog(self, *, limit: int = 50) -> Sequence[SavedWorkflow]:
        del limit
        return ()

    async def catalog_size(self) -> int:
        return len(self.specs)

    async def locate(self, name: str, version: int) -> str | None:
        workflow_id = f"wf.{name}.v{version}"
        return workflow_id if workflow_id in self.specs else None

    async def current(self, workflow_id: str) -> str:
        """The real library's rule: the newest live version of the name, or the id itself."""
        name = workflow_id.removeprefix("wf.").rsplit(".v", 1)[0]
        versions = sorted(int(wid.rsplit(".v", 1)[1]) for wid in self.specs if wid.startswith(f"wf.{name}.v"))
        return f"wf.{name}.v{versions[-1]}" if versions else workflow_id


@dataclass
class _RecordingLaunch:
    """A `LaunchWorkflow` stand-in: records every call, starts nothing for real."""

    calls: list[dict[str, object]] = field(default_factory=list)

    async def __call__(self, session: Runtime, *, spec: Workflow, workflow_id: str, params: WorkflowArguments, runner: str) -> None:
        self.calls.append({"session": session, "spec": spec, "workflow_id": workflow_id, "params": params, "runner": runner})


_DIGEST = "research-digest"
"""The digest workflow name the research schema in these tests gives."""

_SCHEMA_PATH = "/api/knowledge-bases/research/schema"


def _schema_naming(name: str) -> httpx.Response:
    return httpx.Response(200, json={"sources": {"digest_workflow": name}})


def _digest_spec(*, version: int = 1, work: str = "fetch_sales", name: str = _DIGEST) -> Workflow:
    """A `research-digest`-shaped spec over the shared `registry` fixture's own `fetch_sales` step.

    `fetch_sales(quarter: str)` is what `tests/conftest.py` registers; this workflow's own
    parameter is named `collection` (the one `digest_now` fills in), wired to the step's `quarter`
    input -- a workflow parameter's name and a step's own argument name are never the same thing.
    """
    return Workflow(
        name=name,
        version=version,
        description="stand-in for the real research-digest workflow",
        parameters={"collection": ParameterSpec(description="the knowledge base", json_schema={"type": "string"})},
        nodes=(RunNode(id=work, work=work, inputs={"quarter": FromParameter(parameter="collection")}),),
        output=FromNode(node=work),
    )


def _runtime(registry: StepRegistry, store: _FakeStore, *, session_id: str = "sess-1") -> Runtime:
    return Runtime(registry=registry, library=store, agent=cast("Any", None), active=ActiveRun(), session_id=session_id, session_log=NoSessionLog(), state=a_throwaway_state())


def _ctx(session: Runtime) -> RunContext[Runtime]:
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


def _transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Route a request by its exact path; a path nothing prepared for is a test bug, not a 404."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path not in responses:
            msg = f"no fake response prepared for {request.method} {path}"
            raise AssertionError(msg)
        return responses[path]

    return httpx.MockTransport(handle)


def _unreachable_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    return httpx.MockTransport(handle)


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://studio.test")


# --- _latest_workflow_id ------------------------------------------------------------------------


async def test_latest_workflow_id_answers_the_newest_version() -> None:
    store = _FakeStore(specs={"wf.research-digest.v1": _digest_spec(version=1), "wf.research-digest.v2": _digest_spec(version=2)})
    assert await _latest_workflow_id(store, _DIGEST) == "wf.research-digest.v2"


async def test_latest_workflow_id_survives_a_gap_in_the_version_numbering() -> None:
    """The shipped library has no v8, and the old version walk stopped there and launched v7 (the agent-filed friction)."""
    store = _FakeStore(specs={"wf.research-digest.v1": _digest_spec(version=1), "wf.research-digest.v3": _digest_spec(version=3)})
    assert await _latest_workflow_id(store, _DIGEST) == "wf.research-digest.v3"


async def test_latest_workflow_id_stops_at_v1_when_no_v2_exists() -> None:
    store = _FakeStore(specs={"wf.research-digest.v1": _digest_spec(version=1)})
    assert await _latest_workflow_id(store, _DIGEST) == "wf.research-digest.v1"


async def test_latest_workflow_id_is_none_when_the_store_holds_no_version() -> None:
    assert await _latest_workflow_id(_FakeStore(), _DIGEST) is None


# --- StudioKnowledgeBaseRefresher.queue_source ---------------------------------------------------


async def test_queue_source_succeeds_quietly() -> None:
    client = _client(_transport({"/api/knowledge-bases/research/source-queue": httpx.Response(200, json={"key": "k1", "status": "requested"})}))
    assert await StudioKnowledgeBaseRefresher(client).queue_source("research", "https://example.test/paper") is None


async def test_queue_source_answers_studios_error_detail() -> None:
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/source-queue": httpx.Response(
                    400, json={"error": "sources_without_queue", "detail": "knowledge base declares no source queue"}
                )
            }
        )
    )
    refusal = await StudioKnowledgeBaseRefresher(client).queue_source("research", "https://example.test/paper")
    assert refusal is not None
    assert "knowledge base declares no source queue" in refusal
    assert "https://example.test/paper" in refusal


async def test_queue_source_answers_when_studio_is_unreachable() -> None:
    refusal = await StudioKnowledgeBaseRefresher(_client(_unreachable_transport())).queue_source("research", "https://example.test/paper")
    assert refusal is not None
    assert "could not reach studio" in refusal


# --- StudioKnowledgeBaseRefresher.sync_and_index -----------------------------------------------
# Studio enqueues the refresh and answers `202 {"job_id": N}`; the worker runs it. These fake that
# contract: the enqueue names a job, and `/api/jobs/{id}` walks it to a terminal state.


def _job(state: str, *, last_error: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"id": 7, "state": state, "last_error": last_error})


async def test_sync_and_index_waits_for_the_enqueued_job_to_land_done() -> None:
    states = iter(["queued", "running", "done"])
    responses = {
        "/api/knowledge-bases/research/refresh": lambda: httpx.Response(202, json={"job_id": 7}),
        "/api/jobs/7": lambda: _job(next(states)),
    }
    client = _client(httpx.MockTransport(lambda request: responses[request.url.path]()))
    assert await StudioKnowledgeBaseRefresher(client, poll_seconds=0).sync_and_index("research") is None


async def test_sync_and_index_answers_a_failed_job_with_its_error() -> None:
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("failed", last_error="schema preflight refused"),
            }
        )
    )
    refusal = await StudioKnowledgeBaseRefresher(client, poll_seconds=0).sync_and_index("research")
    assert refusal is not None
    assert "schema preflight refused" in refusal


async def test_sync_and_index_answers_an_abandoned_job_asking_for_a_person() -> None:
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("abandoned", last_error="embedding credentials expired"),
            }
        )
    )
    refusal = await StudioKnowledgeBaseRefresher(client, poll_seconds=0).sync_and_index("research")
    assert refusal is not None
    assert "embedding credentials expired" in refusal
    assert "needs a person" in refusal


async def test_sync_and_index_watches_the_job_a_conflict_names() -> None:
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(
                    409, json={"error": "refresh_in_progress", "detail": "a refresh run for 'research' is already underway.", "job_id": 7}
                ),
                "/api/jobs/7": _job("done"),
            }
        )
    )
    assert await StudioKnowledgeBaseRefresher(client, poll_seconds=0).sync_and_index("research") is None


async def test_sync_and_index_hands_a_long_running_job_back_to_the_caller() -> None:
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("running"),
            }
        )
    )
    refusal = await StudioKnowledgeBaseRefresher(client, poll_seconds=0, timeout_seconds=0).sync_and_index("research")
    assert refusal is not None
    assert "still running" in refusal
    assert "check_runs" in refusal


async def test_sync_and_index_answers_studios_error_detail_on_a_bad_status() -> None:
    client = _client(
        _transport({"/api/knowledge-bases/research/refresh": httpx.Response(404, json={"error": "unknown_knowledge_base", "detail": "no knowledge base named 'research'"})})
    )
    refusal = await StudioKnowledgeBaseRefresher(client).sync_and_index("research")
    assert refusal is not None
    assert "no knowledge base named 'research'" in refusal


async def test_sync_and_index_answers_when_studio_is_unreachable() -> None:
    refusal = await StudioKnowledgeBaseRefresher(_client(_unreachable_transport())).sync_and_index("research")
    assert refusal is not None
    assert "could not reach studio" in refusal


async def test_sync_and_index_answers_on_a_malformed_body() -> None:
    client = _client(_transport({"/api/knowledge-bases/research/refresh": httpx.Response(200, content=b"not json")}))
    refusal = await StudioKnowledgeBaseRefresher(client).sync_and_index("research")
    assert refusal is not None


# --- _resolve_and_launch --------------------------------------------------------------------------


async def test_resolve_and_launch_answers_when_no_digest_workflow_is_registered(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore())
    launch = _RecordingLaunch()

    answer = await _resolve_and_launch(session, "research", _DIGEST, launch)

    assert f"no {_DIGEST!r} workflow is registered here" in answer
    assert launch.calls == []


async def test_resolve_and_launch_answers_when_the_spec_no_longer_checks_out(registry: StepRegistry) -> None:
    ghost = _digest_spec(work="a_step_nobody_registered")
    session = _runtime(registry, _FakeStore(specs={"wf.research-digest.v1": ghost}))
    launch = _RecordingLaunch()

    answer = await _resolve_and_launch(session, "research", _DIGEST, launch)

    assert "does not check out here" in answer
    assert launch.calls == []


async def test_resolve_and_launch_launches_the_newest_version_with_the_collection_argument(registry: StepRegistry) -> None:
    store = _FakeStore(specs={"wf.research-digest.v1": _digest_spec(version=1), "wf.research-digest.v2": _digest_spec(version=2)})
    session = _runtime(registry, store)
    launch = _RecordingLaunch()

    answer = await _resolve_and_launch(session, "research", _DIGEST, launch)

    assert len(launch.calls) == 1
    call = launch.calls[0]
    assert call["workflow_id"] == "wf.research-digest.v2"
    assert call["params"] == {"collection": "research"}
    assert call["runner"] == "app:chat:digest-now"
    assert "wf.research-digest.v2" in answer
    assert "/workflows/wf.research-digest.v2" in answer


# --- digest_now_tool, the whole closure ------------------------------------------------------------


async def test_digest_now_refuses_a_second_run_while_one_is_live(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore())
    session.active.workflow_id = "wf.other.v1"
    session.active.task = asyncio.ensure_future(asyncio.sleep(10))
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(_client(_transport({}))), launch=_RecordingLaunch())
    try:
        answer = await tool(_ctx(session), "research", None)
    finally:
        session.active.task.cancel()
        with suppress(asyncio.CancelledError):
            await session.active.task

    assert "wf.other.v1" in answer
    assert "still running" in answer


async def test_digest_now_refuses_while_the_sessions_run_is_paused(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore())
    session.active.workflow_id = "wf.other.v1"
    session.active.paused_by_restart = True
    launch = _RecordingLaunch()
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(_client(_transport({}))), launch=launch)

    with pytest.raises(ModelRetry, match="paused"):
        await tool(_ctx(session), "research", None)

    assert launch.calls == []


def _launching(records: RunRecords) -> LaunchWorkflow:
    """`running_service.start` with this test's own record homes already bound."""

    async def launch(session: Runtime, *, spec: Workflow, workflow_id: str, params: WorkflowArguments, runner: str) -> None:
        await running_service.start(
            session,
            records=records,
            topics=FakeBroadcaster(),
            run=RunSettings(),
            spec=spec,
            workflow_id=workflow_id,
            params=params,
            runner=runner,
        )

    return launch


async def test_digest_now_without_a_url_skips_the_source_queue_call(registry: StepRegistry, tmp_path: Path) -> None:
    session = _runtime(registry, _FakeStore(specs={"wf.research-digest.v1": _digest_spec()}))
    client = _client(
        _transport(
            {"/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}), "/api/jobs/7": _job("done"), _SCHEMA_PATH: _schema_naming(_DIGEST)}
        )
    )
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=_launching(records_under(tmp_path)))

    answer = await tool(_ctx(session), "research", None)

    assert "Started wf.research-digest.v1" in answer
    assert session.active.task is not None
    await session.active.task
    assert "Finished" in session.active.result


async def test_digest_now_queues_the_url_syncs_and_launches_end_to_end(registry: StepRegistry, tmp_path: Path) -> None:
    session = _runtime(registry, _FakeStore(specs={"wf.research-digest.v1": _digest_spec()}))
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/source-queue": httpx.Response(200, json={"key": "k1", "status": "requested"}),
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("done"),
                _SCHEMA_PATH: _schema_naming(_DIGEST),
            }
        )
    )
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=_launching(records_under(tmp_path)))

    answer = await tool(_ctx(session), "research", "https://example.test/paper")

    assert "Started wf.research-digest.v1 over 'research'" in answer
    assert "/workflows/wf.research-digest.v1" in answer
    assert session.active.task is not None
    await session.active.task
    assert "Finished" in session.active.result


async def test_digest_now_stops_at_a_failed_sync_and_never_launches(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore(specs={"wf.research-digest.v1": _digest_spec()}))
    client = _client(
        _transport({"/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}), "/api/jobs/7": _job("failed", last_error="boom")})
    )
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=_RecordingLaunch())

    answer = await tool(_ctx(session), "research", None)

    assert "boom" in answer
    assert session.active.task is None


async def test_digest_now_runs_the_workflow_the_knowledge_base_names(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore(specs={"wf.team-digest.v1": _digest_spec(name="team-digest")}))
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("done"),
                "/api/knowledge-bases/research/schema": httpx.Response(200, json={"sources": {"digest_workflow": "team-digest"}}),
            }
        )
    )
    launch = _RecordingLaunch()
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=launch)

    answer = await tool(_ctx(session), "research", None)

    assert [call["workflow_id"] for call in launch.calls] == ["wf.team-digest.v1"]
    assert "Started wf.team-digest.v1" in answer


async def test_digest_now_refuses_a_knowledge_base_that_names_none(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore(specs={"wf.research-digest.v1": _digest_spec()}))
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("done"),
                "/api/knowledge-bases/research/schema": httpx.Response(200, json={"sources": {}}),
            }
        )
    )
    launch = _RecordingLaunch()
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=launch)

    answer = await tool(_ctx(session), "research", None)

    assert "'research'" in answer
    assert "sources.digest_workflow" in answer
    assert launch.calls == []


async def test_digest_now_answers_when_studio_cannot_serve_the_schema(registry: StepRegistry) -> None:
    session = _runtime(registry, _FakeStore(specs={"wf.research-digest.v1": _digest_spec()}))
    client = _client(
        _transport(
            {
                "/api/knowledge-bases/research/refresh": httpx.Response(202, json={"job_id": 7}),
                "/api/jobs/7": _job("done"),
                _SCHEMA_PATH: httpx.Response(404, json={"detail": "no knowledge base 'research'"}),
            }
        )
    )
    launch = _RecordingLaunch()
    tool = digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=launch)

    answer = await tool(_ctx(session), "research", None)

    assert "could not read 'research''s schema" in answer
    assert launch.calls == []


async def test_the_tool_mounts_as_a_function_toolset() -> None:
    """Constructing the toolset builds the tool's schema, so a type-only annotation fails here, not at boot."""
    from pydantic_ai.toolsets import FunctionToolset

    async def launch(session: object, *, spec: object, workflow_id: str, params: WorkflowArguments, runner: str) -> None:
        del session, spec, workflow_id, params, runner

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)), base_url="http://studio.test")
    try:
        toolset = FunctionToolset([digest_now_tool(StudioKnowledgeBaseRefresher(client), launch=launch)])
        assert "digest_now" in {tool.name for tool in toolset.tools.values()}
    finally:
        await client.aclose()
