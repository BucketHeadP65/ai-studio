"""The X scanner: a Grok model with native X search lists an account's recent posts, rendered straight into the source queue cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.files import atomic_write
from engram.sources.contracts import FoundSource, ScanError, claimants_for, classify_url
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.capabilities import XSearch

from engram_connectors.x.domain import XScan, render_post
from engram_connectors.xai import build_xai_model, xai_resource

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from pathlib import Path

    from engram.config import SourceResources
    from engram.sources.contracts import Scanner, UrlClaimant, WatchEntry

    from engram_connectors.x.domain import XPost
    from engram_connectors.xai import XaiResource

type AgentFactory = Callable[[Sequence[str], datetime | None], Agent[None, XScan]]
"""A function from the handles and the earliest date of a scan to the agent that runs it."""
type ClaimantResolver = Callable[[Sequence[str]], Sequence[UrlClaimant]]
"""A function from declared connector names to the claimants that key a post's URL."""

_INSTRUCTIONS = (
    "You list what an X account posted. Use X search over the allowed handles and any date window you were given. "
    "Return every post with substance: a claim, a link, a thread. Give each post's URL, the handle, the date, the "
    "full text, and every link it carries. Newest first. Do not summarize. Copy the text."
)


def x_scan_agent(resource: XaiResource, *, handles: Sequence[str], since: datetime | None) -> Agent[None, XScan]:
    """Build a Grok agent with native X search bounded to `handles` and `since`.

    Only the first twenty handles are searched.

    Args:
        resource: The xAI connection the agent's model runs on.
        handles: The X handles the search may read, of which the first twenty are kept.
        since: The earliest date the search covers, or `None` for no lower bound.

    Returns:
        An agent whose structured output is an `XScan`.
    """
    model = build_xai_model(resource)
    return Agent(
        model,
        # an output tool would never be called: with X search running, Grok ends its answer with text
        output_type=NativeOutput(XScan),
        instructions=_INSTRUCTIONS,
        capabilities=[XSearch(allowed_x_handles=list(handles)[:20], from_date=since)],
    )


def grok_agent_factory(resource: XaiResource) -> AgentFactory:
    """Build the live agent factory over one xAI connection, which makes a fresh Grok agent per scan.

    Args:
        resource: The xAI connection every agent runs on.

    Returns:
        A factory that takes the handles and the window of one scan.
    """
    return lambda handles, since: x_scan_agent(resource, handles=handles, since=since)


def _by_date_descending(post: XPost) -> tuple[bool, float]:
    """Sort key for newest-first order: dated posts by descending timestamp, undated posts last."""
    if post.date is None:
        return (True, 0.0)
    return (False, -post.date.timestamp())


class XScanner:
    """`Scanner` for `x` feeds: one agent run per handle, posts rendered into the source queue cache."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        cache_dir: Path,
        connectors: Sequence[str] = (),
        resolve_claimants: ClaimantResolver = claimants_for,
        max_posts: int = 50,
    ) -> None:
        """Keep the agent factory, the cache folder, the connector names and the per-scan post cap.

        `connectors` are names, not claimants. They resolve on the first post a scan keys, not at
        construction, so nothing a knowledge base declares is imported until a scan classifies a URL.
        An empty `connectors` keys every post generically with `classify_url`.

        Args:
            agent_factory: Makes one agent for the handles and window of a scan.
            cache_dir: The source queue cache folder each post is rendered into.
            connectors: The knowledge base's declared connector names, in its own order.
            resolve_claimants: Turns the connector names into the claimants that key a post's URL.
            max_posts: The most posts one scan reports, before the entry's own cap applies.
        """
        self._agent_factory = agent_factory
        self._cache_dir = cache_dir
        self._connectors = connectors
        self._resolve_claimants = resolve_claimants
        self._claimants: Sequence[UrlClaimant] | None = None
        self._max_posts = max_posts

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

    async def scan(self, entry: WatchEntry, *, since: datetime | None) -> list[FoundSource]:
        """List the posts by `entry.ref` since `since`, newest first, rendering each one to the cache.

        A post dated at or before `since` is dropped after the cap, so a scan reports only what is new.
        A post carrying no date is kept, since nothing says it is old.

        Args:
            entry: The watch-list entry whose `ref` is the handle and whose `max_items` caps the scan.
            since: The date of the last scan, or `None` for a first scan. A first scan asks the model for the last seven
                days and sets no lower bound on the search itself.

        Returns:
            One source per post, followed by one source per link that post carries.

        Raises:
            ScanError: The agent could not be built or its run failed.
        """
        window = f" since {since.isoformat()}" if since is not None else " over the last seven days"
        try:
            agent = self._agent_factory([entry.ref], since)
            result = await agent.run(f"List every post by @{entry.ref}{window}.")
        except Exception as error:
            raise ScanError(str(error)) from error
        cap = min(self._max_posts, entry.max_items)
        newest_first = sorted(result.output.posts, key=_by_date_descending)
        entries: list[FoundSource] = []
        for post in newest_first[:cap]:
            if since is not None and post.date is not None and post.date <= since:
                continue
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
