"""The schedules HTTP surface: CRUD, a cadence preview, and firing one by hand.

Reuses `test_schedule_ticker.py`'s fakes -- `FakeSchedules` is the reference `Schedules`
behavior, `FakeWorkflowStore` is `WorkflowLibrary`'s minimal double -- rather than a second copy
of either. `SchedulingController` is constructed directly and driven over HTTP through
`tests.support.controllers.a_client_of`, the same seam any controller's own route test uses.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import ParameterSpec, StepRegistry
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from harness.features.running.records import RunCollaborators
from harness.features.running.spend import RunSpend, SpendTotals, rows_for
from harness.features.scheduling.controller import SchedulingController
from harness.features.scheduling.service import Scheduler
from harness.session import NoSessionLog, Runtime, SessionPool
from harness.settings import RunSettings
from tests.support.controllers import a_client_of
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.spend import FakeSpendLog
from tests.support.state import a_state, records_under
from tests.unit.test_run_spend import _response
from tests.unit.test_schedule_ticker import FakeWorkflowStore, _one_node_spec, _schedule, _typed_registry, _typed_spec

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    import httpx

    from pydantic_ai.messages import ModelResponse


def _run(run_id: str, workflow_id: str, runner: str, started_at: datetime, responses: list[ModelResponse]) -> RunSpend:
    """One folded run, its cost already summed the way a ledger's own read hands it back."""
    totals = SpendTotals()
    for row in rows_for("fetch_sales", responses):
        totals = totals.plus(row)
    return RunSpend(run_id=run_id, workflow_id=workflow_id, runner=runner, session_id="s", started_at=started_at.isoformat(), totals=totals, steps={})


@asynccontextmanager
async def _studio(
    store: FakeWorkflowStore,
    schedules: FakeSchedules,
    home: Path,
    *,
    registry: StepRegistry | None = None,
    spend: FakeSpendLog | None = None,
) -> AsyncGenerator[tuple[httpx.AsyncClient, SessionPool]]:
    """A client over `SchedulingController` alone and the pool it launches into, with a schedule store and a spend ledger."""
    runtime = Runtime(
        registry=registry if registry is not None else StepRegistry(),
        library=store,
        agent=Agent(TestModel(), deps_type=Runtime),
        session_log=NoSessionLog(),
        state=a_state(home),
    )
    pool = SessionPool(runtime)
    written = records_under(home)
    records = written if spend is None else replace(written, spend=spend)
    topics = FakeBroadcaster()
    controller = SchedulingController(
        scheduler=Scheduler(schedules, schedules, topics),
        sessions=pool,
        run=RunCollaborators(records=records, topics=topics, run_settings=RunSettings(), sessions=APool(pool)),
    )
    async with a_client_of(controller) as client:
        yield client, pool


async def test_create_lists_and_reads_back_a_schedule(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    async with _studio(store, FakeSchedules(), tmp_path, registry=registry) as (client, _):
        created = await client.post(
            "/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "every 30m"}
        )

        assert created.status_code == 201
        body = created.json()["schedule"]
        assert body["workflow_id"] == "wf-sales"
        assert body["cadence"] == "every 30m"
        assert body["arguments"] == {"quarter": "Q1"}
        assert body["next_run_at"] is not None, "a fresh schedule is due-scannable the moment it exists"
        assert body["live"] is None

        listed = await client.get("/api/schedules")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()["schedules"]] == [body["id"]]


async def test_the_listing_names_the_newest_live_version_a_schedule_will_fire(registry: StepRegistry, tmp_path: Path) -> None:
    """A row written against v1 lists as v2 once v2 is the newest live version: the id shown is the one that runs."""
    store = FakeWorkflowStore(
        specs={"wf.sales.v1": _one_node_spec("sales"), "wf.sales.v2": _one_node_spec("sales")},
        newest={"wf.sales.v1": "wf.sales.v2"},
    )
    async with _studio(store, FakeSchedules(), tmp_path, registry=registry) as (client, _):
        created = await client.post(
            "/api/schedules", json={"workflow_id": "wf.sales.v1", "arguments": {"quarter": "Q1"}, "cadence": "every 30m"}
        )
        assert created.status_code == 201
        assert created.json()["schedule"]["workflow_id"] == "wf.sales.v2"

        listed = (await client.get("/api/schedules")).json()["schedules"]
        assert [row["workflow_id"] for row in listed] == ["wf.sales.v2"]


