"""The `ArxivHtmlConverter`: fetch an arXiv paper's HTML version, convert it to markdown, save its figures.

It reads `source_url` and ignores `pdf_path`.
When the URL is not an arXiv link or the HTML version is missing, it raises `ConversionError`.
"""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

import httpx

from core.observability import get_logger
from engram.sources.contracts import ConversionError, ConversionResult

from .fetch import ASSETS_DIR, HTTP_TIMEOUT, absolute_image_url, arxiv_id, asset_name, is_arxiv_image_url
from .markdown import html_to_markdown
from .svg import resolve_data_reference

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)

# Figure references in the converted Markdown. Images whose attributes (id/class/width) pandoc cannot
# express in plain Markdown survive as raw `<img ...>` tags, attribute-free ones become `![alt](src)`
# with the same root-relative `/html/<id>vN/...` src. Both reference the arXiv host, dead in Obsidian.
_IMG_TAG = re.compile(r"<img\b[^>]*?/?>", re.IGNORECASE)
_IMG_SRC = re.compile(r'\bsrc="([^"]+)"', re.IGNORECASE)
# Localise every Markdown image whose src is a data: URI or anything non-absolute (root-relative
# `/html/...`, version-prefixed `<id>vN/...`, bare `x9.png`). Absolute http(s) srcs are left
# alone, they are either external or our own degraded-download fallbacks.
_MD_IMG = re.compile(r"!\[[^\]]*\]\((data:[^)\s]+|(?!https?://)[^)\s]+)\)")


class ArxivHtmlConverter:
    """`DocumentConverter` that fetches the arXiv HTML version and converts it to markdown with pandoc.

    The output keeps the author's LaTeX as `$...$` math and drops ar5iv's page structure.
    Tables come out as pipe tables and numeric-only math becomes plain text.
    The markdown renders without any Obsidian or ar5iv support.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        """Store an injected async client, or remember to build (and close) our own.

        Args:
            client: Optional preconfigured async client. When `None`, each conversion
                opens a redirect-following client and closes it before returning.
        """
        self._client = client

    async def to_markdown(self, pdf_path: Path, *, source_url: str | None = None) -> ConversionResult:  # noqa: ARG002 (part of the DocumentConverter port, this adapter converts the HTML, not the PDF)
        """Convert the arXiv HTML version named by `source_url` to markdown, ignoring `pdf_path`.

        Each image the paper embeds is downloaded and returned in `ConversionResult.assets`,
        keyed by an `_assets/<id>/<name>` path the body's image reference now points at.
        An image that fails to download keeps its absolute arXiv URL and never fails the conversion.

        Args:
            pdf_path: Unused.
            source_url: The item's web URL. It must resolve to an arXiv id.

        Returns:
            The markdown with `page_count` 0 and the downloaded figure assets.

        Raises:
            ConversionError: If `source_url` is not an arXiv reference, the
                HTML version cannot be fetched or is unavailable (non-200), pandoc
                fails, or the conversion is empty.
        """
        identifier = arxiv_id(source_url)
        if identifier is None:
            msg = f"Not an arXiv source_url, cannot use HTML route: {source_url!r}"
            raise ConversionError(msg)

        if self._client is not None:
            return await self._convert(self._client, identifier)
        async with httpx.AsyncClient(follow_redirects=True, timeout=HTTP_TIMEOUT) as client:
            return await self._convert(client, identifier)

    async def _convert(self, client: httpx.AsyncClient, identifier: str) -> ConversionResult:
        """Fetch, convert and localise figures over one client."""
        html = await self._fetch(client, identifier)
        markdown, math_stash = html_to_markdown(html)
        markdown, assets = await self._localise_images(client, markdown, identifier, math_stash)
        if not markdown.strip():
            msg = f"arXiv HTML for {identifier} converted to empty Markdown"
            raise ConversionError(msg)
        return ConversionResult(markdown=markdown, page_count=0, converter="arxiv-html/pandoc", assets=assets)

    @staticmethod
    async def _fetch(client: httpx.AsyncClient, identifier: str) -> str:
        """GET the arXiv HTML version, raising `ConversionError` when the request fails or answers non-200."""
        url = f"https://arxiv.org/html/{identifier}"
        try:
            response = await client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as error:
            msg = f"arXiv HTML version could not be fetched for {identifier}: {error}"
            raise ConversionError(msg) from error
        if response.status_code != httpx.codes.OK:
            msg = f"arXiv HTML version unavailable for {identifier} (HTTP {response.status_code})"
            raise ConversionError(msg)
        return response.text

    async def _localise_images(
        self, client: httpx.AsyncClient, markdown: str, identifier: str, math_stash: list[str]
    ) -> tuple[str, dict[str, bytes]]:
        """Download every referenced figure and point its reference at a local `_assets/` path."""
        references: list[tuple[str, str]] = []  # (full matched text, src)
        for tag in _IMG_TAG.findall(markdown):
            src_match = _IMG_SRC.search(tag)
            if src_match is not None:
                references.append((tag, src_match.group(1)))
        references.extend((match.group(0), match.group(1)) for match in _MD_IMG.finditer(markdown))

        assets: dict[str, bytes] = {}
        replacements: dict[str, str] = {}
        for full, src in references:
            if full in replacements:
                continue
            if src.startswith("data:"):
                resolved = resolve_data_reference(full, src, markdown, identifier, math_stash)
                if resolved is None:
                    continue
                replacements[full], asset = resolved
                if asset is not None:
                    assets[asset[0]] = asset[1]
                continue
            url, data = await self._fetch_remote_image(client, src, identifier)
            if data is None:
                replacements[full] = f"![]({url})"
                continue
            key = f"{ASSETS_DIR}/{identifier}/{asset_name(src)}"
            assets[key] = data
            # Empty alt on purpose: the visible caption is the adjacent "Figure N:" paragraph, and
            # caption-from-alt renderers would otherwise display the asset filename as a caption.
            replacements[full] = f"![]({key})"

        for full, replacement in replacements.items():
            markdown = markdown.replace(full, replacement)
        return markdown, assets

    async def _fetch_remote_image(self, client: httpx.AsyncClient, src: str, identifier: str) -> tuple[str, bytes | None]:
        """Download a remote figure as `(resolved_url, bytes_or_None)`, retrying a bare src under `/html/<id>/`."""
        url = absolute_image_url(src, identifier)
        data = await self._fetch_image(client, url)
        if data is None and not src.startswith(("http://", "https://", "/")):
            fallback = f"https://arxiv.org/html/{identifier}/{src}"
            if fallback != url:
                data = await self._fetch_image(client, fallback)
                if data is not None:
                    return fallback, data
        return url, data

    @staticmethod
    async def _fetch_image(client: httpx.AsyncClient, url: str) -> bytes | None:
        """GET one figure's bytes, or `None` with a warning on an HTTP failure or a host off arXiv."""
        if not is_arxiv_image_url(url):
            _logger.warning("Figure download blocked for disallowed host {url}. Keeping remote reference", url=url)
            return None
        try:
            response = await client.get(url, follow_redirects=True, timeout=HTTP_TIMEOUT)
        except httpx.HTTPError as error:
            _logger.warning("Figure download failed for {url} ({reason}). Keeping remote reference", url=url, reason=str(error))
            return None
        if response.status_code != httpx.codes.OK:
            _logger.warning("Figure download got HTTP {status} for {url}. Keeping remote reference", status=response.status_code, url=url)
            return None
        if not is_arxiv_image_url(str(response.url)):
            _logger.warning("Figure download redirected off-host to {url}. Keeping remote reference", url=str(response.url))
            return None
        return response.content
