"""What a launch does with the values a caller supplies: admitted as JSON, never restringed.

The fixture is a decision brief -- a question, the knowledge bases to read, the context and the
constraints it works under, plus a boolean, a number, an enum and an object. It is a shape to
validate against, and carries no product vocabulary of its own.

The `/api/run` tests drive the running controller over ASGI, so what a browser posts, what the
step is handed, and what the archive keeps are one round trip rather than three assertions about
three layers.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    ArgumentRefusedError,
    FromNode,
    FromParameter,
    InputPresentation,
    LibraryError,
    ParameterSpec,
    RunNode,
    SavedWorkflow,
    StepRegistry,
    Workflow,
    WorkflowPresentation,
)
from core.ai.workflows.contracts import normalize_arguments as admit
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.adapters.knowledge_base import knowledge_base_names
from harness.domain.arguments import admitted_arguments, presentation_of
from harness.features.running.api_models import RUN_TITLE_MAX
from harness.features.running.records import RunCollaborators
from harness.features.running.tools import run_workflow
from harness.session import SessionPool
from harness.settings import RunSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.sessions import APool
from tests.support.fakes.studio_registry import FakeStudioApi, registry_row, unreachable_studio
from tests.support.running import a_running_controller
from tests.support.runtime import EmptyLibrary, a_runtime
from tests.support.state import a_state, records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from harness.features.running.controller import RunningController
    from harness.session import Runtime

def _ctx(session: Runtime) -> RunContext[Runtime]:
    """One turn's context over `session`, the seam the agent tools are exercised through."""
    return RunContext(deps=session, model=TestModel(), usage=RunUsage())


_KNOWLEDGE_BASES = "knowledge_bases"
"""The array parameter the first-party selector is declared on, named once."""

_BUDGET_SCHEMA = {
    "$ref": "#/$defs/Budget",
    "$defs": {
        "Budget": {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "currency": {"type": "string", "minLength": 1}},
            "required": ["amount", "currency"],
            "additionalProperties": False,
        }
    },
}


class Budget(BaseModel):
    """What a decision may cost, as the step declares it."""

    amount: float
    currency: str


class Brief(BaseModel):
    """What the step answers with, so a run has something to collect."""

    question: str


class Received(BaseModel):
    """What the step was actually handed, kept for the test to read back."""

    model_config = {"arbitrary_types_allowed": False}

    question: str
    knowledge_bases: list[str]
    project_context: str
    constraints: str
    include_rejected: bool
    options_wanted: int
    horizon: str
    budget: Budget


def _brief_spec() -> Workflow:
    """The decision brief: every JSON shape a person is asked for, wired into one step."""
    return Workflow(
        name="decision-brief",
        version=1,
        description="Read the named knowledge bases and brief the person on one decision.",
        parameters={
            "question": ParameterSpec(description="the decision to take", json_schema={"type": "string", "minLength": 1}),
            _KNOWLEDGE_BASES: ParameterSpec(
                description="which knowledge bases to read",
                json_schema={
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                    "maxItems": 3,
                    "uniqueItems": True,
                },
            ),
            "project_context": ParameterSpec(description="what this is about", json_schema={"type": "string"}, required=False),
            "constraints": ParameterSpec(description="what it must respect", json_schema={"type": "string"}, required=False),
            "include_rejected": ParameterSpec(description="keep the options ruled out", json_schema={"type": "boolean"}),
            "options_wanted": ParameterSpec(description="how many options", json_schema={"type": "integer", "minimum": 1, "maximum": 5}),
            "horizon": ParameterSpec(
                description="when it is decided by", json_schema={"enum": ["this week", "this quarter", "this year"]}
            ),
            "budget": ParameterSpec(description="what it may cost", json_schema=_BUDGET_SCHEMA),
        },
        nodes=(
            RunNode(
                id="brief",
                work="write_brief",
                inputs={
                    "question": FromParameter(parameter="question"),
                    _KNOWLEDGE_BASES: FromParameter(parameter=_KNOWLEDGE_BASES),
                    "project_context": FromParameter(parameter="project_context"),
                    "constraints": FromParameter(parameter="constraints"),
                    "include_rejected": FromParameter(parameter="include_rejected"),
                    "options_wanted": FromParameter(parameter="options_wanted"),
                    "horizon": FromParameter(parameter="horizon"),
                    "budget": FromParameter(parameter="budget"),
                },
            ),
        ),
        output=FromNode(node="brief"),
    )


