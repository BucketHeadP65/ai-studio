"""`GET /api/spend`: the report as the dashboard reads it, and a 400 that names the fix.

Each case asks the route for its answer directly, with a real `SpendReporter` over a fake telemetry:
what is under test is what the route folds and refuses, and the reporter is the one collaborator
that would otherwise reach a network.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.settings import InfrastructureSettings, LogfireSettings
from fastapi.testclient import TestClient

from harness.adapters.logfire_query import QueryColumn, QueryResult, Telemetry, TelemetryUnreachable
from harness.features.running.spend_report import SpendReporter
from tests.support.container import AN_INFRASTRUCTURE, IO_LEAVES, an_app, answering_from_memory
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.running import a_pool, a_running_controller

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import StepRegistry


def _one_line() -> QueryResult:
    return QueryResult(
        columns=[
            QueryColumn(name="key", datatype="Utf8", values=["run-1"]),
            QueryColumn(name="calls", datatype="Int64", values=[3]),
            QueryColumn(name="cost_usd", datatype="Float64", values=[0.5]),
            QueryColumn(name="input_tokens", datatype="Int64", values=[10]),
            QueryColumn(name="output_tokens", datatype="Int64", values=[2]),
            QueryColumn(name="cache_read_tokens", datatype="Int64", values=[0]),
            QueryColumn(name="cache_write_tokens", datatype="Int64", values=[0]),
        ]
    )


async def _get(
    registry: StepRegistry, state: Path, telemetry: FakeTelemetry, *, scope: str, subject: str = "", since_hours: int = 24
) -> tuple[int, dict[str, object]]:
    """The route's own answer, its reporter reading from `telemetry`."""
    controller = a_running_controller(a_pool(registry), state)
    response = await controller.spend(scope=scope, reporter=SpendReporter(telemetry), subject=subject, since_hours=since_hours)
    body = json.loads(bytes(response.body))
    assert isinstance(body, dict)
    return response.status_code, body


async def test_a_report_rides_as_json_with_decimals_as_strings(registry: StepRegistry, tmp_path: Path) -> None:
    status, body = await _get(registry, tmp_path, FakeTelemetry(result=_one_line()), scope="run", subject="run-1", since_hours=48)

    assert status == 200
    assert body["available"] is True
    assert body["scope"] == "run"
    assert body["since_hours"] == 48
    assert body["total"] == {
        "key": "total", "calls": 3, "cost_usd": "0.5", "input_tokens": 10, "output_tokens": 2,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    lines = body["lines"]
    assert isinstance(lines, list)
    first = lines[0]
    assert isinstance(first, dict)
    assert first["key"] == "run-1"


async def test_unreachable_telemetry_is_available_false_with_the_reason(registry: StepRegistry, tmp_path: Path) -> None:
    unreachable = FakeTelemetry(failure=TelemetryUnreachable("telemetry refused the query (HTTP 403)"))

    status, body = await _get(registry, tmp_path, unreachable, scope="day")

    assert status == 200
    assert body["available"] is False
    assert body["reason"] == "telemetry refused the query (HTTP 403)"
    fallback = body["fallback"]
    assert isinstance(fallback, str)
    assert fallback.startswith("The spend log on disk")


async def test_a_bad_request_is_a_400_naming_the_fix(registry: StepRegistry, tmp_path: Path) -> None:
    status, body = await _get(registry, tmp_path, FakeTelemetry(), scope="run", subject="")
    assert status == 400
    assert str(body["detail"]).startswith("run needs an id")

    status, body = await _get(registry, tmp_path, FakeTelemetry(), scope="hour")
    assert status == 400
    assert body["detail"] == "No scope 'hour', the scopes are day, model, run, session, step, workflow."


def test_the_spend_reporter_resolves_through_the_container(tmp_path: Path) -> None:
    """`/api/spend`'s `Injected[SpendReporter]` is the container's own registration over `Telemetry`, not a stand-in built by hand."""
    telemetry = FakeTelemetry(result=_one_line())
    built = an_app(state=tmp_path)
    with (
        answering_from_memory(built.state.wireup_container),
        built.state.wireup_container.override({Telemetry: telemetry}),
        TestClient(built, base_url="http://localhost") as client,
    ):
        response = client.get("/api/spend", params={"scope": "run", "subject": "run-1", "since_hours": 48})

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["total"]["calls"] == 3
    assert telemetry.asked, "the route must have queried the fake this test installed through the container"


def test_without_a_logfire_read_token_the_app_starts_and_spend_says_logfire_is_off(tmp_path: Path) -> None:
    """No read token on file is a deployment without Logfire: the app boots, and a spend report says Logfire is off."""
    leaves: dict[type, object] = {key: value for key, value in IO_LEAVES if key is not Telemetry}
    leaves[InfrastructureSettings] = AN_INFRASTRUCTURE.model_copy(update={"logfire": LogfireSettings(base_url="https://telemetry.example")})
    built = an_app(state=tmp_path)
    with built.state.wireup_container.override(leaves), TestClient(built, base_url="http://localhost") as client:
        response = client.get("/api/spend", params={"scope": "day"})

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "Logfire is off" in body["reason"]
    assert "infrastructure.logfire.read_token" in body["reason"]
