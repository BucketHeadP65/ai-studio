"""Turn one source queue row into rendered markdown, one resolver per source type."""

from __future__ import annotations

import re

from datetime import datetime
from typing import TYPE_CHECKING

import httpx

from core.files import atomic_write_bytes
from core.net import FetchError, refuse_unfetchable
from engram.sources.contracts import ConversionError, Resolved
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.capabilities import XSearch

from engram_connectors.source_queue.exceptions import ResolveError
from engram_connectors.x.contracts import render_post
from engram_connectors.xai import build_xai_model
from engram_connectors.youtube.contracts import VIDEO_KEY_PREFIX, TranscriptUnavailableError, VideoFetchFailedError, transcript_markdown

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from engram.sources.contracts import DocumentConverter, SourceQueueEntry

    from engram_connectors.source_queue.internal_contracts import Resolver
    from engram_connectors.xai import XaiResource
    from engram_connectors.youtube.contracts import TranscriptFetcher

_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
# Titles a site answers with instead of the page: bot checks, access walls, JavaScript gates.
_INTERSTITIAL_TITLE = re.compile(
    r"verify(?:ing)? (?:your|you are a) (?:browser|human)|just a moment|attention required|access denied"
    r"|checking your browser|are you a robot|bot verification|enable javascript and cookies|403 forbidden",
    re.IGNORECASE,
)
# What a challenge page asks of the reader when it keeps the site's own title. Read only on a short page.
_CHALLENGE_BODY = re.compile(
    r"complete the (?:security )?check below|checking if the site connection is secure|please complete the verification"
    r"|verify(?:ing)? (?:that )?you are (?:a )?human|enable javascript and cookies to continue|verifying your browser",
    re.IGNORECASE,
)
_CHALLENGE_PAGE_WORDS = 150
_MIN_ARTICLE_WORDS = 40


class VideoResolver:
    """A YouTube row: the transcript, segmented, as markdown."""

    def __init__(self, fetcher: TranscriptFetcher) -> None:
        """Store the transcript fetcher.

        Args:
            fetcher: The fetcher that answers a video's metadata and transcript.
        """
        self._fetcher = fetcher

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Fetch the transcript behind the row's video id, which is the key minus its `yt-` prefix.

        Args:
            entry: The video row to resolve.

        Returns:
            The segmented transcript as markdown, with the video's title, channel and published date.

        Raises:
            ResolveError: The video has no transcript, or the fetch fails.
        """
        try:
            fetched = await self._fetcher.fetch(entry.key.removeprefix(VIDEO_KEY_PREFIX))
        except (TranscriptUnavailableError, VideoFetchFailedError) as error:
            raise ResolveError(str(error)) from error
        doc = fetched.doc
        return Resolved(
            title=doc.title,
            markdown=transcript_markdown(doc),
            url=fetched.url,
            channel=doc.channel,
            published=doc.published,
            converter="youtube-transcript",
        )


_PDF_TIMEOUT = 60.0


async def _get_pdf(url: str) -> httpx.Response:
    """One GET of a PDF, following redirects, within the PDF timeout."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=_PDF_TIMEOUT) as client:
        return await client.get(url)


async def fetch_arxiv_pdf(paper_id: str) -> bytes | None:
    """Fetch `https://arxiv.org/pdf/<id>`.

    Args:
        paper_id: The arXiv paper id.

    Returns:
        The PDF bytes, or `None` when arXiv does not answer the PDF.
    """
    try:
        response = await _get_pdf(f"https://arxiv.org/pdf/{paper_id}")
    except httpx.HTTPError:
        return None
    return response.content if response.status_code == httpx.codes.OK else None