def _registry(seen: list[Received]) -> StepRegistry:
    """A registry whose one step records exactly what the run handed it."""
    registry = StepRegistry()

    async def write_brief(
        question: str,
        knowledge_bases: list[str],
        project_context: str,
        constraints: str,
        include_rejected: bool,
        options_wanted: int,
        horizon: str,
        budget: Budget,
    ) -> Brief:
        seen.append(
            Received(
                question=question,
                knowledge_bases=knowledge_bases,
                project_context=project_context,
                constraints=constraints,
                include_rejected=include_rejected,
                options_wanted=options_wanted,
                horizon=horizon,
                budget=budget,
            )
        )
        return Brief(question=question)

    registry.register(write_brief)
    return registry


_REGISTERED = (registry_row("research", None), registry_row("repo-docs", None), registry_row("vault", None))
"""The registry listing the first-party selector is checked against."""

_CARD = SavedWorkflow(
    id="wf.brief",
    title="Decision brief",
    summary="Read the named knowledge bases and brief the person on one decision.",
    presentation=WorkflowPresentation(
        inputs=(
            InputPresentation(key="question", label="The decision", control="textarea"),
            InputPresentation(key=_KNOWLEDGE_BASES, label="Knowledge bases", control="knowledge-base"),
        )
    ),
)
"""The saved card: the selector is declared here, on the author's words, never in the spec the
hash is taken over."""


def _library(spec: Workflow | None = None, card: SavedWorkflow | None = _CARD) -> FakeWorkflowStore:
    """A library holding the decision brief and, unless a test says otherwise, its card."""
    return FakeWorkflowStore(
        specs={"wf.brief": spec if spec is not None else _brief_spec()}, cards={} if card is None else {"wf.brief": card}
    )


def _controller(
    registry: StepRegistry, home: Path, studio: FakeStudioApi | None = None, library: FakeWorkflowStore | None = None
) -> tuple[RunningController, SessionPool]:
    """The running controller over a pool holding the decision brief, and that pool."""
    default = a_runtime(registry=registry, library=library if library is not None else _library(), state=a_state(home))
    pool = SessionPool(default)
    answering = studio if studio is not None else FakeStudioApi(rows=list(_REGISTERED))
    return a_running_controller(pool, home, knowledge_bases=knowledge_base_names(answering)), pool


async def _client(registry: StepRegistry, home: Path) -> AsyncIterator[tuple[httpx.AsyncClient, SessionPool]]:
    """A client over the running controller alone, and the pool its launches land in."""
    controller, pool = _controller(registry, home)
    async with a_client_of(controller) as client:
        yield client, pool


_GOOD = {
    "question": "Which cache?",
    _KNOWLEDGE_BASES: ["research", "repo-docs"],
    "project_context": "payments",
    "constraints": "offline",
    "include_rejected": True,
    "options_wanted": 3,
    "horizon": "this quarter",
    "budget": {"amount": 1200.5, "currency": "EUR"},
}
"""Every declared parameter, each as the JSON value the person actually chose."""


async def _finished(pool: SessionPool, session_id: str) -> str:
    """Let the launched run finish, and answer the id it was recorded under."""
    session = await pool.get(session_id)
    assert session.active.task is not None
    await session.active.task
    return session.active.run_id or ""


async def test_a_launch_hands_the_step_the_values_it_was_posted(tmp_path: Path) -> None:
    seen: list[Received] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        started = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD})

        assert started.status_code == 200, started.text
        run_id = await _finished(pool, started.json()["session_id"])

        assert len(seen) == 1
        got = seen[0]
        assert got.question == "Which cache?"
        assert got.knowledge_bases == ["research", "repo-docs"]
        assert got.include_rejected is True
        assert got.options_wanted == 3
        assert got.budget == Budget(amount=1200.5, currency="EUR")

        record = records_under(tmp_path).archive.get(run_id)
        assert record is not None
        assert record.params == _GOOD, "the archive answers the same JSON the launch was posted"


