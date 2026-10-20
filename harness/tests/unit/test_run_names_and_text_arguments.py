"""Archived runs and stored schedules read back through the surfaces that serve them.

Every stored shape here is hand-typed JSON rather than a dump of a current model, so a field that
starts being written by default fails loudly here instead of the fixture quietly growing it. A run
nobody named reads as unnamed and never borrows its session's name, and a value stored as text
reaches the step as the JSON its parameter declares.
"""

from __future__ import annotations

import json

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import ArgumentRefusedError, ParameterSpec, normalize_arguments, unsatisfied_parameters

from harness.features.running.domain.ended_run import RunArchiveRecord
from harness.features.running.infrastructure.archive import FileRunArchive
from harness.features.running.records import RunCollaborators
from harness.features.scheduling.domain import Schedule
from harness.features.scheduling.service import Scheduler
from harness.session import SessionPool
from harness.settings import RunSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.running import a_pool, a_running_controller
from tests.support.state import KEPT_DAYS, a_state, records_under
from tests.unit.test_schedule_ticker import FakeWorkflowStore, _runtime, _typed_registry, _typed_spec

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Workflow

# --- one archived run nobody named ---------------------------------------------------------------

_ARCHIVED_RUN_JSON = (
    '{"run_id":"run-unnamed","workflow_id":"wf.quarterly-close","run_title":"","outcome":"completed",'
    '"result":{"status":"unknown","summary":"no result declared"},'
    '"started_at":"2026-01-05T09:00:00+00:00","ended_at":"2026-01-05T09:04:00+00:00",'
    '"runner":"app:harness","session_id":"sess-shared","params":{"quarter":"Q1"},'
    '"spec":{"name":"quarterly-close","version":1},'
    '"values":{"collect_sales":{"total":900}},'
    '"events":[{"seq":1,"node":"collect_sales","at":0.2,"finished":true,"failed":false}],'
    '"gates_approved":["approve_report"]}'
)
"""One archived run its launch gave no name."""

_SESSION_TITLE = "Tuesday morning, catching up on the quarter"
"""What the session this run happened to live in was called. A conversation's name, not a run's,
and nothing on the run side may borrow it."""


def _archived(home: Path, payload: str) -> FileRunArchive:
    """Write `payload` where an ended run rests, and answer an archive over that directory."""
    record = RunArchiveRecord.model_validate_json(payload)
    root = a_state(home).archive
    folder = root / record.workflow_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{record.run_id}.json").write_text(payload, encoding="utf-8")
    return FileRunArchive(root, retention_days=KEPT_DAYS)


def test_a_run_nobody_named_reads_as_unnamed(tmp_path: Path) -> None:
    """Nothing fills in a name. A reader that wants one falls back on its own side of the wire."""
    record = _archived(tmp_path, _ARCHIVED_RUN_JSON).get("run-unnamed")

    assert record is not None
    assert record.run_title == ""
    assert record.session_id == "sess-shared"


async def test_the_served_history_of_an_unnamed_run_carries_no_session_title(tmp_path: Path) -> None:
    """A session is a container with a name of its own. No projection of a run joins that name on."""
    controller = a_running_controller(a_pool(state=tmp_path), tmp_path)
    _archived(tmp_path, _ARCHIVED_RUN_JSON)

    async with a_client_of(controller) as client:
        listed = await client.get("/api/workflows/wf.quarterly-close/history")

    assert listed.status_code == 200
    rows = listed.json()["runs"]
    assert [row["run_title"] for row in rows] == [""]
    assert _SESSION_TITLE not in json.dumps(rows), "no run projection may wear the name of the conversation it ran in"
    assert [row["session_id"] for row in rows] == ["sess-shared"], "which container it ran in is still said, plainly"


# --- a schedule whose arguments are text -----------------------------------------------------------

