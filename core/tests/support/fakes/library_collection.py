"""A whole small library collection: the ingest writes into the same pages the rows read from, so a save is read back."""

from __future__ import annotations

import json
import re

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from core.ai.workflows.infrastructure.library_collection import (
    CONSUMERS_SQL,
    FETCH_SQL,
    LIVE_STEP_VERSIONS_SQL,
    LIVE_VERSIONS_SQL,
    PAGE_SQL,
    STEP_PAGES_SQL,
    SUPERSEDED_SQL,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

COLUMNS = ("id", "type", "title", "summary", "status")
"""The modelled keys `api.cards` answers as columns, outside the `frontmatter` column."""


def pruned(value: object) -> object:
    """What engram stores for one frontmatter value: `None` and empty containers dropped at every depth."""
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        items = [kept for kept in (pruned(item) for item in value) if kept is not None]
        return items or None
    if isinstance(value, Mapping):
        nested = {str(key): kept for key, kept in ((key, pruned(item)) for key, item in value.items()) if kept is not None}
        return nested or None
    return None


def _like(pattern: str) -> re.Pattern[str]:
    """SQL `like`: `%` is any run of characters and `_` is any one character."""
    return re.compile("".join(".*" if char == "%" else "." if char == "_" else re.escape(char) for char in pattern))


def _contains(record: object, wanted: object) -> bool:
    """What jsonb `@>` answers for a list of objects: every wanted object is a sub-object of one held object."""
    if not isinstance(record, list) or not isinstance(wanted, list):
        return False
    return all(
        isinstance(item, Mapping) and any(isinstance(held, Mapping) and all(held.get(key) == value for key, value in item.items()) for held in record)
        for item in wanted
    )


class FakeCollection:
    """The rows and the ingest of one collection over one dict of pages, shaped the way `api.cards` answers.

    `pages` holds each page by id: the `COLUMNS` keys, `superseded`, the remaining `frontmatter` and the
    `applicability`. With `prune` on, the ingest drops `None` and empty containers at every depth before it stores, as
    engram's ingest does. `payloads` keeps every payload the ingest was handed and `sent` every statement read, in
    order. A statement the fake does not answer fails the test.
    """

    def __init__(self, *, collection: str = "workflows", prune: bool = False) -> None:
        self.collection = collection
        self.prune = prune
        self.pages: dict[str, dict[str, Any]] = {}
        self.payloads: list[Mapping[str, Any]] = []
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def ingest(self, payload: Mapping[str, Any], *, collection: str) -> None:
        """Store the payload's page, as the knowledge base's validated write path would."""
        assert collection == self.collection
        self.payloads.append(payload)
        authored = dict(payload["frontmatter"])
        long_tail = {key: value for key, value in authored.items() if key not in (*COLUMNS, "superseded_by")}
        if self.prune:
            long_tail = {key: kept for key, kept in ((key, pruned(value)) for key, value in long_tail.items()) if kept is not None}
        self.pages[str(authored["id"])] = {
            **{key: authored[key] for key in COLUMNS},
            "superseded": authored.get("superseded_by") is not None,
            "frontmatter": long_tail,
            "applicability": payload["sections"][0]["body"],
        }

    def written(self, page_id: str) -> Mapping[str, Any]:
        """The frontmatter the last write of `page_id` handed the ingest."""
        return next(payload["frontmatter"] for payload in reversed(self.payloads) if payload["frontmatter"]["id"] == page_id)

    def _live(self, page_type: str) -> list[dict[str, Any]]:
        return [page for page in self.pages.values() if page["type"] == page_type and "archived_at" not in page["frontmatter"] and not page["superseded"]]

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        """Answer the library's row statements from `pages`."""
        self.sent.append((sql, args))
        if sql == STEP_PAGES_SQL:
            names = args[1]
            assert isinstance(names, list)
            return [
                {"id": page["id"], "step": json.dumps(page["frontmatter"]["step"])}
                for page in self.pages.values()
                if page["type"] == "step" and page["frontmatter"]["step"]["name"] in names
            ]
        if sql in (LIVE_STEP_VERSIONS_SQL, LIVE_VERSIONS_SQL):
            like = _like(str(args[1]))
            return [{"id": page["id"]} for page in self._live("step" if sql == LIVE_STEP_VERSIONS_SQL else "workflow") if like.fullmatch(str(page["id"]))]
        if sql == CONSUMERS_SQL:
            assert args[0] == self.collection
            wanted = json.loads(str(args[1]))
            return [
                {"id": page["id"], "reusable_steps": json.dumps(page["frontmatter"]["reusable_steps"])}
                for page in self._live("workflow")
                if _contains(page["frontmatter"].get("reusable_steps"), wanted)
            ]
        page = self.pages.get(str(args[0]))
        if sql == SUPERSEDED_SQL:
            return [{"superseded": page["superseded"]}] if page is not None else []
        assert sql == PAGE_SQL, sql
        return [page] if page is not None else []

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        """Answer the library's one-value statement, a page's frontmatter, from `pages`."""
        self.sent.append((sql, args))
        assert sql == FETCH_SQL, sql
        page = self.pages.get(str(args[0]))
        return page["frontmatter"] if page is not None else None
