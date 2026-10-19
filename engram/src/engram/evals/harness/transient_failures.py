"""The transient-failure rule: a failed case whose error came from the model's service or the database connection, not the agent.

A transient failure is a model connection error, a model request timeout, an HTTP 429 or 5xx from the model, or a
database connection failure: a connection to the knowledge base's database that was refused, could not be opened or
closed under a statement, or a database shutting down or starting up. A tool-call limit, a refused answer, an error the
agent's own SQL statement causes and every other error are not transient. A resumed evaluation run runs a case that
failed transiently again and keeps every other case line.
"""

from __future__ import annotations

import re

from core.connection_failures import is_database_connection_failure, is_database_connection_failure_text
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, *range(500, 600)})
"""The HTTP statuses from the model that make a failure transient: 429, too many requests, and every server error."""

TRANSIENT_MODEL_MESSAGES: frozenset[str] = frozenset({"Connection error.", "Request timed out."})
"""The messages of the model errors that report a connection error and a request timeout, as the OpenAI client words them."""

_STATUS_CODE = re.compile(r"status_code: (\d+),")


def is_transient_failure(error: BaseException) -> bool:
    """Whether `error`, raised by a case's task, is a transient failure.

    Args:
        error: What the case raised.

    Returns:
        True for a model connection error, a model request timeout, an HTTP 429 or 5xx from the model, or a database
        connection failure.
    """
    if isinstance(error, ModelHTTPError):
        return error.status_code in TRANSIENT_STATUS_CODES
    if isinstance(error, ModelAPIError):
        return error.message in TRANSIENT_MODEL_MESSAGES
    return is_database_connection_failure(error)


def is_transient_error_text(error: str | None) -> bool:
    """Whether a failed case's recorded error text is a transient failure, for a results file that did not record it.

    The text is the error's type name and its message, separated by a colon and a space, and it reads the same way
    `is_transient_failure` reads the error itself.

    Args:
        error: The case line's error text, or None for an answered case.

    Returns:
        True when the text records a model connection error, a model request timeout, an HTTP 429 or 5xx from the model,
        or a database connection failure.
    """
    name, _separator, message = (error or "").partition(": ")
    if name == ModelHTTPError.__name__:
        status = _STATUS_CODE.match(message)
        return status is not None and int(status.group(1)) in TRANSIENT_STATUS_CODES
    if name == ModelAPIError.__name__:
        return message in TRANSIENT_MODEL_MESSAGES
    return is_database_connection_failure_text(name, message)