_TEXT_SCHEDULE_JSON = (
    '{"id":"3f0d6d1e-0c0e-4a6e-9a6a-9b7a1c2d3e4f","workflow_id":"wf-typed",'
    '"arguments":{"knowledge_bases":"[\\"research\\", \\"repo-docs\\"]","include_rejected":"true"},'
    '"cadence":"every 30m","enabled":true,"gate_policy":"wait","gate_mode":"wait",'
    '"session_id":"","created_at":"2026-01-05T09:00:00+00:00",'
    '"last_run_at":null,"last_outcome":"","next_run_at":"2026-08-24T12:00:00+00:00","paused_at":null}'
)
"""One stored schedule from a client that sends nothing but text: an array and a boolean, both
written down as the strings somebody typed."""


async def _fire(tmp_path: Path, row: Schedule, seen: list[tuple[list[str], bool]]) -> Schedule:
    """Fire `row` once through the ticker, let any launched run finish, and answer the stamped row."""
    store = FakeWorkflowStore(specs={"wf-typed": _typed_spec()})
    pool = SessionPool(_runtime(_typed_registry(seen), store))
    schedules = FakeSchedules()
    await schedules.insert(row)

    await Scheduler(schedules, schedules, FakeBroadcaster()).tick(
        pool,
        run=RunCollaborators(records=records_under(tmp_path), topics=FakeBroadcaster(), run_settings=RunSettings()),
        now=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    after = await schedules.get(row.id)
    assert after is not None
    if after.session_id:
        session = await pool.get(after.session_id)
        if session.active.task is not None:
            await session.active.task
    return after


def test_a_schedule_stored_as_text_still_parses() -> None:
    """The row loads as it was written. Nothing is rewritten on the way in."""
    row = Schedule.model_validate_json(_TEXT_SCHEDULE_JSON)

    assert row.arguments == {"knowledge_bases": '["research", "repo-docs"]', "include_rejected": "true"}


async def test_a_text_only_schedule_fires_with_the_types_its_workflow_declares(tmp_path: Path) -> None:
    """A stored string reaches an array parameter as an array, and a boolean one as a boolean."""
    seen: list[tuple[list[str], bool]] = []

    after = await _fire(tmp_path, Schedule.model_validate_json(_TEXT_SCHEDULE_JSON), seen)

    assert after.last_outcome == "launched"
    assert seen == [(["research", "repo-docs"], True)], "never the text of a list, never the four characters 'true'"


async def test_a_text_only_schedule_keeps_its_stored_text_after_firing(tmp_path: Path) -> None:
    """Admission is what a launch is given, never a migration of the row behind it."""
    seen: list[tuple[list[str], bool]] = []

    after = await _fire(tmp_path, Schedule.model_validate_json(_TEXT_SCHEDULE_JSON), seen)

    assert after.arguments == {"knowledge_bases": '["research", "repo-docs"]', "include_rejected": "true"}


async def test_a_stored_python_repr_is_refused_rather_than_read(tmp_path: Path) -> None:
    """Text that is not JSON is a refusal. Nothing guesses what a repr was meant to say."""
    seen: list[tuple[list[str], bool]] = []
    row = Schedule.model_validate_json(_TEXT_SCHEDULE_JSON).model_copy(
        update={"arguments": {"knowledge_bases": "['research', 'repo-docs']", "include_rejected": "True"}}
    )

    after = await _fire(tmp_path, row, seen)

    assert after.last_outcome.startswith("refused: ")
    assert "knowledge_bases" in after.last_outcome
    assert seen == []


# --- the same text, on the run side ----------------------------------------------------------------


def test_a_caller_with_nothing_but_text_reaches_a_typed_parameter() -> None:
    """The one decode pass a non-string schema gets, and nothing more."""
    admitted = normalize_arguments(_typed_spec(), {"knowledge_bases": '["research", "repo-docs"]', "include_rejected": "true"})

    assert admitted == {"knowledge_bases": ["research", "repo-docs"], "include_rejected": True}


def test_text_that_is_not_json_is_refused_naming_the_parameter() -> None:
    """The control on the decode pass: it reads JSON, and refuses everything that is not."""
    with pytest.raises(ArgumentRefusedError) as refusal:
        normalize_arguments(_typed_spec(), {"knowledge_bases": "research, repo-docs", "include_rejected": "true"})

    assert refusal.value.parameter == "knowledge_bases"


# --- an optional parameter nobody answered ---------------------------------------------------------


def _optional(spec: Workflow, **added: ParameterSpec) -> Workflow:
    """`spec` with `added` beside its own parameters, for the cases about what may be left out."""
    return spec.model_copy(update={"parameters": {**spec.parameters, **added}})


def test_an_optional_parameter_left_out_is_not_a_missing_answer() -> None:
    """What a launch is still waiting for names required parameters only."""
    spec = _typed_spec()
    relaxed = spec.model_copy(
        update={
            "parameters": {
                name: (parameter if name != "include_rejected" else parameter.model_copy(update={"required": False}))
                for name, parameter in spec.parameters.items()
            }
        }
    )

    assert unsatisfied_parameters(relaxed, {"knowledge_bases": ["research"]}) == ()


def test_a_required_parameter_left_out_is_still_named() -> None:
    """The control: the same reader says so when the missing parameter is one somebody must answer."""
    assert unsatisfied_parameters(_typed_spec(), {"knowledge_bases": ["research"]}) == ("include_rejected",)


def test_an_optional_string_arrives_as_the_empty_fill_and_nothing_else_does() -> None:
    """An omitted optional string resolves its wire. An omitted optional array stays absent."""
    spec = _optional(
        _typed_spec(),
        note=ParameterSpec(description="anything worth saying", json_schema={"type": "string"}, required=False),
        extras=ParameterSpec(
            description="anything else to read", json_schema={"type": "array", "items": {"type": "string"}}, required=False
        ),
    )

    admitted = normalize_arguments(spec, {"knowledge_bases": ["research"], "include_rejected": True})

    assert admitted["note"] == ""
    assert "extras" not in admitted


def test_an_optional_parameter_that_was_answered_keeps_its_answer() -> None:
    """Optional is about omission alone. A value supplied for one is admitted like any other."""
    spec = _optional(
        _typed_spec(),
        extras=ParameterSpec(
            description="anything else to read", json_schema={"type": "array", "items": {"type": "string"}}, required=False
        ),
    )

    admitted = normalize_arguments(spec, {"knowledge_bases": ["research"], "include_rejected": True, "extras": ["vault"]})

    assert admitted["extras"] == ["vault"]


# --- a run's own name, beside the session it ran in --------------------------------------------------


def test_a_named_run_and_the_session_it_runs_in_are_two_separate_facts(tmp_path: Path) -> None:
    """The archive keeps the name the launch gave the run, and says separately which session held it."""
    named = json.loads(_ARCHIVED_RUN_JSON)
    named["run_id"] = "run-named"
    named["run_title"] = "Which cache do we use?"
    archive = _archived(tmp_path, json.dumps(named))

    record = archive.get("run-named")

    assert record is not None
    assert record.run_title == "Which cache do we use?"
    assert record.session_id == "sess-shared"
    assert record.run_title != _SESSION_TITLE


async def test_both_runs_of_one_session_are_projected_under_their_own_names(tmp_path: Path) -> None:
    """One session can hold several runs, so the container's name could never have named any of them."""
    controller = a_running_controller(a_pool(state=tmp_path), tmp_path)
    for run_id, title in (("run-a", "Which cache do we use?"), ("run-b", "Which queue do we use?")):
        payload = json.loads(_ARCHIVED_RUN_JSON)
        payload["run_id"], payload["run_title"] = run_id, title
        _archived(tmp_path, json.dumps(payload))

    async with a_client_of(controller) as client:
        rows = (await client.get("/api/runs/recent")).json()["runs"]

    assert sorted(row["run_title"] for row in rows) == ["Which cache do we use?", "Which queue do we use?"]
    assert {row["session_id"] for row in rows} == {"sess-shared"}


def test_the_registry_the_schedule_fixtures_run_against_holds_the_step_they_name() -> None:
    """A guard on the fixtures themselves: an empty registry would make the fires pass vacuously."""
    assert "read_bases" in _typed_registry([]).names
