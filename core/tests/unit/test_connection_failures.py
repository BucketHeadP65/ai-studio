"""Whether an error reports a failed database connection, and which service a failed connection reached."""

from __future__ import annotations

import asyncpg
import httpx2
import pytest

from asyncpg.exceptions import (
    AdminShutdownError,
    CannotConnectNowError,
    ClientCannotConnectError,
    ConnectionDoesNotExistError,
    ConnectionFailureError,
    ConnectionRejectionError,
    CrashShutdownError,
    PostgresError,
    PostgresSyntaxError,
    QueryCanceledError,
    SyntaxOrAccessError,
)
from openai import AsyncOpenAI
from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters, override_allow_model_requests
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError

from core.connection_failures import (
    DATABASE_CONNECTION_ERRORS,
    connection_failure_message,
    is_database_connection_failure,
    is_statement_timeout,
)

DATABASE = "localhost:5434/knowledge_bases"


class _FailingTransport(httpx2.AsyncBaseTransport):
    """Fails every request with one scripted network error, the way a connection that never opened fails."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        raise self.error


def _client(error: Exception) -> AsyncOpenAI:
    transport = _FailingTransport(error)
    return AsyncOpenAI(
        api_key="key", base_url="https://example.openai.azure.com/openai/v1/", http_client=httpx2.AsyncClient(transport=transport), max_retries=0
    )


async def _embedding_failure(error: Exception) -> BaseException:
    """The error an embedding model raises when its request meets `error`."""
    model = OpenAIEmbeddingModel("text-embedding-3-small", provider=OpenAIProvider(openai_client=_client(error)))
    # The request reaches only the scripted transport, never the network.
    with override_allow_model_requests(allow_model_requests=True), pytest.raises(Exception) as raised:  # noqa: PT011 (the test names whatever the model raises)
        await model.embed(["a question"], input_type="query")
    return raised.value


async def _model_failure(error: Exception) -> BaseException:
    """The error a Responses model raises when its request meets `error`."""
    model = OpenAIResponsesModel("deployment-a", provider=OpenAIProvider(openai_client=_client(error)))
    with override_allow_model_requests(allow_model_requests=True), pytest.raises(Exception) as raised:  # noqa: PT011 (the test names whatever the model raises)
        await model.request([ModelRequest(parts=[UserPromptPart("Ask.")])], None, ModelRequestParameters())
    return raised.value


async def test_a_failed_embedding_connection_names_the_embedding_endpoint_and_its_host() -> None:
    error = await _embedding_failure(httpx2.ConnectError("refused"))

    assert connection_failure_message(error, database=DATABASE) == "embedding endpoint example.openai.azure.com: Connection error."


async def test_a_timed_out_embedding_request_names_the_embedding_endpoint() -> None:
    error = await _embedding_failure(httpx2.ReadTimeout("slow"))

    assert connection_failure_message(error, database=DATABASE) == "embedding endpoint example.openai.azure.com: Request timed out."


async def test_a_failed_model_connection_names_the_model_endpoint_and_its_host() -> None:
    error = await _model_failure(httpx2.ConnectError("refused"))

    assert connection_failure_message(error, database=DATABASE) == "model endpoint example.openai.azure.com: Connection error."


def test_a_refused_database_connection_names_the_database() -> None:
    error = ConnectionRefusedError(61, "Connect call failed ('127.0.0.1', 5434)")

    assert connection_failure_message(error, database=DATABASE) == f"database {DATABASE}: [Errno 61] Connect call failed ('127.0.0.1', 5434)"


def test_a_database_connection_closed_under_a_statement_names_the_database_and_keeps_the_text() -> None:
    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)
    driver_error = ConnectionDoesNotExistError("connection was closed in the middle of operation")
    adapted = dbapi.Error(f"{type(driver_error)}: {driver_error}")
    adapted.__cause__ = driver_error
    error = DBAPIError.instance("SELECT 1", (), adapted, dbapi.Error)
    error.__cause__ = adapted

    message = connection_failure_message(error, database=DATABASE)

    assert message == f"database {DATABASE}: {error}"
    assert "connection was closed in the middle of operation" in str(error)


async def test_an_error_raised_from_a_failed_connection_names_that_connection() -> None:
    error = RuntimeError("the query could not be embedded")
    error.__cause__ = await _embedding_failure(httpx2.ConnectError("refused"))

    assert connection_failure_message(error, database=DATABASE) == "embedding endpoint example.openai.azure.com: the query could not be embedded"


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError(2, "No such file or directory"), ValueError("unknown search setup 'dense'"), OSError("disk full")],
    ids=["missing file", "refusal", "other os error"],
)
def test_an_error_that_came_from_no_connection_is_not_named(error: Exception) -> None:
    assert connection_failure_message(error, database=DATABASE) is None


def test_a_database_connection_failure_with_no_text_names_its_type() -> None:
    assert connection_failure_message(ConnectionResetError(), database=DATABASE) == f"database {DATABASE}: ConnectionResetError"


async def test_a_caller_that_opens_no_database_names_only_the_endpoints() -> None:
    endpoint = await _model_failure(httpx2.ConnectError("refused"))

    assert connection_failure_message(ConnectionRefusedError(61, "refused"), database=None) is None
    assert connection_failure_message(endpoint, database=None) == "model endpoint example.openai.azure.com: Connection error."


def test_the_rule_names_the_errors_of_a_refused_failed_or_closed_database_connection() -> None:
    assert (
        ConnectionError,
        BrokenPipeError,
        ConnectionAbortedError,
        ConnectionRefusedError,
        ConnectionResetError,
        ClientCannotConnectError,
        ConnectionDoesNotExistError,
        ConnectionRejectionError,
        ConnectionFailureError,
        AdminShutdownError,
        CrashShutdownError,
        CannotConnectNowError,
    ) == DATABASE_CONNECTION_ERRORS


def _from_statement(driver_error: PostgresError) -> DBAPIError:
    """What SQLAlchemy raises when an asyncpg statement fails with `driver_error`."""
    dbapi = AsyncAdapt_asyncpg_dbapi(asyncpg)
    adapted_type = dbapi.ProgrammingError if isinstance(driver_error, SyntaxOrAccessError) else dbapi.Error
    adapted = adapted_type(f"{type(driver_error)}: {driver_error}")
    adapted.__cause__ = driver_error
    wrapped = DBAPIError.instance("SELECT 1", (), adapted, dbapi.Error)
    wrapped.__cause__ = adapted
    assert isinstance(wrapped, DBAPIError)
    return wrapped


@pytest.mark.parametrize(
    "error",
    [
        ConnectionRefusedError(61, "Connect call failed ('127.0.0.1', 5434)"),
        OSError("Multiple exceptions: [Errno 61] Connect call failed ('::1', 5434, 0, 0), [Errno 61] Connect call failed ('127.0.0.1', 5434)"),
        CannotConnectNowError("the database system is shutting down"),
        _from_statement(ConnectionDoesNotExistError("connection was closed in the middle of operation")),
    ],
    ids=["refused", "every address refused", "starting up", "closed under a statement"],
)
def test_a_refused_failed_or_closed_connection_is_a_database_connection_failure(error: Exception) -> None:
    assert is_database_connection_failure(error) is True


@pytest.mark.parametrize(
    "error",
    [
        _from_statement(PostgresSyntaxError('syntax error at or near "SELEC"')),
        FileNotFoundError(2, "No such file or directory"),
        OSError("Multiple exceptions: [Errno 61] Connect call failed ('::1', 5434, 0, 0), [Errno 2] No such file or directory"),
    ],
    ids=["statement error", "missing file", "mixed connect failures"],
)
def test_an_error_the_statement_or_the_disk_causes_is_not_a_database_connection_failure(error: Exception) -> None:
    assert is_database_connection_failure(error) is False


@pytest.mark.parametrize(
    "error",
    [
        QueryCanceledError("canceling statement due to statement timeout"),
        _from_statement(QueryCanceledError("canceling statement due to statement timeout")),
    ],
    ids=["alone", "wrapped by SQLAlchemy"],
)
def test_a_statement_the_database_cancelled_is_a_statement_timeout(error: Exception) -> None:
    assert is_statement_timeout(error) is True


@pytest.mark.parametrize(
    "error",
    [
        _from_statement(PostgresSyntaxError('syntax error at or near "SELEC"')),
        _from_statement(ConnectionDoesNotExistError("connection was closed in the middle of operation")),
        TimeoutError("Request timed out."),
    ],
    ids=["statement error", "closed under a statement", "other timeout"],
)
def test_any_other_error_is_not_a_statement_timeout(error: Exception) -> None:
    assert is_statement_timeout(error) is False


def test_a_statement_timeout_is_not_a_database_connection_failure() -> None:
    error = _from_statement(QueryCanceledError("canceling statement due to statement timeout"))

    assert is_database_connection_failure(error) is False
    assert connection_failure_message(error, database=DATABASE) is None