@pytest.mark.parametrize(
    ("name", "bad", "expected"),
    [
        ("a blank question", {"question": ""}, "question"),
        ("four knowledge bases", {_KNOWLEDGE_BASES: ["research", "repo-docs", "vault", "extra"]}, _KNOWLEDGE_BASES),
        ("a blank item", {_KNOWLEDGE_BASES: ["research", ""]}, _KNOWLEDGE_BASES),
        ("a duplicate", {_KNOWLEDGE_BASES: ["research", "research"]}, _KNOWLEDGE_BASES),
        ("a python repr", {_KNOWLEDGE_BASES: "['research']"}, _KNOWLEDGE_BASES),
        ("an unregistered knowledge base", {_KNOWLEDGE_BASES: ["research", "nowhere"]}, _KNOWLEDGE_BASES),
        ("a value outside the enum", {"horizon": "next decade"}, "horizon"),
        ("a malformed object", {"budget": {"amount": 10}}, "budget"),
        ("a boolean where an integer belongs", {"options_wanted": True}, "options_wanted"),
        ("a number where a boolean belongs", {"include_rejected": 1}, "include_rejected"),
    ],
)
async def test_a_refused_argument_is_a_four_hundred_before_anything_runs(
    name: str, bad: dict[str, object], expected: str, tmp_path: Path
) -> None:
    del name
    seen: list[Received] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        refused = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": {**_GOOD, **bad}})

        assert refused.status_code == 400, refused.text
        assert expected in refused.json()["message"]
        assert seen == [], "nothing ran"
        assert pool.default.active.task is None, "no run was started in any session this test holds"


async def test_a_refusal_names_the_path_inside_the_value(tmp_path: Path) -> None:
    seen: list[Received] = []
    async for client, _ in _client(_registry(seen), tmp_path):
        refused = await client.post(
            "/api/run", json={"workflow_id": "wf.brief", "arguments": {**_GOOD, _KNOWLEDGE_BASES: ["research", ""]}}
        )

        assert refused.status_code == 400
        assert "$[1]" in refused.json()["message"], "a person reading this has to know which item"


async def test_a_caller_sending_every_value_as_text_still_launches(tmp_path: Path) -> None:
    """The shape a form sends: strings, decoded once."""
    seen: list[Received] = []
    as_text = {
        "question": "Which cache?",
        _KNOWLEDGE_BASES: '["research", "repo-docs"]',
        "project_context": "payments",
        "constraints": "offline",
        "include_rejected": "true",
        "options_wanted": "3",
        "horizon": "this quarter",
        "budget": '{"amount": 1200.5, "currency": "EUR"}',
    }
    async for client, pool in _client(_registry(seen), tmp_path):
        started = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": as_text})

        assert started.status_code == 200, started.text
        await _finished(pool, started.json()["session_id"])

        assert seen[0].knowledge_bases == ["research", "repo-docs"], "never the text of a list"
        assert seen[0].include_rejected is True
        assert seen[0].options_wanted == 3


def test_a_string_schema_keeps_a_string_that_looks_like_something_else() -> None:
    admitted = admit(_brief_spec(), {**_GOOD, "question": "true"})

    assert admitted["question"] == "true"


def test_an_omitted_optional_string_is_filled_and_nothing_else_is() -> None:
    spec = Workflow(
        name="optionals",
        version=1,
        description="One required string, one optional string, one optional integer.",
        parameters={
            "collection": ParameterSpec(description="the base", json_schema={"type": "string", "minLength": 1}),
            "picked_sources": ParameterSpec(description="ids", json_schema={"type": "string"}, required=False),
            "window": ParameterSpec(description="hours", json_schema={"type": "integer"}, required=False),
        },
        nodes=(),
        output=FromParameter(parameter="collection"),
    )

    admitted = admit(spec, {"collection": "research"})

    assert admitted == {"collection": "research", "picked_sources": ""}


def test_an_empty_string_is_never_read_as_an_omission() -> None:
    with pytest.raises(ArgumentRefusedError):
        admit(_brief_spec(), {**_GOOD, "question": ""})