class ArxivResolver:
    """An arXiv row: the HTML rendering through the arXiv converter, keyed by paper id.

    A missing PDF is fetched into the cache first, where the converter's PDF fallback reads it.
    A fetch that answers nothing leaves no file, and the converter then reports on its own.
    `fetch_pdf=None` never fetches.
    """

    def __init__(
        self, converter: DocumentConverter, *, cache_dir: Path, fetch_pdf: Callable[[str], Awaitable[bytes | None]] | None = None
    ) -> None:
        """Store the converter, the cache directory its PDF path lives under, and the PDF fetch.

        Args:
            converter: The converter that renders the arXiv paper as markdown.
            cache_dir: The directory the paper's PDF is cached under.
            fetch_pdf: The fetch that answers a paper's PDF bytes by id, or `None` to never fetch.
        """
        self._converter = converter
        self._cache_dir = cache_dir
        self._fetch_pdf = fetch_pdf

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Convert `https://arxiv.org/abs/<id>`, fetching the PDF into the cache when it is absent.

        Args:
            entry: The `arxiv-`-keyed row to resolve.

        Returns:
            The paper as markdown, titled by its first heading or `arXiv <id>`.

        Raises:
            ResolveError: The converter cannot render the paper.
        """
        paper_id = entry.key.removeprefix("arxiv-")
        url = f"https://arxiv.org/abs/{paper_id}"
        pdf_path = self._cache_dir / f"{entry.key}.pdf"
        if self._fetch_pdf is not None and not pdf_path.is_file() and (pdf := await self._fetch_pdf(paper_id)) is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(pdf_path, pdf)
        try:
            result = await self._converter.to_markdown(pdf_path, source_url=url)
        except ConversionError as error:
            raise ResolveError(str(error)) from error
        title = _clean_title(_title_of(result.markdown, fallback=""), fallback=f"arXiv {paper_id}")
        return Resolved(title=title, markdown=result.markdown, url=url, converter=result.converter)


async def fetch_pdf_bytes(url: str) -> bytes:
    """Fetch `url`'s response body, refused up front when the SSRF guard will not let this process reach it.

    Args:
        url: The PDF's URL.

    Returns:
        The response body.

    Raises:
        ResolveError: The guard refuses `url`, the request itself fails, or the response is not
            a successful status.
    """
    try:
        await refuse_unfetchable(url)
    except FetchError as error:
        raise ResolveError(str(error)) from error
    try:
        response = await _get_pdf(url)
    except httpx.HTTPError as error:
        msg = f"{url} could not be fetched. {error}"
        raise ResolveError(msg) from error
    if response.status_code != httpx.codes.OK:
        msg = f"{url} answered HTTP {response.status_code}, not a PDF"
        raise ResolveError(msg)
    return response.content


class PdfResolver:
    """A non-arXiv PDF row. The row's own URL is fetched, cached and converted, guarded against SSRF.

    A refused fetch, a failed fetch or a conversion failure fails the row. None of them falls back to anything else.
    """

    def __init__(
        self, converter: DocumentConverter, *, cache_dir: Path, fetch: Callable[[str], Awaitable[bytes]] = fetch_pdf_bytes
    ) -> None:
        """Store the converter, the cache directory its PDF path lives under, and the guarded fetch.

        Args:
            converter: The converter that renders the PDF as markdown.
            cache_dir: The directory the PDF is cached under.
            fetch: The guarded fetch that answers a URL's PDF bytes.
        """
        self._converter = converter
        self._cache_dir = cache_dir
        self._fetch = fetch

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Fetch `entry.url` into the cache and convert it. The mirror's URL is `entry.url` itself.

        Args:
            entry: The PDF row to resolve.

        Returns:
            The PDF as markdown, titled by its first heading or its URL.

        Raises:
            ResolveError: The fetch is refused or fails, or the converter cannot read the PDF.
        """
        pdf = await self._fetch(entry.url)
        pdf_path = self._cache_dir / f"{entry.key}.pdf"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(pdf_path, pdf)
        try:
            result = await self._converter.to_markdown(pdf_path, source_url=entry.url)
        except ConversionError as error:
            msg = f"{entry.url} could not be converted. {error}"
            raise ResolveError(msg) from error
        title = _clean_title(_title_of(result.markdown, fallback=""), fallback=entry.url)
        return Resolved(title=title, markdown=result.markdown, url=entry.url, converter=result.converter)


