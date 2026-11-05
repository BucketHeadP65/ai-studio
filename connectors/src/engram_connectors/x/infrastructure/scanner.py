"""The X scanner: a Grok model with native X search lists an account's recent posts, rendered straight into the source queue cache."""

from __future__ import annotations

import re

from datetime import timedelta
from itertools import islice
from typing import TYPE_CHECKING

from core.ai.content_filter import content_filter_message
from core.cqrs.contracts import utcnow
from core.files import atomic_write
from engram.sources.contracts import EntryScan, FoundSource, ScanError, claimants_for, classify_url
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.capabilities import XSearch
from pydantic_ai.exceptions import ContentFilterError, ModelHTTPError, UnexpectedModelBehavior

from engram_connectors.x.domain import XScan, render_post
from engram_connectors.xai import build_xai_model, xai_resource

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from datetime import datetime
    from pathlib import Path

    from engram.config import SourceResources
    from engram.sources.contracts import Scanner, UrlClaimant, WatchEntry

    from engram_connectors.x.domain import XPost
    from engram_connectors.xai import XaiResource

type AgentFactory = Callable[[Sequence[str], datetime, datetime], Agent[None, XScan]]
"""A function from the handles and the start and end of one slice of a scan's window to the agent that searches that slice."""
type ClaimantResolver = Callable[[Sequence[str]], Sequence[UrlClaimant]]
"""A function from declared connector names to the claimants that key a post's URL."""

_INSTRUCTIONS = (
    "You list what an X account posted. Use X search over the allowed handles. "
    "Search with the query `from:<handle>` alone. Never put a date operator such as `since:` or `until:` in the query, "
    "because the date window you were given already bounds X search. "
    "When a search answers a post from outside the window, return it too. "
    "Search more than once before you answer that there are no posts: search in Latest mode and in Top mode, "
    "and never repeat a search that found nothing. "
    "Return every post with substance in the window (a claim, a link, a thread), up to the number of posts the request asks for. "
    "Keep searching while a search still finds posts you do not hold yet. "
    "Give each post's URL, the handle, the date, the full text, and every link it carries. Newest first. "
    "Do not summarize. Copy the text."
)

_SLICE = timedelta(days=1)
"""How much of a scan's window one search run covers."""
_FIRST_SCAN_WINDOW = timedelta(days=7)
"""How far back a first scan reaches."""
# one slice is one model call, so this bounds what one scan costs, and a stamp three weeks old catches up over two scans
_SLICES_PER_SCAN = 14
"""The most one-day slices one scan searches."""


def x_scan_agent(resource: XaiResource, *, handles: Sequence[str], since: datetime, until: datetime) -> Agent[None, XScan]:
    """Build a Grok agent with native X search bounded to `handles` and the window from `since` to `until`.

    Only the first twenty handles are searched.

    Args:
        resource: The xAI connection the agent's model runs on.
        handles: The X handles the search may read, of which the first twenty are kept.
        since: The earliest date the search covers.
        until: The latest date the search covers.

    Returns:
        An agent whose structured output is an `XScan`.
    """
    model = build_xai_model(resource)
    return Agent(
        model,
        # an output tool would never be called: with X search running, Grok ends its answer with text
        output_type=NativeOutput(XScan),
        instructions=_INSTRUCTIONS,
        capabilities=[XSearch(allowed_x_handles=list(handles)[:20], from_date=since, to_date=until)],
    )


def grok_agent_factory(resource: XaiResource) -> AgentFactory:
    """Build the live agent factory over one xAI connection, which makes a fresh Grok agent per slice of a scan.

    Args:
        resource: The xAI connection every agent runs on.

    Returns:
        A factory that takes the handles and the start and end of one slice.
    """
    return lambda handles, since, until: x_scan_agent(resource, handles=handles, since=since, until=until)


