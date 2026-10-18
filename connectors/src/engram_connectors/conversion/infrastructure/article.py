"""Web articles to markdown: fetch the page, keep the main content, drop navigation and boilerplate."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

import httpx
import trafilatura

from core.net import FetchError
from engram.sources.contracts import ConversionError, ConversionResult

if TYPE_CHECKING:
    from pathlib import Path

    from core.net import PageFetcher

_CONVERTER_LABEL = "article/trafilatura"


def extract_article(html: str, *, url: str) -> str | None:
    """Extract a page's main content as markdown.

    Args:
        html: The page's HTML.
        url: The page's URL, which resolves its relative links.

    Returns:
        The main content as markdown, or `None` when trafilatura finds no article in it.
    """
    return trafilatura.extract(html, url=url, output_format="markdown", include_links=True, include_formatting=True)


class _TitleParser(HTMLParser):
    """Collects a page's `<title>` text and an `og:title` meta tag's content, whichever the markup carries."""

    def __init__(self) -> None:
        """Start with neither title found."""
        super().__init__()
        self.og_title: str | None = None
        self._title_parts: list[str] = []
        self._title_done = False
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track entry into `<title>` and read an `og:title` meta tag's `content` attribute."""
        if tag == "title" and not self._title_done:
            self._in_title = True
        elif tag == "meta" and self.og_title is None:
            values = {key.lower(): value for key, value in attrs if value is not None}
            property_name = (values.get("property") or values.get("name") or "").lower()
            if property_name == "og:title":
                self.og_title = values.get("content")

    def handle_endtag(self, tag: str) -> None:
        """Stop collecting `<title>` text once it closes."""
        if tag == "title":
            self._in_title = False
            self._title_done = True

    def handle_data(self, data: str) -> None:
        """Accumulate text found inside `<title>`."""
        if self._in_title:
            self._title_parts.append(data)

    @property
    def title(self) -> str | None:
        """The collected `<title>` text, or `None` when the page carries no title tag."""
        text = "".join(self._title_parts)
        return text or None


def _page_title(html: str) -> str | None:
    """The page's `og:title` meta tag, else its `<title>` tag, with whitespace collapsed to one line."""
    parser = _TitleParser()
    parser.feed(html)
    for candidate in (parser.og_title, parser.title):
        if candidate is None:
            continue
        collapsed = " ".join(candidate.split())
        if collapsed:
            return collapsed
    return None


class ArticleConverter:
    """`DocumentConverter` for a web page: the URL is the source, the path is ignored."""

    def __init__(self, fetcher: PageFetcher) -> None:
        """Store the page fetcher.

        Args:
            fetcher: The fetcher that downloads the page.
        """
        self._fetcher = fetcher

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:
        """Fetch `source_url` and extract its article as markdown, ignoring `pdf_path`.

        Args:
            pdf_path: Unused, the page URL is the source.
            source_url: The page's URL.

        Returns:
            The article's markdown with the page's own title, `page_count` 1 and no assets.

        Raises:
            ConversionError: No `source_url`, the fetch was refused or failed, or nothing extracted.
        """
        del pdf_path
        if source_url is None:
            msg = "article conversion needs the page URL"
            raise ConversionError(msg)
        try:
            html = await self._fetcher.fetch(source_url)
        except (httpx.HTTPError, FetchError) as error:
            msg = f"fetching {source_url} failed: {error}"
            raise ConversionError(msg) from error
        text = extract_article(html, url=source_url)
        if not text or not text.strip():
            msg = f"no article content found at {source_url}"
            raise ConversionError(msg)
        return ConversionResult(markdown=text, page_count=1, converter=_CONVERTER_LABEL, assets={}, title=_page_title(html))