def test_the_caller_dictionary_is_left_alone() -> None:
    given = dict(_GOOD)

    admitted = admit(_brief_spec(), given)

    assert admitted is not given
    assert given == _GOOD


async def test_nothing_is_launched_while_the_registry_cannot_be_read(tmp_path: Path) -> None:
    """A studio that cannot answer refuses the launch rather than admitting an unchecked name."""
    seen: list[Received] = []
    controller, _ = _controller(_registry(seen), tmp_path, unreachable_studio())
    async with a_client_of(controller) as client:
        refused = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD})

    assert refused.status_code == 400
    assert "registry of knowledge bases" in refused.json()["message"]
    assert seen == []


async def test_a_card_that_declares_no_control_is_never_membership_checked(tmp_path: Path) -> None:
    """The selector is the author's declaration. A card carrying none admits whatever the schema does."""
    seen: list[Received] = []
    controller, pool = _controller(_registry(seen), tmp_path, library=_library(card=None))
    async with a_client_of(controller) as client:
        started = await client.post(
            "/api/run", json={"workflow_id": "wf.brief", "arguments": {**_GOOD, _KNOWLEDGE_BASES: ["nowhere"]}}
        )

    assert started.status_code == 200, started.text
    await _finished(pool, started.json()["session_id"])
    assert seen[0].knowledge_bases == ["nowhere"]


async def test_an_omitted_optional_selector_parameter_is_admitted(tmp_path: Path) -> None:
    """An optional selector left out arrives as the empty fill, which is an omission and not a name."""
    spec = Workflow(
        name="optional-selector",
        version=1,
        description="One optional string parameter the card offers a knowledge-base selector for.",
        parameters={
            "collection": ParameterSpec(description="which knowledge base", json_schema={"type": "string"}, required=False),
        },
        nodes=(),
        output=FromParameter(parameter="collection"),
    )
    card = SavedWorkflow(
        id="wf.brief",
        title="Optional selector",
        summary="An optional selector.",
        presentation=WorkflowPresentation(inputs=(InputPresentation(key="collection", label="Knowledge base", control="knowledge-base"),)),
    )

    admitted = await admitted_arguments(spec, {}, presentation=card.presentation, names=knowledge_base_names(FakeStudioApi(rows=list(_REGISTERED))))

    assert admitted == {"collection": ""}, "the compatible fill, not a knowledge base nobody named"


async def test_a_draft_run_is_not_membership_checked(tmp_path: Path) -> None:
    """A draft has no card, so nothing declares a selector and the step's own read refuses a bad name."""
    seen: list[Received] = []
    session = a_runtime(registry=_registry(seen), library=_library(), state=a_state(tmp_path))
    session.conversation.last_workflow = _brief_spec()
    pool = SessionPool(session)
    run = RunCollaborators(
        records=records_under(tmp_path),
        topics=FakeBroadcaster(),
        run_settings=RunSettings(),
        sessions=APool(pool),
        knowledge_bases=knowledge_base_names(FakeStudioApi(rows=list(_REGISTERED))),
    )

    answer = await run_workflow(_ctx(session), "draft", {**_GOOD, _KNOWLEDGE_BASES: ["nowhere"]}, run=run)

    assert "draft" in answer
    assert session.active.task is not None
    await session.active.task
    assert seen[0].knowledge_bases == ["nowhere"], "the draft is admitted against its schema alone"


async def test_a_parameter_pointing_at_a_definition_it_never_carried_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    """A hand-edited library page must answer a refusal, never the validator's own internals."""
    dangling = ParameterSpec.model_construct(description="points at nothing", json_schema={"$ref": "#/$defs/Missing", "$defs": {}})
    spec = Workflow.model_construct(
        name="dangling",
        version=1,
        description="A parameter pointing at a definition it never carried.",
        parameters={"budget": dangling},
        nodes=(),
        output=FromParameter(parameter="budget"),
    )
    seen: list[Received] = []
    controller, _ = _controller(_registry(seen), tmp_path, library=_library(spec=spec, card=None))
    async with a_client_of(controller) as client:
        refused = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": {"budget": {"amount": 1}}})

    assert refused.status_code == 400, refused.text
    assert seen == []


