"""The database connection failure rule: whether a case's error reports a refused, failed or closed database connection.

A database connection failure is a connection to the knowledge base's database that was refused, could not be opened
or closed under a statement, or a database shutting down or starting up. It is matched by the error's type, raised
alone or wrapped by SQLAlchemy, and by asyncio's text for a connection attempt that failed. An error the statement
itself causes, such as a syntax error, an undefined column, a refused permission or a statement timeout, is not one.
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
)
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


def is_database_connection_failure(error: BaseException) -> bool:
    """Whether `error`, raised by a case's task, is a database connection failure.

    Args:
        error: What the case raised.

    Returns:
        True for one of `DATABASE_CONNECTION_ERRORS`, alone or wrapped by SQLAlchemy, and for a connection attempt that
        asyncio reports as failed.
    """
    if isinstance(error, DBAPIError):
        return any(isinstance(cause, DATABASE_CONNECTION_ERRORS) for cause in _causes(error))
    if type(error) in _CONNECT_ATTEMPT_ERRORS:
        return _CONNECT_ATTEMPT_FAILURE.fullmatch(str(error)) is not None
    return isinstance(error, DATABASE_CONNECTION_ERRORS)


def _causes(error: BaseException) -> Iterator[BaseException]:
    """The errors `error` was raised from, nearest first."""
    cause = error.__cause__
    while cause is not None:
        yield cause
        cause = cause.__cause__