_ID = re.compile(r"\s*\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
"""A UUID and the space before it, the shape of the team id xAI names in an account error."""
_SENTENCE = re.compile(r".*?[.!?](?=\s|$)")
_SENTENCE_LIMIT = 200
"""The longest first sentence of a response body a failure keeps. A longer one is dropped."""


def _first_sentence(body: str) -> str:
    """The first sentence of a response body with any team id cut, or empty when it is longer than `_SENTENCE_LIMIT`."""
    line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    found = _SENTENCE.match(line)
    sentence = _ID.sub("", found.group(0) if found else line)
    return sentence if len(sentence) <= _SENTENCE_LIMIT else ""


def _failure_text(error: Exception) -> str:
    """What `error` says without a response body: a filter's categories, a model error's message, else its whole text."""
    if isinstance(error, ContentFilterError):
        return content_filter_message(error)
    if isinstance(error, UnexpectedModelBehavior):
        return error.message
    return str(error)


def _scan_failure(error: Exception) -> str:
    """One line saying why a scan failed, any team id cut: xAI's status, model and first body sentence, else the first line."""
    if isinstance(error, ModelHTTPError):
        said = f"xAI answered HTTP {error.status_code} for {error.model_name}"
        sentence = _first_sentence(error.body) if isinstance(error.body, str) else ""
        line = f"{said}: {sentence}" if sentence else said
    else:
        lines = _failure_text(error).strip().splitlines()
        line = lines[0] if lines else type(error).__name__
    return _ID.sub("", line)


def _day_slices(start: datetime, end: datetime) -> Iterator[tuple[datetime, datetime]]:
    """The window from `start` to `end` as one-day slices, oldest first, the newest one shorter when the window is not whole days."""
    slice_start = start
    while slice_start < end:
        slice_end = min(slice_start + _SLICE, end)
        yield slice_start, slice_end
        slice_start = slice_end


def _by_date_descending(post: XPost) -> tuple[bool, float]:
    """Sort key for newest-first order: dated posts by descending timestamp, undated posts last."""
    if post.date is None:
        return (True, 0.0)
    return (False, -post.date.timestamp())


class XScanner:
    """`Scanner` for `x` feeds: one agent run per day of a scan's window, oldest day first, posts rendered into the source queue cache."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        cache_dir: Path,
        connectors: Sequence[str] = (),
        resolve_claimants: ClaimantResolver = claimants_for,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Keep the agent factory, the cache folder, the connector names and the clock.

        `connectors` are names, not claimants. They resolve on the first post a scan keys, not at
        construction, so nothing a knowledge base declares is imported until a scan classifies a URL.
        An empty `connectors` keys every post generically with `classify_url`.

        Args:
            agent_factory: Makes one agent for the handles and window of a scan.
            cache_dir: The source queue cache folder each post is rendered into.
            connectors: The knowledge base's declared connector names, in its own order.
            resolve_claimants: Turns the connector names into the claimants that key a post's URL.
            clock: Timezone-aware "now", where every scan's window ends.
        """
        self._agent_factory = agent_factory
        self._cache_dir = cache_dir
        self._connectors = connectors
        self._resolve_claimants = resolve_claimants
        self._claimants: Sequence[UrlClaimant] | None = None
        self._clock = clock

    @classmethod
    def build(
        cls,
        resources: SourceResources,
        *,
        agent_factory_for: Callable[[XaiResource], AgentFactory] = grok_agent_factory,
        resolve_claimants: ClaimantResolver = claimants_for,
    ) -> Scanner:
        """Construct the `x` scanner, which renders into the knowledge base's `_meta/source-queue-cache`.

        Reads no file. A post's cache key comes from the knowledge base's declared connectors in
        `resources.connectors`, resolved on first use. A rendered post uses the same key the knowledge
        base's source queue gives that URL. `agent_factory_for` and `resolve_claimants` are the two live
        defaults a caller may replace to drive a built scanner without a model call or an installed connector.

        Args:
            resources: The knowledge base, its declared connectors and the `xai` settings block.
            agent_factory_for: Builds the agent factory from the xAI connection.
            resolve_claimants: Turns the connector names into the claimants that key a post's URL.

        Returns:
            The scanner, ready to scan a watch-list entry.

        Raises:
            ValueError: `resources` declares no `xai` settings block with an `api_key`, which every scan needs.
            ValidationError: The `xai` settings block is present but does not validate.
        """
        resource = xai_resource(resources)
        if resource is None:
            msg = "the x scanner requires an xai settings block carrying an api_key, none was given"
            raise ValueError(msg)
        return cls(
            agent_factory_for(resource),
            cache_dir=resources.knowledge_base.path / "_meta" / "source-queue-cache",
            connectors=resources.connectors,
            resolve_claimants=resolve_claimants,
        )

    def _url_claimants(self) -> Sequence[UrlClaimant]:
        """The claimants the declared names stand for, resolved once on first use and kept."""
        if self._claimants is None:
            self._claimants = self._resolve_claimants(self._connectors)
        return self._claimants

    async def scan(self, entry: WatchEntry, *, since: datetime | None) -> EntryScan:
        """List the posts by `entry.ref` since `since`, rendering each one to the cache, and say how far the scan covered.

        The window runs from `since`, or from seven days back on a first scan, to now. It is searched one day at a time,
        oldest day first, for at most fourteen days. The walk stops after the day that brings it to `entry.max_items`
        posts and keeps every post that day answered. A post dated at or before `since` is dropped. A post carrying no
        date is kept, since nothing says it is old.

        Args:
            entry: The watch-list entry whose `ref` is the handle and whose `max_items` stops the walk.
            since: The date of the last scan, or `None` for a first scan.

        Returns:
            One source per post, newest first, each followed by one source per link it carries, with the end of the last
            day searched when the walk stopped before now. When a search fails after posts were found, the posts found so
            far, covered up to the end of the day before it, with one line saying why the scan stopped.

        Raises:
            ScanError: A search failed before any post was found, with one line saying why.
        """
        end = self._clock()
        start = since if since is not None else end - _FIRST_SCAN_WINDOW
        posts: dict[str, XPost] = {}
        covered = start
        for slice_start, slice_end in islice(_day_slices(start, end), _SLICES_PER_SCAN):
            try:
                found = await self._search(entry, slice_start, slice_end, wanted=entry.max_items - len(posts))
            except Exception as error:
                if not posts:
                    raise ScanError(_scan_failure(error)) from error
                return EntryScan(sources=self._sources(posts), covered_until=covered, failure=_scan_failure(error))
            for post in found:
                if since is None or post.date is None or post.date > since:
                    posts.setdefault(post.url, post)
            covered = slice_end
            if len(posts) >= entry.max_items:
                break
        return EntryScan(sources=self._sources(posts), covered_until=None if covered >= end else covered)

    async def _search(self, entry: WatchEntry, start: datetime, end: datetime, *, wanted: int) -> list[XPost]:
        """The posts one agent run answers for the window from `start` to `end`."""
        agent = self._agent_factory([entry.ref], start, end)
        result = await agent.run(f"List every post by @{entry.ref} from {start.isoformat()} to {end.isoformat()}, up to {wanted} posts.")
        return result.output.posts

    def _sources(self, posts: Mapping[str, XPost]) -> list[FoundSource]:
        """Render every post to the cache and answer one source per post, newest first, then one per link it carries."""
        entries: list[FoundSource] = []
        for post in sorted(posts.values(), key=_by_date_descending):
            _, key = classify_url(post.url, connectors=self._url_claimants())
            self._render(key, post)
            entries.append(FoundSource(url=post.url, title=post.text[:80], published=post.date))
            entries.extend(FoundSource(url=link, title=link, published=post.date) for link in post.links)
        return entries

    def _render(self, key: str, post: XPost) -> None:
        """Write one post and its metadata to the source queue cache under `key`."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        rendered = render_post(url=post.url, handle=post.handle, date=post.date, text=post.text, links=post.links)
        atomic_write(self._cache_dir / f"{key}.md", rendered.markdown)
        atomic_write(self._cache_dir / f"{key}.meta.json", rendered.model_dump_json(exclude={"markdown"}))