async def test_a_card_that_cannot_be_read_refuses_the_launch(tmp_path: Path) -> None:
    """The spec came from the same library, so a card read that fails is an error, not an absent card."""
    seen: list[Received] = []
    library = _library()
    library.card_error = LibraryError("the library is not answering")
    controller, pool = _controller(_registry(seen), tmp_path, library=library)
    async with a_client_of(controller) as client:
        refused = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD})

    assert refused.status_code == 400, refused.text
    assert "wf.brief" in refused.json()["message"]
    assert seen == []
    assert pool.default.active.task is None, "nothing was started"


async def test_the_agent_is_told_when_a_card_cannot_be_read(tmp_path: Path) -> None:
    seen: list[Received] = []
    library = _library()
    library.card_error = LibraryError("the library is not answering")
    session = a_runtime(registry=_registry(seen), library=library, state=a_state(tmp_path))
    pool = SessionPool(session)
    run = RunCollaborators(
        records=records_under(tmp_path),
        topics=FakeBroadcaster(),
        run_settings=RunSettings(),
        sessions=APool(pool),
        knowledge_bases=knowledge_base_names(FakeStudioApi(rows=list(_REGISTERED))),
    )

    with pytest.raises(ModelRetry, match=re.escape("wf.brief")):
        await run_workflow(_ctx(session), "wf.brief", _GOOD, run=run)

    assert seen == []
    assert session.active.task is None


async def test_a_library_that_serves_no_cards_at_all_still_launches(tmp_path: Path) -> None:
    """A library predating `fetch_card` declares no selector, which is absence rather than failure."""
    seen: list[Received] = []
    session = a_runtime(registry=_registry(seen), library=EmptyLibrary(), state=a_state(tmp_path))
    session.conversation.last_workflow = _brief_spec()
    pool = SessionPool(session)

    admitted = await admitted_arguments(
        _brief_spec(), _GOOD, presentation=await presentation_of(session.library, "wf.brief"), names=None
    )

    assert admitted == _GOOD
    assert pool.default.active.task is None


# --- what the run itself is called ------------------------------------------------------------


async def test_a_launch_names_the_run_and_every_reader_of_it_keeps_that_name(tmp_path: Path) -> None:
    """The title travels with the launch: the live fleet row wears it, and the archive keeps it."""
    seen: list[Received] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        started = await client.post(
            "/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD, "run_title": "Which cache do we use?"}
        )

        assert started.status_code == 200, started.text
        session_id = started.json()["session_id"]
        session = await pool.get(session_id)
        assert session.active.run_title == "Which cache do we use?"

        run_id = await _finished(pool, session_id)
        record = records_under(tmp_path).archive.get(run_id)
        assert record is not None
        assert record.run_title == "Which cache do we use?"
        assert record.session_id == session_id, "the run is named apart from the session it was given"


async def test_a_run_name_past_the_bound_is_refused_and_nothing_is_started(tmp_path: Path) -> None:
    """A name is written into the run log and every projection of it, so it is bounded like any other."""
    seen: list[Received] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        started = await client.post(
            "/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD, "run_title": "x" * (RUN_TITLE_MAX + 1)}
        )

        assert started.status_code == 422
        assert seen == [], "a refused request starts nothing"
        assert pool.default.active.run_title == ""


async def test_a_run_name_at_the_bound_still_launches(tmp_path: Path) -> None:
    """The control: the bound is a ceiling, not a margin somebody has to stay clear of."""
    seen: list[Received] = []
    name = "x" * RUN_TITLE_MAX
    async for client, pool in _client(_registry(seen), tmp_path):
        started = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD, "run_title": name})

        assert started.status_code == 200, started.text
        session = await pool.get(started.json()["session_id"])
        assert session.active.run_title == name


async def test_a_launch_that_names_no_run_leaves_the_record_untitled(tmp_path: Path) -> None:
    seen: list[Received] = []
    async for client, pool in _client(_registry(seen), tmp_path):
        started = await client.post("/api/run", json={"workflow_id": "wf.brief", "arguments": _GOOD})

        assert started.status_code == 200, started.text
        run_id = await _finished(pool, started.json()["session_id"])

        record = records_under(tmp_path).archive.get(run_id)
        assert record is not None
        assert record.run_title == ""
