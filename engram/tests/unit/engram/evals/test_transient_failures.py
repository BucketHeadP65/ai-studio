"""The transient-failure rule: which failed cases a resumed evaluation run runs again."""

from __future__ import annotations

import asyncpg
import httpx2
import pytest

from asyncpg.exceptions import (
    AdminShutdownError,
    CannotConnectNowError,
    ConnectionDoesNotExistError,
    InsufficientPrivilegeError,
    PostgresError,
    PostgresSyntaxError,
    ProtocolViolationError,
    QueryCanceledError,
    SyntaxOrAccessError,
    UndefinedColumnError,
)
from openai import AsyncOpenAI
from pydantic_ai.exceptions import ContentFilterError, ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError

from engram.evals.harness.transient_failures import (
    TRANSIENT_MODEL_MESSAGES,
    TRANSIENT_STATUS_CODES,
    is_transient_error_text,
    is_transient_failure,
)


def test_the_rule_names_429_and_every_server_error() -> None:
    assert frozenset({429, *range(500, 600)}) == TRANSIENT_STATUS_CODES


def test_the_rule_names_the_connection_error_and_the_request_timeout() -> None:
    assert frozenset({"Connection error.", "Request timed out."}) == TRANSIENT_MODEL_MESSAGES


_ASYNCPG_DBAPI = AsyncAdapt_asyncpg_dbapi(asyncpg)


def _from_statement(driver_error: PostgresError) -> DBAPIError:
    """What SQLAlchemy raises when an asyncpg statement fails with `driver_error`: a syntax or access error as `ProgrammingError`, others as `Error`."""
    adapted_type = _ASYNCPG_DBAPI.ProgrammingError if isinstance(driver_error, SyntaxOrAccessError) else _ASYNCPG_DBAPI.Error
    adapted = adapted_type(f"{type(driver_error)}: {driver_error}")
    adapted.__cause__ = driver_error
    wrapped = DBAPIError.instance("SELECT * FROM api.search(query => $1)", ("regional influence",), adapted, _ASYNCPG_DBAPI.Error)
    wrapped.__cause__ = adapted
    assert isinstance(wrapped, DBAPIError)
    return wrapped


_TRANSIENT: list[BaseException] = [
    ModelAPIError("deployment-a", "Connection error."),
    ModelAPIError("deployment-a", "Request timed out."),
    ModelHTTPError(429, "deployment-a", {"error": "too many requests"}),
    ModelHTTPError(500, "deployment-a"),
    ModelHTTPError(503, "deployment-a"),
    ModelHTTPError(599, "deployment-a"),
]

_KEPT: list[BaseException] = [
    UsageLimitExceeded("The next tool call(s) would exceed the tool_calls_limit of 2 (tool_calls=3)."),
    ContentFilterError("The model refused to answer."),
    UnexpectedModelBehavior("Exceeded maximum retries (1) for output validation"),
    ModelHTTPError(400, "deployment-a", {"error": "bad request"}),
    ModelHTTPError(404, "deployment-a"),
    ModelAPIError("deployment-a", "server_error: the response failed"),
    RuntimeError("Connection error."),
    TimeoutError("Request timed out."),
]


_DATABASE_TRANSIENT: list[BaseException] = [
    ConnectionRefusedError(61, "Connect call failed ('127.0.0.1', 5434)"),
    ConnectionResetError(54, "Connection reset by peer"),
    OSError("Multiple exceptions: [Errno 61] Connect call failed ('::1', 5434, 0, 0), [Errno 61] Connect call failed ('127.0.0.1', 5434)"),
    TimeoutError(60, "Connect call failed ('10.0.0.5', 5434)"),
    OSError(65, "Connect call failed ('10.0.0.5', 5434)"),
    CannotConnectNowError("the database system is shutting down"),
    ConnectionDoesNotExistError("connection was closed in the middle of operation"),
    _from_statement(ConnectionDoesNotExistError("connection was closed in the middle of operation")),
    _from_statement(AdminShutdownError("terminating connection due to administrator command")),
]

_DATABASE_KEPT: list[BaseException] = [
    _from_statement(PostgresSyntaxError('syntax error at or near "SELEC"')),
    _from_statement(UndefinedColumnError('column "titel" does not exist')),
    _from_statement(InsufficientPrivilegeError("permission denied for table pages")),
    _from_statement(QueryCanceledError("canceling statement due to statement timeout")),
    _from_statement(ProtocolViolationError("bind message supplies 1 parameters, but prepared statement requires 2")),
    PostgresSyntaxError('syntax error at or near "SELEC"'),
    QueryCanceledError("canceling statement due to statement timeout"),
    FileNotFoundError(2, "No such file or directory"),
    OSError("Multiple exceptions: [Errno 61] Connect call failed ('::1', 5434, 0, 0), [Errno 2] No such file or directory"),
]


@pytest.mark.parametrize("error", _DATABASE_TRANSIENT, ids=repr)
def test_a_refused_failed_or_closed_database_connection_is_transient(error: BaseException) -> None:
    assert is_transient_failure(error) is True


@pytest.mark.parametrize("error", _DATABASE_KEPT, ids=repr)
def test_an_error_the_agent_s_own_statement_causes_is_not_transient(error: BaseException) -> None:
    assert is_transient_failure(error) is False


