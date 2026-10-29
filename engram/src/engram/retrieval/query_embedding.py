"""The rule for a search query that cannot be embedded: the search runs keyword only, and its answer says why.

`run_sql` and `search` both follow it. An embedding that raises, or that runs past its deadline, leaves the search
on its BM25 leg alone, and the answer carries `keyword_only_wording`, naming the deadline or the error's own text.
"""

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from core.observability import get_logger

from .exceptions import QueryNotEmbeddedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

_logger = get_logger(__name__)

EMBED_DEADLINE_SECONDS = 10.0
"""The longest one search's query may take to embed, every text of one statement together."""

NO_EMBEDDER_REASON = "embedding not configured for this server"
"""Why a search ran keyword only on a server that has no embedder."""


def keyword_only_wording(reason: str, *, knowledge_bases: Sequence[str] = ()) -> str:
    """The words an answer carries when its search ran keyword only, `run_sql`'s `note` among them.

    Args:
        reason: Why the query was not embedded.
        knowledge_bases: The knowledge bases that ran keyword only, named when a search read several. Empty names none.

    Returns:
        The words, naming `reason` and any knowledge bases.
    """
    where = f" in {', '.join(knowledge_bases)}" if knowledge_bases else ""
    return f"(embedding unavailable, ran BM25-only{where}: {reason})"


async def embed_within[T](embed: Callable[[], Awaitable[T]], *, deadline: float) -> T:
    """Run one query's embedding within `deadline`.

    Args:
        embed: Starts the embedding when called.
        deadline: The longest the embedding may take, in seconds.

    Returns:
        What the embedding answered.

    Raises:
        QueryNotEmbeddedError: When the embedding raised or ran past `deadline`. Its `reason` names the deadline,
            or the error's text (its type name when it has none).
    """
    try:
        async with asyncio.timeout(deadline):
            return await embed()
    except TimeoutError as exc:
        _logger.info("query embedding ran past {seconds}s, degrading to BM25-only", seconds=deadline)
        reason = f"embedding took longer than {deadline:g} seconds"
        raise QueryNotEmbeddedError(reason) from exc
    except Exception as exc:
        _logger.info("query embedding failed ({problem}), degrading to BM25-only", problem=exc.__class__.__name__)
        raise QueryNotEmbeddedError(str(exc).strip() or exc.__class__.__name__) from exc