async def test_a_create_naming_only_workflow_arguments_and_cadence_defaults_every_other_setting(registry: StepRegistry, tmp_path: Path) -> None:
    """A create naming only the workflow, its arguments and its cadence gets every other setting's default."""
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), FakeSchedules(), tmp_path, registry=registry) as (client, _):
        created = await client.post("/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "every 30m"})

        assert created.status_code == 201
        body = created.json()["schedule"]
        assert body["capacity_key"] is None
        assert body["capacity_limit"] is None
        assert body["blackout"] is None
        assert body["catch_up"] is False


async def test_create_with_the_new_fields_round_trips_them(registry: StepRegistry, tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), FakeSchedules(), tmp_path, registry=registry) as (client, _):
        created = await client.post(
            "/api/schedules",
            json={
                "workflow_id": "wf-sales",
                "arguments": {"quarter": "Q1"},
                "cadence": "every 30m",
                "capacity_key": "pool-a",
                "capacity_limit": 2,
                "blackout": "22:00-06:00",
                "catch_up": True,
            },
        )

        assert created.status_code == 201
        body = created.json()["schedule"]
        assert body["capacity_key"] == "pool-a"
        assert body["capacity_limit"] == 2
        assert body["blackout"] == "22:00-06:00"
        assert body["catch_up"] is True


