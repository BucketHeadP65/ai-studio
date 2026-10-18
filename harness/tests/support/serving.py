"""How a test drives a route and reads what it answered: over the wire, or on the object itself."""

from __future__ import annotations

import json

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse, StreamingResponse

from tests.support.container import IO_LEAVES, a_client, an_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping
    from pathlib import Path

    import httpx


@asynccontextmanager
async def a_served_client(*, state: Path, answering: Mapping[type, object] | None = None) -> AsyncGenerator[httpx.AsyncClient]:
    """A client over an application whose lifespan has run, so its controllers are mounted.

    `answering` is what this test puts in the container beside `IO_LEAVES`: its own session, its own
    pool, whatever the case scripts. The lifespan is entered on the caller's own loop rather than in
    a portal thread, which is what lets a test hold a turn open on an `asyncio.Event` the
    application waits on too.

    Args:
        state: The state home this application writes under.
        answering: What answers for a registration, over `IO_LEAVES`.

    Yields:
        A client over the entered application.
    """
    app = an_app(state=state)
    with app.state.wireup_container.override({**dict(IO_LEAVES), **dict(answering or {})}):
        async with app.router.lifespan_context(app), a_client(app) as client:
            yield client


def answered(response: JSONResponse | StreamingResponse) -> dict[str, Any]:
    """The JSON body a route answered, decoded -- what the browser would have parsed.

    A route that streamed instead of answering fails here rather than further along.
    """
    assert isinstance(response, JSONResponse), "the turn streamed rather than answering one JSON body"
    body = json.loads(bytes(response.body))
    assert isinstance(body, dict), "a route answers one JSON object"
    return body


async def streamed(response: JSONResponse | StreamingResponse) -> list[dict[str, Any]]:
    """The protocol chunks a streamed turn wrote, in order; the `[DONE]` terminator dropped.

    Drains the response's own body, which is what the server sends: draining it is also what runs
    the turn, since the run happens as the stream is drawn. A refusal answered instead of a stream
    fails here, carrying its own words.
    """
    assert isinstance(response, StreamingResponse), f"the turn was refused rather than streamed: {bytes(response.body)!r}"
    written = "".join([part if isinstance(part, str) else bytes(part).decode() async for part in response.body_iterator])
    return [
        json.loads(line.removeprefix("data: "))
        for line in written.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
