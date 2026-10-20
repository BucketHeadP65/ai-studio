"""Whether an error reports a failed connection, and which service it reached: an embedding endpoint, a model endpoint or the database.

A database connection failure is a connection to the database that was refused, could not be opened or closed under a
statement, or a database shutting down or starting up. It is matched by the error's type, raised alone or wrapped by
SQLAlchemy, and by asyncio's text for a connection attempt that failed. An error the statement itself causes, such as a
syntax error, an undefined column, a refused permission or a statement timeout, is not one. A statement timeout is told
apart on its own.

A service connection failure's message names the service before the error's own text, so a person can tell which one
failed. An embedding endpoint is an OpenAI-compatible endpoint whose request path ends in `/embeddings`, and any other
such endpoint is a model endpoint. Both are named by their host. A database connection failure names the database the
caller gives.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

from asyncpg.exceptions import (
    AdminShutdownError,
    CannotConnectNowError,
    ClientCannotConnectError,
    ConnectionDoesNotExistError,
    ConnectionFailureError,
    ConnectionRejectionError,
    CrashShutdownError,
    QueryCanceledError,
)
from openai import APIConnectionError
from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from collections.abc import Iterator

DATABASE_CONNECTION_ERRORS: tuple[type[Exception], ...] = (
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
)
"""The errors of a refused, reset or closed connection and of a database shutting down or starting up.

Python's connection errors, and asyncpg's errors for a connection that could not be opened, does not exist, was
rejected or failed, for an administrator or crash shutdown, and for a database not yet accepting connections.
"""

# The exact types asyncio raises a failed connection attempt as when its errno names no connection error.
_CONNECT_ATTEMPT_ERRORS: tuple[type[OSError], ...] = (OSError, TimeoutError)

# asyncio's text for a connection attempt that failed on one address, or on every address of the host.
_CONNECT_CALL_FAILED = r"\[Errno \d+\] Connect call failed \([^()]*\)"
_CONNECT_ATTEMPT_FAILURE = re.compile(rf"{_CONNECT_CALL_FAILED}|Multiple exceptions: {_CONNECT_CALL_FAILED}(?:, {_CONNECT_CALL_FAILED})*")

_EMBEDDINGS_PATH = "/embeddings"


def is_database_connection_failure(error: BaseException) -> bool:
    """Whether `error` is a database connection failure.

    Args:
        error: What was raised.

    Returns:
        True for one of `DATABASE_CONNECTION_ERRORS`, alone or wrapped by SQLAlchemy, and for a connection attempt that
        asyncio reports as failed.
    """
    if isinstance(error, DBAPIError):
        return any(isinstance(cause, DATABASE_CONNECTION_ERRORS) for cause in _causes(error))
    if type(error) in _CONNECT_ATTEMPT_ERRORS:
        return _CONNECT_ATTEMPT_FAILURE.fullmatch(str(error)) is not None
    return isinstance(error, DATABASE_CONNECTION_ERRORS)


def is_statement_timeout(error: BaseException) -> bool:
    """Whether `error` reports a statement the database cancelled, as it cancels one past its statement timeout.

    Args:
        error: What was raised.

    Returns:
        True for asyncpg's `QueryCanceledError`, alone or wrapped by SQLAlchemy. The database raises it for a statement
        past its statement timeout and for a statement a cancel request stopped.
    """
    if isinstance(error, DBAPIError):
        return any(isinstance(cause, QueryCanceledError) for cause in _causes(error))
    return isinstance(error, QueryCanceledError)


def connection_failure_message(error: BaseException, *, database: str | None) -> str | None:
    """The error's text after the service its failed connection reached, or None when no connection to a service failed.

    It reads `error` and every error it was raised from or while handling, nearest first.

    Args:
        error: What was raised.
        database: The database URL with its password redacted, which a database connection failure names. None when the
            caller opens no database, and then a database connection failure is not named.

    Returns:
        `<service>: <error text>`, such as `embedding endpoint example.openai.azure.com: Connection error.`, or None. An
        error with no text gives its type name in its place.
    """
    service = _failed_service(error, database=database)
    if service is None:
        return None
    return f"{service}: {str(error) or type(error).__name__}"


def _failed_service(error: BaseException, *, database: str | None) -> str | None:
    for link in _chain(error):
        if isinstance(link, APIConnectionError):
            url = link.request.url
            kind = "embedding endpoint" if url.path.rstrip("/").endswith(_EMBEDDINGS_PATH) else "model endpoint"
            return f"{kind} {url.host}"
        if database is not None and is_database_connection_failure(link):
            return f"database {database}"
    return None


def _causes(error: BaseException) -> Iterator[BaseException]:
    """The errors `error` was raised from, nearest first."""
    cause = error.__cause__
    while cause is not None:
        yield cause
        cause = cause.__cause__


def _chain(error: BaseException) -> Iterator[BaseException]:
    """`error`, then the error it was raised from, or else the one it was raised while handling, nearest first."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ if current.__cause__ is not None else (None if current.__suppress_context__ else current.__context__)
