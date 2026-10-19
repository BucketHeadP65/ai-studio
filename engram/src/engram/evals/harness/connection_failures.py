"""Which service a failed connection reached: an embedding endpoint, a model endpoint or the database.

A service connection failure's message names the service before the error's own text, so a person can tell which one
failed. An embedding endpoint is an OpenAI-compatible endpoint whose request path ends in `/embeddings`, and any other
such endpoint is a model endpoint. Both are named by their host. A database connection failure names the database the
caller gives.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openai import APIConnectionError

from engram.evals.harness.infrastructure.database_failures import is_database_connection_failure

if TYPE_CHECKING:
    from collections.abc import Iterator

_EMBEDDINGS_PATH = "/embeddings"


class ServiceConnectionError(Exception):
    """A case's connection to a service failed. Its message names the service before the error's own text."""


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


def _chain(error: BaseException) -> Iterator[BaseException]:
    """`error`, then the error it was raised from, or else the one it was raised while handling, nearest first."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ if current.__cause__ is not None else (None if current.__suppress_context__ else current.__context__)
