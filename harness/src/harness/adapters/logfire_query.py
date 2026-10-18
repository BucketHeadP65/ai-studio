"""Logfire's SQL query API as a read-only client, the one place this app reads its own telemetry back."""

from __future__ import annotations

from typing import NoReturn, Protocol

import httpx

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

QUERY_TIMEOUT_SECONDS = 30.0
"""Seconds one query may take before the telemetry counts as unreachable."""

type Cell = str | int | float | bool | None
"""One value of one column, as the wire carries it."""


class QueryColumn(BaseModel):
    """One column of an answer: its name, its type, and every row's value in order."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    datatype: str
    values: list[Cell]


class QueryResult(BaseModel):
    """An answer to one SQL query, column-major as the wire carries it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    columns: list[QueryColumn]

    @model_validator(mode="after")
    def _same_height(self) -> QueryResult:
        if len({len(column.values) for column in self.columns}) > 1:
            msg = "columns of one answer must have the same number of rows"
            raise ValueError(msg)
        return self

    def rows(self) -> list[dict[str, Cell]]:
        """The same answer row by row.

        Returns:
            One mapping per row, keyed by column name, in row order.
        """
        names = [column.name for column in self.columns]
        return [dict(zip(names, cells, strict=True)) for cells in zip(*(column.values for column in self.columns), strict=True)]


class TelemetryUnreachable(Exception):
    """The telemetry could not answer: offline, refused, rate limited, or an answer in a shape this app does not read."""


class Telemetry(Protocol):
    """The part of a telemetry backend a report needs: run one SQL query, get its columns back."""

    async def query(self, sql: str) -> QueryResult:
        """Run one SQL query.

        Args:
            sql: The query.

        Returns:
            The answer's columns.

        Raises:
            TelemetryUnreachable: The backend is offline, refused or rate limited the query, or answered in a shape this
                app does not read.
        """
        ...


LOGFIRE_OFF = "Logfire is off: the settings file carries no infrastructure.logfire.read_token, so spend cannot be queried"
"""What a telemetry read answers on a deployment with no Logfire read token."""


class LogfireOff:
    """`Telemetry` for a deployment with no Logfire read token: every query answers that Logfire is off."""

    async def query(self, sql: str) -> NoReturn:
        """Refuse `sql` without sending it, because no read token is on file.

        Args:
            sql: The query, never sent.

        Raises:
            TelemetryUnreachable: Always, with `LOGFIRE_OFF` as its message.
        """
        del sql
        raise TelemetryUnreachable(LOGFIRE_OFF)


class LogfireQuery:
    """`Telemetry` over Logfire's `GET /v1/query`, authenticated with a read token.

    Each query opens its own HTTP client, so nothing stays open between reports.
    """

    def __init__(self, *, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Hold the address and the token every query is sent with.

        Args:
            base_url: Logfire's API address.
            token: A Logfire read token.
            transport: The HTTP transport requests go through. `None` sends them over the network.
        """
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._transport = transport

    async def query(self, sql: str) -> QueryResult:
        """Send `sql` to `GET /v1/query` and validate the answer.

        Args:
            sql: The query.

        Returns:
            The answer's columns.

        Raises:
            TelemetryUnreachable: Logfire could not be reached, rate limited or refused the query, or answered in a shape
                this app does not read.
        """
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, headers=self._headers, timeout=httpx.Timeout(QUERY_TIMEOUT_SECONDS), transport=self._transport
            ) as client:
                response = await client.get("/v1/query", params={"sql": sql})
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            msg = f"telemetry unreachable ({type(error).__name__})"
            raise TelemetryUnreachable(msg) from error
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            msg = "telemetry rate limited, ask again in a minute"
            raise TelemetryUnreachable(msg)
        if response.is_error:
            msg = f"telemetry refused the query (HTTP {response.status_code})"
            raise TelemetryUnreachable(msg)
        try:
            return QueryResult.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            msg = "telemetry answered in a shape this app does not read"
            raise TelemetryUnreachable(msg) from error

