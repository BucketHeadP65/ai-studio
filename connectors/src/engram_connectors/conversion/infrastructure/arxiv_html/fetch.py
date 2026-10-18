"""arXiv id parsing, image URL resolution, the figure download host guard and figure asset names."""

from __future__ import annotations

import re

from urllib.parse import urljoin, urlsplit

import httpx

HTTP_TIMEOUT = httpx.Timeout(30.0)
"""The bound on one arXiv HTTP fetch."""

_ARXIV_ID = r"\d{4}\.\d{4,5}"
_ARXIV_URL = re.compile(rf"arxiv\.org/(?:abs|pdf|html)/({_ARXIV_ID})(?:v\d+)?", re.IGNORECASE)
_ARXIV_DOI = re.compile(rf"10\.48550/arxiv\.({_ARXIV_ID})(?:v\d+)?", re.IGNORECASE)

# The version-qualified prefix ar5iv puts on image srcs (with or without the `/html/` part,
# depending on src shape), stripped to name assets by their in-paper path.
_HTML_PREFIX = re.compile(rf"^(?:/html/)?{_ARXIV_ID}(?:v\d+)?/", re.IGNORECASE)
ASSETS_DIR = "_assets"
"""The folder name, inside a paper's note folder, that holds its figures."""


def arxiv_id(source_url: str | None) -> str | None:
    """Extract the bare arXiv id (e.g. `2412.15605`) from an arXiv URL or DOI.

    Handles `arxiv.org/abs/<id>`, `/pdf/<id>` and `/html/<id>` (with an
    optional `vN` suffix) and the DOI form `.../10.48550/arXiv.<id>`.

    Args:
        source_url: The item's web URL, or `None`.

    Returns:
        The version-stripped arXiv id, or `None` when `source_url` is
        `None` or is not an arXiv reference.
    """
    if source_url is None:
        return None
    match = _ARXIV_URL.search(source_url) or _ARXIV_DOI.search(source_url)
    return match.group(1) if match else None


def absolute_image_url(src: str, identifier: str) -> str:
    """Resolve an image src to an absolute arXiv URL exactly as a browser would from the page URL.

    Args:
        src: The image's `src` attribute.
        identifier: The arXiv paper id the page belongs to.

    Returns:
        The absolute URL.
    """
    return urljoin(f"https://arxiv.org/html/{identifier}", src)


def is_arxiv_image_url(url: str) -> bool:
    """Whether `url` is an https URL on `arxiv.org` or one of its subdomains.

    Args:
        url: The URL to test.

    Returns:
        `True` for an https URL on arXiv, else `False`.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "arxiv.org" or host.endswith(".arxiv.org")


def asset_name(src: str) -> str:
    """A flat, filesystem-safe asset filename from an image src (in-paper subdirs flattened with `-`).

    Args:
        src: The image's `src` attribute.

    Returns:
        The file name, or `image` when nothing is left of the path.
    """
    path = re.sub(r"^https?://[^/]+", "", src)
    path = _HTML_PREFIX.sub("", path).lstrip("/")
    return path.replace("/", "-") or "image"