class PaperResolver:
    """A paper row. The arXiv route serves an `arxiv-`-keyed row, the PDF route serves every other paper key.

    An arXiv link keys `arxiv-<id>` and reads the abstract page through the arXiv route.
    Any other PDF link keys the generic `url-<hash>` and is fetched, cached and converted from its own URL.
    It is never sent to arXiv.
    """

    def __init__(self, arxiv: Resolver, pdf: Resolver) -> None:
        """Store the two paper routes. `resolve` dispatches between them on the row's key.

        Args:
            arxiv: The route for an `arxiv-`-keyed row.
            pdf: The route for every other paper row.
        """
        self._arxiv = arxiv
        self._pdf = pdf

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Resolve through the arXiv route when `entry.key` starts with `arxiv-`, else the PDF route.

        Args:
            entry: The paper row to resolve.

        Returns:
            What the chosen route resolves.

        Raises:
            ResolveError: The chosen route cannot resolve the row.
        """
        resolver = self._arxiv if entry.key.startswith("arxiv-") else self._pdf
        return await resolver.resolve(entry)


def _word_count(markdown: str) -> int:
    return sum(1 for token in markdown.split() if any(char.isalnum() for char in token))


def challenge_reason(title: str, markdown: str, *, url: str) -> str | None:
    """Why a page fetched for `url` is a bot check rather than the article, or `None` when it is not one.

    A page is a bot check when its title is a challenge title, or when it is short and its body asks the reader to
    pass a check, whatever title it keeps.

    Args:
        title: The page's title.
        markdown: The page rendered as markdown.
        url: The URL the page was fetched for, named in the reason.

    Returns:
        The reason a source queue row carries when it fails, or `None`.
    """
    if _INTERSTITIAL_TITLE.search(title) or (_word_count(markdown) < _CHALLENGE_PAGE_WORDS and _CHALLENGE_BODY.search(markdown)):
        return f"{url} answered a bot-check page, not the article: {title!r}"
    return None


def _refuse_interstitial(title: str, markdown: str, *, url: str) -> None:
    """Raise `ResolveError` when a fetch answered a bot check or too few words to be the page asked for."""
    if (reason := challenge_reason(title, markdown, url=url)) is not None:
        raise ResolveError(reason)
    words = _word_count(markdown)
    if words < _MIN_ARTICLE_WORDS:
        msg = f"{url} answered {words} words, too few to be the article"
        raise ResolveError(msg)


class ArticleResolver:
    """A web page row: the article converter over the URL. A bot check or a near-empty answer fails the row."""

    def __init__(self, converter: DocumentConverter, *, cache_dir: Path) -> None:
        """Store the article converter and the cache directory its placeholder path names.

        Args:
            converter: The converter that fetches and renders the page.
            cache_dir: The directory the placeholder path of the page lives under.
        """
        self._converter = converter
        self._cache_dir = cache_dir

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Convert the page behind the row's URL. The page's own title names the mirror before its first heading does.

        Args:
            entry: The web page row to resolve.

        Returns:
            The page as markdown with its title.

        Raises:
            ResolveError: The converter cannot render the page, or the page is a bot check or too thin.
        """
        try:
            result = await self._converter.to_markdown(self._cache_dir / f"{entry.key}.html", source_url=entry.url)
        except ConversionError as error:
            raise ResolveError(str(error)) from error
        title = _clean_title(result.title or "", _title_of(result.markdown, fallback=""), fallback=entry.url)
        _refuse_interstitial(title, result.markdown, url=entry.url)
        return Resolved(title=title, markdown=result.markdown, url=entry.url, converter=result.converter)


def _title_of(markdown: str, *, fallback: str) -> str:
    match = _HEADING.search(markdown)
    return match.group(1) if match else fallback


_TITLE_ARTIFACTS = ("![", "[Image blocked", "](")
_MAX_TITLE = 150


def _usable_title(candidate: str) -> bool:
    """Whether `candidate` reads as a title: non-empty, one line's worth, no markdown artifacts in it."""
    stripped = candidate.strip()
    return bool(stripped) and len(stripped) <= _MAX_TITLE and not any(marker in stripped for marker in _TITLE_ARTIFACTS)


def _clean_title(*candidates: str, fallback: str) -> str:
    """The first candidate that reads as a title, else `fallback`: a broken render's junk heading never names a mirror."""
    for candidate in candidates:
        if _usable_title(candidate):
            return candidate.strip()
    return fallback


_POST_INSTRUCTIONS = (
    "You retrieve one X post, named by its URL. Use X search to find exactly that post. Report its "
    "author handle, its date, its full text, and every link it carries. Do not summarize. Copy the "
    "text. Set found to false if the post cannot be located."
)


class PostAnswer(BaseModel):
    """One X post as the post resolver's one-shot agent reports it."""

    model_config = ConfigDict(frozen=True)

    found: bool
    handle: str = ""
    date: datetime | None = None
    text: str = ""
    links: list[str] = []


def _post_agent(resource: XaiResource) -> Agent[None, PostAnswer]:
    """A Grok agent with native X search that retrieves exactly one post named in its prompt."""
    # an output tool would never be called: with X search running, Grok ends its answer with text
    return Agent(build_xai_model(resource), output_type=NativeOutput(PostAnswer), instructions=_POST_INSTRUCTIONS, capabilities=[XSearch()])


class PostResolver:
    """A hand-added X post row: one Grok agent run over `XSearch`, rendered like the X scanner's posts."""

    def __init__(self, resource: XaiResource, *, agent_factory: Callable[[XaiResource], Agent[None, PostAnswer]] = _post_agent) -> None:
        """Store the xAI resource and the factory that builds the one-shot agent.

        Args:
            resource: The xAI connection the agent runs against.
            agent_factory: The factory that builds the agent from the resource. The default builds a Grok agent with X search.
        """
        self._resource = resource
        self._agent_factory = agent_factory

    async def resolve(self, entry: SourceQueueEntry) -> Resolved:
        """Ask the agent for the post at `entry.url`.

        Args:
            entry: The post row to resolve.

        Returns:
            The post as markdown, titled by the start of its text.

        Raises:
            ResolveError: The agent run fails, or the post cannot be found.
        """
        try:
            agent = self._agent_factory(self._resource)
            result = await agent.run(f"Retrieve the X post at {entry.url}.")
        except Exception as error:
            raise ResolveError(str(error)) from error
        answer = result.output
        if not answer.found or not answer.text.strip():
            msg = f"could not retrieve the post at {entry.url}"
            raise ResolveError(msg)
        return render_post(url=entry.url, handle=answer.handle, date=answer.date, text=answer.text, links=answer.links)
