"""The change-topic stream every reader refetches from."""

from core.cqrs.contracts import Broadcaster, change_frames
from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from wireup.integration.fastapi import WireupRoute

from studio.knowledge_base.http.api_models import error_response


class EventsController:
    """Every change this API hears, as an event stream, read from the broadcaster in the application's state."""

    router = APIRouter(prefix="/api/events", tags=["events"], route_class=WireupRoute)

    @router.get("", summary="Stream change topics", operation_id="streamChanges")
    async def stream(self, request: Request, topics: str = "") -> Response:
        """Every change this API hears, as an event stream.

        `topics=a,b` keeps only those roots and everything under them. No filter means every topic.
        The frames carry a topic and a time, never data: the reader refetches what it already reads.
        No broadcaster installed answers 503, which a browser's EventSource takes as final rather
        than reconnecting to.
        """
        channel: Broadcaster | None = getattr(request.app.state, "broadcaster", None)
        if channel is None:
            return error_response(503, "events_unavailable", "change notifications are not available, this process has no broadcaster.")
        wanted = frozenset(name.strip() for name in topics.split(",") if name.strip()) or None
        return StreamingResponse(change_frames(channel, wanted), media_type="text/event-stream")
