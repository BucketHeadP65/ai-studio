"""Logfire's query API as this app reads it: columns in, rows out, and every failure said as unreachable."""

from __future__ import annotations

import re

import httpx
import pytest

from harness.adapters.logfire_query import LogfireQuery, TelemetryUnreachable


def _columns(*columns: tuple[str, list[str | int | float | None]]) -> dict[str, list[dict[str, object]]]:
    return {"columns": [{"name": name, "datatype": "Utf8", "nullable": True, "values": values} for name, values in columns]}


def _query(handler: httpx.MockTransport | None = None, **responses: httpx.Response) -> LogfireQuery:
    def answer(request: httpx.Request) -> httpx.Response:
        return responses["response"]

    transport = handler if handler is not None else httpx.MockTransport(answer)
    return LogfireQuery(base_url="https://logfire.test", token="read-token", transport=transport)


async def test_a_query_is_sent_with_the_read_token_and_answers_row_by_row() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_columns(("key", ["a", "b"]), ("calls", [3, 4])))

    query = LogfireQuery(base_url="https://logfire.test", token="read-token", transport=httpx.MockTransport(answer))
    result = await query.query("select 1")

    assert seen[0].url.path == "/v1/query"
    assert seen[0].url.params["sql"] == "select 1"
    assert seen[0].headers["authorization"] == "Bearer read-token"
    assert result.rows() == [{"key": "a", "calls": 3}, {"key": "b", "calls": 4}]


async def test_no_columns_is_no_rows() -> None:
    result = await _query(response=httpx.Response(200, json={"columns": []})).query("select 1")
    assert result.rows() == []


@pytest.mark.parametrize(
    ("status", "said"),
    [(403, "telemetry refused the query (HTTP 403)"), (429, "telemetry rate limited, ask again in a minute")],
)
async def test_a_refusal_is_unreachable_with_its_reason(status: int, said: str) -> None:
    with pytest.raises(TelemetryUnreachable, match=re.escape(said)):
        await _query(response=httpx.Response(status, json={"detail": "no"})).query("select 1")


async def test_a_network_failure_is_unreachable() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        msg = "no route"
        raise httpx.ConnectError(msg, request=request)

    with pytest.raises(TelemetryUnreachable, match=re.escape("telemetry unreachable (ConnectError)")):
        await _query(handler=httpx.MockTransport(answer)).query("select 1")


async def test_a_malformed_base_url_is_unreachable() -> None:
    query = LogfireQuery(
        base_url="https://logfire.test:notaport", token="read-token", transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )

    with pytest.raises(TelemetryUnreachable, match="telemetry unreachable"):
        await query.query("select 1")


_UNEVEN = {"columns": [{"name": "a", "datatype": "Utf8", "values": [1]}, {"name": "b", "datatype": "Utf8", "values": []}]}


@pytest.mark.parametrize("body", [{"rows": []}, _UNEVEN])
async def test_an_answer_in_another_shape_is_unreachable(body: dict[str, object]) -> None:
    with pytest.raises(TelemetryUnreachable, match="telemetry answered in a shape this app does not read"):
        await _query(response=httpx.Response(200, json=body)).query("select 1")
