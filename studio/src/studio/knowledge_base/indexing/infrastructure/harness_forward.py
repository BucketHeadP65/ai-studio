"""Telling the harness about each index's changed pages, over the one harness endpoint this app was configured with.

The address comes from this app's own settings. The knowledge base is named as the registry of knowledge bases names it,
found from the collection the index wrote. An answer the harness can recover from raises, so the worker delivers the event
again on its next pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import httpx

from core.observability import get_logger
from engram.indexing.contracts import PagesLoaded
from engram.kb.contracts import load_knowledge_base_schema

from studio.knowledge_base.indexing.exceptions import IndexForwardFailedError
from studio.knowledge_base.settings import read_registry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from core.cqrs.contracts import Event

_logger = get_logger(__name__)

INDEX_RUNS_PATH = "/api/schedules/index-runs"
"""The harness route each index's changed pages are posted to."""


class IndexRunPayload(TypedDict):
    """The body the harness route reads: which event, which registered knowledge base, and its changed pages."""

    event_id: str
    knowledge_base: str
    page_ids: list[str]


def registered_knowledge_base_named(settings_path: Path, collection: str) -> str | None:
    """The name of the registered knowledge base whose schema declares `collection`, read from the settings file on every call.

    A settings file that does not read answers `None`, and a registered knowledge base whose schema does not load is skipped,
    each with a log line. When several declare the collection, the first in the registry answers.

    Args:
        settings_path: The settings file whose `knowledge_bases` map is the registry of knowledge bases.
        collection: The collection an index wrote, as a schema's `retrieval.collection` declares it.

    Returns:
        The registered name, or `None` when no registered knowledge base declares that collection.
    """
    try:
        registry, _broken = read_registry(settings_path)
    except (OSError, ValueError) as exc:
        _logger.warning(
            "the harness index forward could not read the registry at {path}: {reason}", path=str(settings_path), reason=str(exc)
        )
        return None
    for name, entry in registry.items():
        try:
            schema = load_knowledge_base_schema(entry.path)
        except (OSError, ValueError) as exc:
            _logger.warning(
                "the harness index forward skips {name}, its knowledge-base-schema.json did not load: {reason}", name=name, reason=str(exc)
            )
            continue
        if schema.retrieval.collection == collection:
            return name
    return None


def _delivered_again(status: int) -> bool:
    """Whether an answer means the event goes back to the outbox: no such route yet, or the harness failed."""
    return status == httpx.codes.NOT_FOUND or status >= httpx.codes.INTERNAL_SERVER_ERROR


class HarnessIndexForward:
    """`EventSubscriber` posting each index's changed pages to the harness, under the registered name of the knowledge base indexed.

    An index with no changed pages, or of a collection no registered knowledge base declares, is not posted. A harness that
    cannot be reached, answers 404, or answers a server error raises `IndexForwardFailedError`, so the event is delivered
    again. Any other refusal is logged with the harness's own words and counts as delivered, because the same body would be
    refused again.
    """

    def __init__(
        self,
        base_url: str,
        *,
        knowledge_base_named: Callable[[str], str | None],
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Hold where to post, how to name the knowledge base, how long to wait, and the transport to post over.

        Args:
            base_url: The harness this deployment reports to. Configuration, never a request.
            knowledge_base_named: Answers the registered name of the knowledge base whose schema declares a collection, or
                `None` when none does. It is asked again for every event.
            timeout_seconds: How long one request may take before it is refused.
            transport: The transport that carries the request. `None` lets httpx open its own.
        """
        self._base_url = base_url.rstrip("/")
        self._knowledge_base_named = knowledge_base_named
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def __call__(self, event: Event) -> None:
        """Post `event` to the harness when it is an index with changed pages of a registered knowledge base.

        Args:
            event: The event being delivered.

        Raises:
            IndexForwardFailedError: The harness could not be reached, has no such route yet, or failed.
        """
        if not isinstance(event, PagesLoaded) or not event.page_ids:
            return
        knowledge_base = self._knowledge_base_named(event.collection)
        if knowledge_base is None:
            _logger.info(
                "no registered knowledge base declares collection {collection!r}, its changed pages are not forwarded",
                collection=event.collection,
            )
            return
        payload: IndexRunPayload = {"event_id": str(event.event_id), "knowledge_base": knowledge_base, "page_ids": list(event.page_ids)}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = await client.post(f"{self._base_url}{INDEX_RUNS_PATH}", json=payload)
        except httpx.HTTPError as error:
            msg = f"the harness at {self._base_url} could not be reached for index {event.event_id} of {knowledge_base!r}: {error}"
            raise IndexForwardFailedError(msg) from error
        if response.is_success:
            return
        if _delivered_again(response.status_code):
            msg = f"the harness answered {response.status_code} for index {event.event_id} of {knowledge_base!r}"
            raise IndexForwardFailedError(msg)
        _logger.warning(
            "harness_refused_index_run: index {event_id} of {knowledge_base!r} answered {status}: {body}",
            event_id=str(event.event_id),
            knowledge_base=knowledge_base,
            status=response.status_code,
            body=response.text,
        )
