"""Atom and RSS 2.0 parsing with the standard library, enough for the documents a watch list names."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from engram.sources.domain.watch_list import FoundSource

_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_atom_or_rss(xml_text: str) -> list[FoundSource]:
    """Entries of an Atom document or items of an RSS 2.0 channel, in document order.

    A well-formed document with an unrecognized root parses to an empty list.

    Args:
        xml_text: The document's text.

    Returns:
        The sources it lists.

    Raises:
        xml.etree.ElementTree.ParseError: `xml_text` is not well-formed XML.
    """
    root = ET.fromstring(xml_text)  # noqa: S314 (documents are fetched from owner-chosen URLs, defusedxml is not a dependency)
    if root.tag == f"{_ATOM}feed":
        return [entry for entry in (_atom_entry(node) for node in root.findall(f"{_ATOM}entry")) if entry is not None]
    if root.tag == "rss":
        return [item for item in (_rss_item(node) for node in root.iter("item")) if item is not None]
    return []


def _atom_entry(node: ET.Element) -> FoundSource | None:
    link = next((el.get("href") for el in node.findall(f"{_ATOM}link") if el.get("rel", "alternate") == "alternate"), None)
    if not link:
        return None
    title = (node.findtext(f"{_ATOM}title") or "").strip() or link
    return FoundSource(url=link, title=title, published=_iso(node.findtext(f"{_ATOM}published") or node.findtext(f"{_ATOM}updated")))


def _rss_item(node: ET.Element) -> FoundSource | None:
    link = (node.findtext("link") or "").strip()
    if not link:
        return None
    title = (node.findtext("title") or "").strip() or link
    return FoundSource(url=link, title=title, published=_rfc822(node.findtext("pubDate")))


def _iso(text: str | None) -> datetime | None:
    """An Atom timestamp as a UTC-aware datetime. One carrying no offset is read as UTC."""
    if not text:
        return None
    try:
        return _utc(datetime.fromisoformat(text.strip()))
    except ValueError:
        return None


def _rfc822(text: str | None) -> datetime | None:
    """An RSS `pubDate` as a UTC-aware datetime. `-0000`, which `parsedate_to_datetime` reads as naive, is UTC."""
    if not text:
        return None
    try:
        return _utc(parsedate_to_datetime(text.strip()))
    except (TypeError, ValueError):
        return None


def _utc(moment: datetime) -> datetime:
    """`moment` in UTC, taking a value with no offset as already being UTC."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
