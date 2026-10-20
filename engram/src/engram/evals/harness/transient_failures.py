"""The transient-failure rule: a failed case whose error came from the model's service or the database, not the agent.

A transient failure is a model connection error, a model request timeout, an HTTP 429 or 5xx from the model, a
database connection failure (a connection to the knowledge base's database that was refused, could not be opened or
closed under a statement, or a database shutting down or starting up), or a statement the database cancelled past its
statement timeout. A tool-call limit, a refused answer, any other error an SQL statement causes and every other error
are not transient. A resumed evaluation run runs a case that failed transiently again and keeps every other case line.
A search score runs such a case again after a wait, a bounded number of times.
"""

from __future__ import annotations

from core.connection_failures import is_database_connection_failure, is_statement_timeout
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

TRANSIENT_STATUS_CODES: frozenset[int] = frozenset({429, *range(500, 600)})
"""The HTTP statuses from the model that make a failure transient: 429, too many requests, and every server error."""

TRANSIENT_MODEL_MESSAGES: frozenset[str] = frozenset({"Connection error.", "Request timed out."})
"""The messages of the model errors that report a connection error and a request timeout, as the OpenAI client words them."""


def is_transient_failure(error: BaseException) -> bool:
    """Whether `error`, raised by a case's task, is a transient failure.

    Args:
        error: What the case raised.

    Returns:
        True for a model connection error, a model request timeout, an HTTP 429 or 5xx from the model, a database
        connection failure, or a statement timeout.
    """
    if isinstance(error, ModelHTTPError):
        return error.status_code in TRANSIENT_STATUS_CODES
    if isinstance(error, ModelAPIError):
        return error.message in TRANSIENT_MODEL_MESSAGES
    return is_database_connection_failure(error) or is_statement_timeout(error)
