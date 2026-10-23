"""The source queue's rows, the rules that classify and key a URL, and the identifiers that name a source.

A row is a URL somebody wants in the knowledge base, what type of thing it is, and where it
stands. A URL's source type and key come from the knowledge base's own connectors, asked in
declared order. The only hosts read here are the identifier schemes `arxiv_id_of` and `doi_of`
name, and neither claims a URL. `source_identities` names one source by those schemes and its URL.
"""

from __future__ import annotations

import hashlib
import re

from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

_ARXIV_ID = re.compile(r"(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?")
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org"})
_ARXIV_DOI = re.compile(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)


class SourceType(StrEnum):
    """What a URL turns out to be, decided from the URL alone."""

    VIDEO = "video"
    PAPER = "paper"
    POST = "post"
    ARTICLE = "article"


class SourceQueueStatus(StrEnum):
    """Where one row stands in the sync loop.

    `seen` is a row recorded with its reason and never fetched by a sync, until a decision
    moves it. `removed` is terminal: no decision, retry or sync moves a row out of it.
    """

    REQUESTED = "requested"
    RESOLVED = "resolved"
    FAILED = "failed"
    SKIPPED = "skipped"
    SEEN = "seen"
    REMOVED = "removed"


NOT_SERVED = frozenset({SourceQueueStatus.FAILED, SourceQueueStatus.SKIPPED, SourceQueueStatus.SEEN, SourceQueueStatus.REMOVED})
"""The statuses a row can hold and contribute no item: failed, skipped, seen (recorded, never fetched), and removed."""


class SourceQueueEntry(BaseModel):
    """One row: the URL, its source type, its key (the mirror's join key), its status, and how it was found."""

    model_config = ConfigDict(frozen=True)

    key: str
    url: str
    source_type: SourceType
    status: SourceQueueStatus
    reason: str | None = None
    found_via: str | None = None
    found_at: datetime | None = None


SourceQueueDecision = Literal["fetch", "drop"]
"""What the owner can do to a row: `fetch` makes it requested, `drop` makes it skipped."""


class Resolved(BaseModel):
    """One row's resolution: the rendered markdown and the metadata its cache sidecar carries.

    It is the one shape shared by whatever renders a row into the cache (a connector's own
    resolvers, or a scanner rendering a fetched post straight into it) and whatever reads the
    cache back later (a connector's own re-serve path). Neither side depends on the other.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    markdown: str
    url: str
    channel: str | None = None
    published: date | None = None
    converter: str


class UrlClaimant(Protocol):
    """The slice of `Connector` classification needs: recognizing its own URLs and their keys.

    Every `Connector` satisfies this structurally through its `claims` and `key_for` methods,
    so a caller holding a knowledge base's real, resolved connectors passes them straight through.
    """

    def claims(self, url: str) -> SourceType | None:
        """The source type this claimant would yield for `url`, or `None` when the URL is not its host.

        Args:
            url: The URL to recognize.

        Returns:
            The source type, or `None` for a URL on another host.
        """
        ...

    def key_for(self, url: str) -> str:
        """The stable row and mirror key for `url`, once `claims` has already returned this claimant's source type for it.

        Args:
            url: A URL this claimant claims.

        Returns:
            The key the URL's row and mirror share.
        """
        ...


def arxiv_id_of(url: str) -> str | None:
    """The arXiv paper id, without its version, in an arxiv.org abs, pdf or html URL.

    An identifier-scheme reader, not a host claimant: it names a paper page's citation identity
    whether or not any declared connector recognizes arxiv.org URLs.

    Args:
        url: Any URL.

    Returns:
        The paper id, or `None` for any other URL.
    """
    parts = urlsplit(url)
    if parts.netloc.lower().removeprefix("www.") == "arxiv.org" and (match := _ARXIV_ID.search(parts.path)):
        return match.group(1)
    return None


def doi_of(url: str) -> str | None:
    """The DOI a doi.org URL names (`https://doi.org/10.1000/xyz` -> `10.1000/xyz`).

    An identifier-scheme reader, not a host claimant, like `arxiv_id_of`.

    Args:
        url: Any URL.

    Returns:
        The DOI, or `None` for any other URL.
    """
    parts = urlsplit(url)
    if parts.netloc.lower().removeprefix("www.") in _DOI_HOSTS and parts.path.strip("/"):
        return parts.path.strip("/")
    return None


def source_identities(*, url: str | None, doi: str | None) -> frozenset[str]:
    """The identifiers that name one source whatever connector brought it: its URL, its arXiv id and its DOI.

    Each identifier carries its scheme (`url:`, `arxiv:`, `doi:`), so values of two schemes never
    match. A DOI compares in lower case, whether given as a DOI or inside a doi.org URL, and an
    arXiv DOI (`10.48550/arXiv.<id>`) also names its arXiv id. A `None` or blank value names nothing.

    Args:
        url: The source's URL.
        doi: The source's DOI.

    Returns:
        The identifiers.
    """
    identities: set[str] = set()
    dois: list[str] = []
    if url is not None and url.strip():
        identities.add(f"url:{url.strip()}")
        if (arxiv_id := arxiv_id_of(url)) is not None:
            identities.add(f"arxiv:{arxiv_id}")
        if (url_doi := doi_of(url)) is not None:
            dois.append(url_doi)
    if doi is not None and doi.strip():
        dois.append(doi.strip())
    for named in dois:
        identities.add(f"doi:{named.lower()}")
        if (match := _ARXIV_DOI.fullmatch(named)) is not None:
            identities.add(f"arxiv:{match.group(1)}")
    return frozenset(identities)


def classify_url(url: str, *, connectors: Sequence[UrlClaimant]) -> tuple[SourceType, str]:
    """The source type the knowledge base's registered connectors claim for `url`, and the stable key its row and mirror share.

    Each connector is asked in turn, in the knowledge base's own declared order. The first to
    claim the URL decides its source type, and that same connector's `key_for` decides its key.
    There is no default connector: a caller with none to offer passes an empty sequence. When no
    connector claims the URL, it is an article keyed by `url_key`, so the same URL always lands on
    the same row.

    Args:
        url: The URL to classify.
        connectors: The knowledge base's connectors, in declared order.

    Returns:
        The source type and the key.
    """
    for claimant in connectors:
        source_type = claimant.claims(url)
        if source_type is not None:
            return source_type, claimant.key_for(url)
    return SourceType.ARTICLE, url_key(url)


def url_key(url: str) -> str:
    """The generic row key for a URL no connector keys more specifically: `url-` plus its sha256's first sixteen hex characters.

    A connector's own `key_for` may fall back to it for a URL it claims but has no narrower
    identifier for.

    Args:
        url: Any URL.

    Returns:
        The key.
    """
    return "url-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
