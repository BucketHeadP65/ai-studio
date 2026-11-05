"""A whole small library collection: the ingest writes into the same pages the rows read from, so a save is read back."""

from __future__ import annotations

import json
import re

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from core.ai.workflows.infrastructure.library_collection import (
    ALL_VERSIONS_SQL,
    CONSUMERS_SQL,
    FETCH_SQL,
    FLAG_SQL,
    FLAGS_SQL,
    LIVE_STEP_VERSIONS_SQL,
    LIVE_VERSIONS_SQL,
    PAGE_SQL,
    STEP_PAGES_SQL,
    SUPERSEDED_SQL,
)
from core.markdown import heading_slug

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.cqrs.contracts import Event

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

    `pages` holds each page by id: the `COLUMNS` keys, `superseded`, the remaining `frontmatter`, the body of each
    section by its slug under `sections`, and the `applicability`. With `prune` on, the ingest drops `None` and empty
    containers at every depth before it stores, as engram's ingest does. The ingest refuses a page whose type is in
    `refused_types`. `payloads` keeps every payload the ingest was handed, `events` every event handed with one,
    beside the id of the page it came with, and `sent` every statement read, in order. A statement the fake does not
    answer fails the test.
    """

    def __init__(self, *, collection: str = "workflows", prune: bool = False) -> None:
        self.collection = collection
        self.prune = prune
        self.refused_types: frozenset[str] = frozenset()
        self.pages: dict[str, dict[str, Any]] = {}
        self.payloads: list[Mapping[str, Any]] = []
        self.events: list[tuple[str, Event]] = []
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def ingest(self, payload: Mapping[str, Any], *, collection: str, events: Sequence[Event] = ()) -> None:
        """Store the payload's page and keep the events handed with it, as the knowledge base's validated write path would."""
        assert collection == self.collection
        authored = dict(payload["frontmatter"])
        if authored["type"] in self.refused_types:
            msg = f"refused a {authored['type']} page"
            raise RuntimeError(msg)
        self.payloads.append(payload)
        self.events.extend((str(authored["id"]), event) for event in events)
        long_tail = {key: value for key, value in authored.items() if key not in (*COLUMNS, "superseded_by")}
        if self.prune:
            long_tail = {key: kept for key, kept in ((key, pruned(value)) for key, value in long_tail.items()) if kept is not None}
        sections = {heading_slug(section["heading"]): section["body"] for section in payload["sections"]}
        self.pages[str(authored["id"])] = {
            **{key: authored[key] for key in COLUMNS},
            "superseded": authored.get("superseded_by") is not None,
            "frontmatter": long_tail,
            "sections": sections,
            "applicability": sections.get("applicability", ""),
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
        if sql in (LIVE_STEP_VERSIONS_SQL, LIVE_VERSIONS_SQL, ALL_VERSIONS_SQL):
            like = _like(str(args[1]))
            page_type = "step" if sql == LIVE_STEP_VERSIONS_SQL else "workflow"
            held = [page for page in self.pages.values() if page["type"] == page_type] if sql == ALL_VERSIONS_SQL else self._live(page_type)
            return [{"id": page["id"]} for page in held if like.fullmatch(str(page["id"]))]
        if sql == CONSUMERS_SQL:
            assert args[0] == self.collection
            wanted = json.loads(str(args[1]))
            return [
                {"id": page["id"], "reusable_steps": json.dumps(page["frontmatter"]["reusable_steps"])}
                for page in self._live("workflow")
                if _contains(page["frontmatter"].get("reusable_steps"), wanted)
            ]
        if sql in (FLAG_SQL, FLAGS_SQL):
            return self._flags(sql, args)
        page = self.pages.get(str(args[0]))
        if sql == SUPERSEDED_SQL:
            return [{"superseded": page["superseded"]}] if page is not None else []
        assert sql == PAGE_SQL, sql
        return [page] if page is not None else []

    def _flags(self, sql: str, args: tuple[object, ...]) -> Sequence[Mapping[str, Any]]:
        """Answer `FLAG_SQL` with the one page of that id, of any type, and `FLAGS_SQL` with the flag pages it filters, newest first."""
        assert args[0] == self.collection
        if sql == FLAG_SQL:
            page = self.pages.get(str(args[1]))
            return [self._flag_row(page)] if page is not None else []
        status, kind = args[1], args[2]
        flags = [
            self._flag_row(page)
            for page in self.pages.values()
            if page["type"] == "maintenance-flag"
            and (status is None or page["status"] == status)
            and (kind is None or page["frontmatter"].get("flag_kind") == kind)
        ]
        flags.sort(key=lambda row: str(row["id"]))
        flags.sort(key=lambda row: datetime.fromisoformat(str(row["frontmatter"]["raised_at"])), reverse=True)
        return flags

    @staticmethod
    def _flag_row(page: Mapping[str, Any]) -> Mapping[str, Any]:
        """A page in the columns `FLAG_SQL` answers: the card columns, the frontmatter and the two flag sections."""
        sections = page.get("sections", {})
        return {
            **{key: page[key] for key in COLUMNS},
            "frontmatter": page["frontmatter"],
            "evidence": sections.get("evidence", ""),
            "proposed_change": sections.get("proposed_change", ""),
        }

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        """Answer the library's one-value statement, a page's frontmatter, from `pages`."""
        self.sent.append((sql, args))
        assert sql == FETCH_SQL, sql
        page = self.pages.get(str(args[0]))
        return page["frontmatter"] if page is not None else None