async def test_create_refuses_a_blackout_that_is_not_one(registry: StepRegistry, tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), FakeSchedules(), tmp_path, registry=registry) as (client, _):
        refused = await client.post("/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "every 30m", "blackout": "whenever"})

        assert refused.status_code == 400
        assert "blackout looks like" in refused.json()["message"]


async def test_patch_updates_the_new_fields(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        patched = await client.patch(
            f"/api/schedules/{schedule.id}",
            json={"capacity_key": "pool-b", "capacity_limit": 3, "blackout": "01:00-02:00", "catch_up": True},
        )

        assert patched.status_code == 200
        body = patched.json()["schedule"]
        assert body["capacity_key"] == "pool-b"
        assert body["capacity_limit"] == 3
        assert body["blackout"] == "01:00-02:00"
        assert body["catch_up"] is True


async def test_patch_refuses_a_blackout_that_is_not_one(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        refused = await client.patch(f"/api/schedules/{schedule.id}", json={"blackout": "whenever"})

        assert refused.status_code == 400
        assert "blackout looks like" in refused.json()["message"]


async def test_create_refuses_a_workflow_that_does_not_check_out(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        refused = await client.post("/api/schedules", json={"workflow_id": "no-such-workflow", "cadence": "every 30m"})

        assert refused.status_code == 404


async def test_create_refuses_a_cadence_that_is_not_one(registry: StepRegistry, tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), FakeSchedules(), tmp_path, registry=registry) as (client, _):
        refused = await client.post("/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "whenever"})

        assert refused.status_code == 400
        assert "cadence looks like" in refused.json()["message"]


async def test_patch_updates_only_the_given_fields(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        patched = await client.patch(f"/api/schedules/{schedule.id}", json={"enabled": False, "cadence": "every 2h"})

        assert patched.status_code == 200
        body = patched.json()["schedule"]
        assert body["enabled"] is False
        assert body["cadence"] == "every 2h"
        assert body["workflow_id"] == "wf-sales", "an untouched field survives a partial update"


async def test_patch_flipping_enabled_off_stamps_paused_at_and_back_on_clears_it(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        paused = await client.patch(f"/api/schedules/{schedule.id}", json={"enabled": False})
        assert paused.status_code == 200
        assert paused.json()["schedule"]["paused_at"] is not None

        resumed = await client.patch(f"/api/schedules/{schedule.id}", json={"enabled": True})
        assert resumed.status_code == 200
        assert resumed.json()["schedule"]["paused_at"] is None


async def test_patch_of_an_unknown_schedule_is_a_404(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        missing = await client.patch(f"/api/schedules/{_schedule().id}", json={"enabled": False})

        assert missing.status_code == 404


async def test_delete_removes_the_row(tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(), schedules, tmp_path) as (client, _):
        deleted = await client.delete(f"/api/schedules/{schedule.id}")

        assert deleted.status_code == 200
        assert deleted.json() == {"ok": True}
        assert await schedules.get(schedule.id) is None


async def test_delete_of_an_unknown_schedule_is_a_404(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        missing = await client.delete(f"/api/schedules/{_schedule().id}")

        assert missing.status_code == 404


async def test_run_now_launches_immediately_into_the_schedules_runner_session(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule(next_run_at=None)
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, pool):
        fired = await client.post(f"/api/schedules/{schedule.id}/run-now")

        assert fired.status_code == 200
        assert fired.json() == {"ok": True, "outcome": "launched"}
        after = await schedules.get(schedule.id)
        assert after is not None
        session = await pool.get(after.session_id)
        task = session.active.task
        assert task is not None
        await task


async def test_run_now_of_an_unknown_schedule_is_a_404(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        missing = await client.post(f"/api/schedules/{_schedule().id}/run-now")

        assert missing.status_code == 404


async def test_run_now_lets_a_key_error_from_inside_the_fire_escape_rather_than_answer_404(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules(stamp_error=KeyError("inside the fire"))
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, pool):
        runner = await pool.create(None, session_kind="runner")
        schedule = _schedule(next_run_at=None, session_id=runner.session_id)
        await schedules.insert(schedule)

        with pytest.raises(KeyError) as raised:
            await client.post(f"/api/schedules/{schedule.id}/run-now")

        assert raised.type is KeyError
        task = runner.active.task
        assert task is not None
        await task


async def test_a_schedule_reports_what_its_own_fires_cost_today_and_this_week(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    runner = f"app:schedule:{schedule.id}"
    now = datetime.now(UTC)
    today_run = _run("today", "wf-sales", runner, now, [_response()])
    one_response_cost = today_run.totals.cost_usd
    ledger = FakeSpendLog(
        runs=[
            today_run,
            _run("last-week", "wf-sales", runner, now - timedelta(days=5), [_response()]),
            _run("button", "wf-sales", "app:harness", now, [_response()]),
        ]
    )

    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry, spend=ledger) as (client, _):
        listed = (await client.get("/api/schedules")).json()["schedules"]

    row = next(item for item in listed if item["id"] == str(schedule.id))
    assert Decimal(row["spend_today_usd"]) == one_response_cost
    assert Decimal(row["spend_week_usd"]) == one_response_cost * 2, "the button run is not the schedule's"


async def test_preview_answers_the_next_three_occurrences(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        previewed = await client.post("/api/schedules/preview", json={"cadence": "every 1h"})

        assert previewed.status_code == 200
        occurrences = [datetime.fromisoformat(stamp) for stamp in previewed.json()["next"]]
        assert len(occurrences) == 3
        assert occurrences[1] - occurrences[0] == timedelta(hours=1)
        assert occurrences[2] - occurrences[0] == timedelta(hours=2)
        assert all(stamp > datetime.now(UTC) for stamp in occurrences)


async def test_preview_refuses_a_cadence_that_is_not_one_naming_the_shape(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        refused = await client.post("/api/schedules/preview", json={"cadence": "whenever"})

        assert refused.status_code == 400
        assert "cadence looks like" in refused.json()["message"]


async def test_a_fire_whose_header_lost_its_timestamp_is_left_out_rather_than_breaking_the_listing(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    runner = f"app:schedule:{schedule.id}"
    now = datetime.now(UTC)
    datable = _run("datable", "wf-sales", runner, now, [_response()])
    undatable = _run("undatable", "wf-sales", runner, now, [_response()]).model_copy(update={"started_at": "whenever"})
    ledger = FakeSpendLog(runs=[datable, undatable])

    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry, spend=ledger) as (client, _):
        listed = (await client.get("/api/schedules")).json()["schedules"]

    row = next(item for item in listed if item["id"] == str(schedule.id))
    assert Decimal(row["spend_today_usd"]) == datable.totals.cost_usd, "a fire nothing can date belongs to no window"
    assert Decimal(row["spend_week_usd"]) == datable.totals.cost_usd


async def test_create_keeps_every_argument_as_the_json_it_was_posted_as(tmp_path: Path) -> None:
    """An array stays an array and a boolean stays a boolean, through create and through the listing."""
    store = FakeWorkflowStore(specs={"wf-typed": _typed_spec()})
    typed = {"knowledge_bases": ["research", "repo-docs"], "include_rejected": True}
    async with _studio(store, FakeSchedules(), tmp_path, registry=_typed_registry([])) as (client, _):
        created = await client.post("/api/schedules", json={"workflow_id": "wf-typed", "arguments": typed, "cadence": "every 30m"})

        assert created.status_code == 201, created.text
        assert created.json()["schedule"]["arguments"] == typed

        listed = (await client.get("/api/schedules")).json()["schedules"]
        assert listed[0]["arguments"] == typed


async def test_create_refuses_an_argument_the_workflow_schema_will_not_admit(tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-typed": _typed_spec()})
    async with _studio(store, FakeSchedules(), tmp_path, registry=_typed_registry([])) as (client, _):
        refused = await client.post(
            "/api/schedules",
            json={"workflow_id": "wf-typed", "arguments": {"knowledge_bases": ["a", "b", "c", "d"], "include_rejected": True}, "cadence": "every 30m"},
        )

        assert refused.status_code == 400, refused.text
        assert "knowledge_bases" in refused.json()["message"]


async def test_patching_arguments_refuses_what_the_schema_refuses(tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-typed": _typed_spec()})
    typed = {"knowledge_bases": ["research"], "include_rejected": False}
    async with _studio(store, FakeSchedules(), tmp_path, registry=_typed_registry([])) as (client, _):
        created = await client.post("/api/schedules", json={"workflow_id": "wf-typed", "arguments": typed, "cadence": "every 30m"})
        schedule_id = created.json()["schedule"]["id"]

        refused = await client.patch(f"/api/schedules/{schedule_id}", json={"arguments": {"knowledge_bases": ["a", "a"], "include_rejected": False}})

        assert refused.status_code == 400, refused.text
        assert "knowledge_bases" in refused.json()["message"]

        held = (await client.get("/api/schedules")).json()["schedules"][0]
        assert held["arguments"] == typed, "a refused patch changes nothing"


async def test_create_names_every_problem_of_a_workflow_this_app_cannot_run(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), FakeSchedules(), tmp_path) as (client, _):
        refused = await client.post("/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "every 30m"})

        assert refused.status_code == 400
        assert refused.json()["message"].startswith("this app cannot run it. ")


async def test_create_refuses_arguments_that_leave_a_required_parameter_unfilled(registry: StepRegistry, tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), FakeSchedules(), tmp_path, registry=registry) as (client, _):
        refused = await client.post("/api/schedules", json={"workflow_id": "wf-sales", "cadence": "every 30m"})

        assert refused.status_code == 400
        assert refused.json()["message"] == "fill in: quarter"
        assert (await client.get("/api/schedules")).json()["schedules"] == []


async def test_patch_refuses_a_workflow_whose_required_parameter_the_held_arguments_leave_unfilled(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule.model_copy(update={"arguments": {}}))
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        refused = await client.patch(f"/api/schedules/{schedule.id}", json={"workflow_id": "wf-sales"})

        assert refused.status_code == 400
        assert refused.json()["message"] == "fill in: quarter"


async def test_saving_a_schedule_with_no_next_fire_gives_it_one_from_its_cadence(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    failed = _schedule(next_run_at=None, last_outcome="failed, boom")
    await schedules.insert(failed)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        saved = await client.patch(f"/api/schedules/{failed.id}", json={"enabled": True})

    assert saved.status_code == 200, saved.text
    restored = await schedules.get(failed.id)
    assert restored is not None
    assert restored.next_run_at is not None
    assert restored.next_run_at > datetime.now(UTC) - timedelta(minutes=1)


async def test_saving_a_schedule_that_has_a_next_fire_leaves_it(registry: StepRegistry, tmp_path: Path) -> None:
    schedules = FakeSchedules()
    schedule = _schedule()
    await schedules.insert(schedule)
    async with _studio(FakeWorkflowStore(specs={"wf-sales": _one_node_spec()}), schedules, tmp_path, registry=registry) as (client, _):
        await client.patch(f"/api/schedules/{schedule.id}", json={"cadence": "every 1h"})

    kept = await schedules.get(schedule.id)
    assert kept is not None
    assert kept.next_run_at == schedule.next_run_at


async def test_create_with_an_event_cadence_starts_resting(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    async with _studio(store, FakeSchedules(), tmp_path, registry=registry) as (client, _):
        created = await client.post(
            "/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "after nightly-report"}
        )

    assert created.status_code == 201
    assert created.json()["schedule"]["next_run_at"] is None


async def test_patching_a_schedule_to_an_event_cadence_clears_the_next_fire_it_held(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    schedules = FakeSchedules()
    held = _schedule(enabled=False, next_run_at=datetime(2026, 8, 28, 7, 0, tzinfo=UTC))
    await schedules.insert(held)
    async with _studio(store, schedules, tmp_path, registry=registry) as (client, _):
        patched = await client.patch(f"/api/schedules/{held.id}", json={"cadence": "on refresh"})
        enabled = await client.patch(f"/api/schedules/{held.id}", json={"enabled": True})

    assert patched.status_code == 200
    assert patched.json()["schedule"]["next_run_at"] is None
    assert enabled.json()["schedule"]["next_run_at"] is None, "turning it on brings back no fire by the clock"


async def test_patching_a_schedule_to_an_on_index_cadence_clears_the_next_fire_it_held(registry: StepRegistry, tmp_path: Path) -> None:
    sales = _one_node_spec()
    changed_pages = ParameterSpec(description="the changed pages", json_schema={"type": "string"}, required=False)
    taking_pages = sales.model_copy(update={"parameters": {**sales.parameters, "changed_page_ids": changed_pages}})
    store = FakeWorkflowStore(specs={"wf-sales": taking_pages})
    schedules = FakeSchedules()
    held = _schedule(next_run_at=datetime(2026, 8, 28, 7, 0, tzinfo=UTC))
    await schedules.insert(held)
    async with _studio(store, schedules, tmp_path, registry=registry) as (client, _):
        patched = await client.patch(f"/api/schedules/{held.id}", json={"cadence": "on index notes quiet 10m"})

    assert patched.status_code == 200
    assert patched.json()["schedule"]["next_run_at"] is None


async def test_a_schedule_cannot_fire_after_its_own_workflow(registry: StepRegistry, tmp_path: Path) -> None:
    store = FakeWorkflowStore(specs={"wf-sales": _one_node_spec()})
    schedules = FakeSchedules()
    held = _schedule()
    await schedules.insert(held)
    async with _studio(store, schedules, tmp_path, registry=registry) as (client, _):
        created = await client.post(
            "/api/schedules", json={"workflow_id": "wf-sales", "arguments": {"quarter": "Q1"}, "cadence": "after wf-sales"}
        )
        patched = await client.patch(f"/api/schedules/{held.id}", json={"cadence": "after wf-sales"})

    assert created.status_code == 400
    assert "cannot fire after" in created.json()["message"]
    assert patched.status_code == 400
    assert "cannot fire after" in patched.json()["message"]


async def test_preview_of_an_event_cadence_lists_no_fires(tmp_path: Path) -> None:
    async with _studio(FakeWorkflowStore(), FakeSchedules(), tmp_path) as (client, _):
        preview = await client.post("/api/schedules/preview", json={"cadence": "on refresh"})

    assert preview.status_code == 200
    assert preview.json() == {"next": []}