@pytest.mark.parametrize("error", _TRANSIENT, ids=repr)
def test_a_model_connection_error_timeout_429_or_5xx_is_transient(error: BaseException) -> None:
    assert is_transient_failure(error) is True


@pytest.mark.parametrize("error", _KEPT, ids=repr)
def test_a_tool_call_limit_a_refusal_and_every_other_error_is_not_transient(error: BaseException) -> None:
    assert is_transient_failure(error) is False


@pytest.mark.parametrize("error", [*_TRANSIENT, *_KEPT, *_DATABASE_TRANSIENT, *_DATABASE_KEPT], ids=repr)
def test_the_error_text_a_case_records_reads_the_same_way_as_the_error(error: BaseException) -> None:
    recorded = f"{type(error).__name__}: {error}"

    assert is_transient_error_text(recorded) is is_transient_failure(error)


@pytest.mark.parametrize(
    ("text", "transient"),
    [
        ("ModelAPIError: Connection error.", True),
        ("ModelAPIError: Request timed out.", True),
        ("ModelHTTPError: status_code: 429, model_name: deployment-a, body: None", True),
        ("ModelHTTPError: status_code: 502, model_name: deployment-a, body: None", True),
        ("ModelHTTPError: status_code: 401, model_name: deployment-a, body: None", False),
        ("UsageLimitExceeded: The next tool call(s) would exceed the tool_calls_limit of 2 (tool_calls=3).", False),
        ("RuntimeError: Connection error.", False),
        ("ModelAPIError: Connection error. Retry later.", False),
        ("ConnectionRefusedError: [Errno 61] Connect call failed ('127.0.0.1', 5434)", True),
        ("OSError: [Errno 28] No space left on device", False),
        ("OSError: Multiple exceptions: [Errno 61] Connect call failed ('::1', 5434, 0, 0), [Errno 2] No such file or directory", False),
        (
            (
                "ProgrammingError: (sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) <class 'asyncpg.exceptions.PostgresSyntaxError'>: "
                'syntax error at or near "SELEC"'
            ),
            False,
        ),
        (
            (
                "DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) <class 'asyncpg.exceptions.QueryCanceledError'>: "
                "canceling statement due to statement timeout"
            ),
            False,
        ),
        ("", False),
        (None, False),
    ],
)
def test_an_older_line_reads_its_error_text(text: str | None, transient: bool) -> None:
    assert is_transient_error_text(text) is transient


_OUTAGE_TEXTS = [
    "OSError: Multiple exceptions: [Errno 61] Connect call failed ('::1', 5434, 0, 0), [Errno 61] Connect call failed ('127.0.0.1', 5434)",
    (
        "DBAPIError: (sqlalchemy.dialects.postgresql.asyncpg.Error) <class 'asyncpg.exceptions.ConnectionDoesNotExistError'>: connection was "
        "closed in the middle of operation\n[SQL: SELECT * FROM api.search(query => $1)]\n[parameters: ('regional influence',)]\n"
        "(Background on this error at: https://sqlalche.me/e/20/dbapi)"
    ),
    "CannotConnectNowError: the database system is shutting down",
]


@pytest.mark.parametrize("text", _OUTAGE_TEXTS, ids=["refused on every address", "closed mid-statement", "shutting down"])
def test_the_error_texts_a_database_outage_leaves_are_transient(text: str) -> None:
    assert is_transient_error_text(text) is True


def test_a_statement_cut_off_by_a_closed_connection_records_the_outage_text() -> None:
    error = _from_statement(ConnectionDoesNotExistError("connection was closed in the middle of operation"))

    assert f"{type(error).__name__}: {error}" == _OUTAGE_TEXTS[1]


class _FailingTransport(httpx2.AsyncBaseTransport):
    """Answers every request with one scripted network error or HTTP status."""

    def __init__(self, *, error: Exception | None = None, status: int = 200) -> None:
        self.error = error
        self.status = status

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if self.error is not None:
            raise self.error
        return httpx2.Response(self.status, json={"error": {"message": "scripted", "type": "scripted"}}, request=request)


async def _model_failure(transport: _FailingTransport) -> BaseException:
    """The error a Responses model raises when its request meets `transport`."""
    client = AsyncOpenAI(api_key="key", base_url="https://deployment.invalid/v1/", http_client=httpx2.AsyncClient(transport=transport), max_retries=0)
    model = OpenAIResponsesModel("deployment-a", provider=OpenAIProvider(openai_client=client))
    # The request reaches only the scripted transport, never the network.
    with override_allow_model_requests(allow_model_requests=True), pytest.raises(Exception) as raised:  # noqa: PT011 (the test classifies whatever the model raises)
        await model.request([ModelRequest(parts=[UserPromptPart("Ask.")])], None, ModelRequestParameters())
    return raised.value


@pytest.mark.parametrize(
    ("transport", "transient"),
    [
        (_FailingTransport(error=httpx2.ConnectError("refused")), True),
        (_FailingTransport(error=httpx2.ReadTimeout("slow")), True),
        (_FailingTransport(status=429), True),
        (_FailingTransport(status=503), True),
        (_FailingTransport(status=400), False),
    ],
    ids=["connection error", "request timeout", "429", "503", "400"],
)
async def test_the_rule_reads_what_the_model_client_raises(transport: _FailingTransport, transient: bool) -> None:
    error = await _model_failure(transport)

    assert is_transient_failure(error) is transient
    assert is_transient_error_text(f"{type(error).__name__}: {error}") is transient
