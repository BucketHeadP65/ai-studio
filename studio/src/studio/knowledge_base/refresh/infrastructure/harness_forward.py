"""Telling the harness about each ended refresh run, over the one harness endpoint this app was configured with.

The address comes from this app's own settings. An answer the harness can recover from raises, so the worker delivers the event
again on its next pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import httpx

from core.observability import get_logger

from studio.knowledge_base.refresh.domain import RefreshAbandoned, RefreshCompleted, is_forwarded
from studio.knowledge_base.refresh.exceptions import RefreshForwardFailedError

if TYPE_CHECKING:
    from core.cqrs.contracts import Event

_logger = get_logger(__name__)

REFRESH_RUNS_PATH = "/api/schedules/refresh-runs"
"""The harness route an ended refresh run is posted to."""


class RefreshRunPayload(TypedDict):
    """The body the harness route reads: which ending, which knowledge base, and what the refresh run brought in."""

    event_id: str
    knowledge_base: str
    sources_created: int
    requested_by_entry: dict[str, int]


def _delivered_again(status: int) -> bool:
    """Whether an answer means the event goes back to the outbox: no such route yet, or the harness failed."""
    return status == httpx.codes.NOT_FOUND or status >= httpx.codes.INTERNAL_SERVER_ERROR


class HarnessRefreshForward:
    """`EventSubscriber` posting every completed refresh run, and every abandoned one that created sources, to the harness.

    A harness that cannot be reached, answers 404, or answers a server error raises `RefreshForwardFailedError`, so the event is
    delivered again. Any other refusal is logged with the harness's own words and counts as delivered, because the same body
    would be refused again.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Hold where to post, how long to wait, and the transport to post over.

        Args:
            base_url: The harness this deployment reports to. Configuration, never a request.
            timeout_seconds: How long one request may take before it is refused.
            transport: The transport that carries the request. `None` lets httpx open its own.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def __call__(self, event: Event) -> None:
        """Post `event` to the harness when it is a refresh run's ending the harness hears about.

        Args:
            event: The event being delivered.

        Raises:
            RefreshForwardFailedError: The harness could not be reached, has no such route yet, or failed.
        """
        if not isinstance(event, RefreshCompleted | RefreshAbandoned) or not is_forwarded(event):
            return
        payload: RefreshRunPayload = {
            "event_id": str(event.event_id),
            "knowledge_base": event.knowledge_base,
            "sources_created": event.counts.sources_created,
            "requested_by_entry": dict(event.counts.requested_by_entry),
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds, transport=self._transport) as client:
                response = await client.post(f"{self._base_url}{REFRESH_RUNS_PATH}", json=payload)
        except httpx.HTTPError as error:
            msg = f"the harness at {self._base_url} could not be reached for refresh run {event.event_id} of {event.knowledge_base!r}: {error}"
            raise RefreshForwardFailedError(msg) from error
        if response.is_success:
            return
        if _delivered_again(response.status_code):
            msg = f"the harness answered {response.status_code} for refresh run {event.event_id} of {event.knowledge_base!r}"
            raise RefreshForwardFailedError(msg)
        _logger.warning(
            "harness_refused_refresh_run: refresh run {event_id} of {knowledge_base!r} answered {status}: {body}",
            event_id=str(event.event_id),
            knowledge_base=event.knowledge_base,
            status=response.status_code,
            body=response.text,
        )
