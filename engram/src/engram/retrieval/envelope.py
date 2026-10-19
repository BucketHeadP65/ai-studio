r"""`EnvelopeHandler`, engram's provider of `DisclosureEnvelope`s from the indexed store.

Given a ref, it returns that unit's envelope: its own disclosed body plus one-line summaries of its
children, never their bodies. A ref is `collection:<name>` (the root map), `group:<collection>/<value>`
(one group of the map when the spec groups it), `page:<page_id>` or `section:<section_id>`. Any other
ref raises `BadRefError`.

Wire the handler to the reader gateway. It reads only through the `api.envelope_*` functions.

A flat map that would exceed the row cap is served grouped by page type instead, with a note in
`content`. A declared `group_by` is never overridden. Under `group_by="app"`, an app group that would
exceed the cap is served as page-type sub-groups, `group:<collection>/<app>/<page_type>`. Page-type
groups and sub-groups truncate with a sentinel child. Listed children are not filtered by page status,
so a caller must not assume every listed unit is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn

from core.ai.disclosure.contracts import ChildSummary, DisclosureEnvelope, DisclosureMode

from .exceptions import _REGROUND_WITH_SEARCH, BadRefError, GroupUnknownError, WrongCollectionError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .domain import Row, SqlGateway, SqlResult

GroupByKey = Literal["app", "page_type"]
"""The keys `EnvelopeHandler` can group a collection map by, the values of `RetrievalSpec.group_by`."""

RefType = Literal["collection", "group", "page", "section", "unit"]
"""The kinds of unit a retrieval ref can name."""

_REF_TYPES = frozenset({"collection", "group", "page", "section", "unit"})


@dataclass(frozen=True)
class ResolvedRef:
    """A parsed retrieval handle with its owning collection separated from the bare unit id."""

    collection: str
    kind: RefType
    unit_id: str

# `group:<collection>/<value>` has 2 segments. `group:<collection>/<app>/<page_type>` has 3, valid under `group_by="app"`.
_GROUP_REF_SEGMENTS = 2
_APP_PAGE_TYPE_REF_SEGMENTS = 3


def fetch_description(collection: str, *, mode: DisclosureMode = "strict", can_search: bool = True) -> str:
    """The leveled `fetch` tool's description for `collection` under its declared `mode`.

    Pass the knowledge base's own `retrieval.collection` and the handler's `mode`. There is no default
    collection, and a `search_only` description advertises no browsable map.

    Args:
        collection: The knowledge base's `retrieval.collection` (named as the starting map under `strict`).
        mode: The collection's declared disclosure mode (`EnvelopeHandler.mode`).
        can_search: Whether the surface registering this `fetch` also registers `search`. `False`
            drops every mention of `search` as a way to find an id.

    Returns:
        The full tool description text.
    """
    if mode == "search_only":
        discovery = (
            "This knowledge base has no browsable map: find things with `search` first, then fetch a hit's id."
            if can_search
            else "This knowledge base has no browsable map, and no `search` tool is registered here: fetch an id "
            "supplied with the task or reached by `graph_expand`/`find_paths`."
        )
        return (
            "Read a unit by id. Results are leveled: you get the unit's own content plus a one-line "
            f"summary of everything directly inside it, each with the id to read next. {discovery} "
            f"{fetchable_forms(collection)} A small page "
            "arrives whole (its text in `content`). A larger one lists its sections to choose from. "
            "Read the summaries before going deeper. Every level costs tokens, and once you have "
            "chosen several sections, call fetch for each in the same message: they run together."
        )
    grounding = (
        ". Ids from `search` hits work too"
        if can_search
        else ". Ids reached by `graph_expand`/`find_paths`, or supplied with the task, work too"
    )
    return (
        "Read a unit by id. Results are leveled: you get the unit's own content plus a one-line "
        "summary of everything directly inside it, each with the id to read next. Call it with no "
        f"arguments to open the '{collection}' map and see what this knowledge base contains{grounding}. "
        f"{fetchable_forms(collection)} A small page arrives whole (its text in `content`). A larger one "
        "lists its sections to choose from. Read the summaries before going deeper. Every level "
        "costs tokens, and once you have chosen several sections, call fetch for each in the same "
        "message: they run together."
    )


def flat_fetch_description(collection: str | None = None) -> str:
    """The `fetch` tool's description when reads are flat: full text by id, no levels, no map.

    Args:
        collection: The one knowledge base the reads resolve within, named in the id forms. `None` names a
            stand-in, for a reader over several knowledge bases.

    Returns:
        The full tool description text.
    """
    return (
        "Read a unit's full text by id. `unit_id` is an id from a `search` hit: a page id reads the "
        "whole page, a section id (the ones with `#`) reads that section. "
        f"{fetchable_forms(collection)} There is no browsable map "
        "here: find units with `search` first, then fetch a hit's id."
    )


def library_fetch_description(names: Sequence[str], *, cross_search: bool = False) -> str:
    """The leveled `fetch` tool's description when one tool reads several knowledge bases.

    Args:
        names: The knowledge_bases's names, in the order they should be listed.
        cross_search: Whether one read may cross knowledge bases. `False` makes `knowledge_base` a required
            argument that scopes the read, so the text names it instead of offering a no-argument
            listing of the bases.

    Returns:
        The full tool description text.
    """
    listed = ", ".join(f"'{name}'" for name in names)
    opening = (
        f"`knowledge_base` names the knowledge base to read and is required ({listed}). `unit_id` is the id "
        "within it, and no `unit_id` opens that base's map. Ids from `search` hits work here."
        if not cross_search
        else f"Call it with no arguments to list your knowledge bases ({listed}). A knowledge base's name opens its map, "
        "and ids from `search` hits work too."
    )
    return (
        "Read a unit by id. Results are leveled: you get the unit's own content plus a one-line "
        f"summary of everything directly inside it, each with the id to read next. {opening} "
        f"{fetchable_forms()} A small page arrives whole (its text in `content`). A larger "
        "one lists its sections to choose from. Read the summaries before going deeper. Every level "
        "costs tokens, and once you have chosen several sections, call fetch for each in the same "
        "message: they run together."
    )


def fetchable_forms(collection: str | None = None) -> str:
    """The sentence naming every id form `fetch` takes, for its descriptions and its not-found refusals.

    Args:
        collection: The knowledge base the qualified forms name. `None` names a stand-in, for a reader
            over several knowledge bases.

    Returns:
        One sentence naming the `page:`, `section:` and `unit:` refs and the bare id.
    """
    owner = "<collection>" if collection is None else collection
    return (
        f"`unit_id` takes a `ref` exactly as another tool returned it, such as 'page:{owner}/<id>', "
        f"'section:{owner}/<id>' or 'unit:{owner}/<id>', or the bare id after the slash."
    )


def normalize_ref(unit_id: str, collection: str, *, reground: str = _REGROUND_WITH_SEARCH) -> str:
    """The envelope ref for whatever id form an agent hands `fetch`.

    It accepts ids from `search` hits (`mu.x`, `mu.x#s1`) and from fetch listings (`page:mu.x`,
    `section:mu.x#s1`). An explicit `collection:`, `page:` or `section:` ref passes through. The bare
    collection name opens the map. An id containing `#` is a section, and anything else is a page.

    Args:
        unit_id: The id an agent handed `fetch`.
        collection: The knowledge base `unit_id` resolves within when it carries no collection of its own.
        reground: How a malformed-ref refusal tells the agent to get a good id, `` `search` `` (the
            default) unless the caller's surface does not register it.

    Returns:
        The ref in its canonical collection-qualified form.

    Raises:
        BadRefError: `unit_id` is malformed.
    """
    return qualify_ref(unit_id, collection, reground=reground)


def parse_ref(ref: str, default_collection: str, *, reground: str = _REGROUND_WITH_SEARCH) -> ResolvedRef:
    """Accept a bare or collection-qualified retrieval handle and separate its identity fields.

    Args:
        ref: The handle to parse.
        default_collection: The knowledge base a ref with no collection segment of its own resolves within.
        reground: How a refusal tells the agent to get a good ref, `` `search` `` (the default)
            unless the caller's surface does not register it.

    Returns:
        The ref's collection, kind and bare unit id.

    Raises:
        BadRefError: `ref` is malformed or `default_collection` is empty.
    """
    if not default_collection:
        _bad_ref(ref, reground=reground)
    prefix, separator, raw = ref.partition(":")
    if not separator or prefix not in _REF_TYPES:
        return _bare_ref(ref, default_collection, reground=reground)
    if prefix == "collection":
        return _collection_ref(ref, raw, reground=reground)
    if prefix == "group":
        return _group_ref(ref, raw, reground=reground)
    return _unit_ref(ref, prefix, raw, default_collection, reground=reground)


def _bare_ref(ref: str, default_collection: str, *, reground: str) -> ResolvedRef:
    """Parse an unprefixed id using the caller's knowledge base as its owner."""
    if not ref:
        _bad_ref(ref, reground=reground)
    if ref == default_collection:
        return ResolvedRef(default_collection, "collection", "")
    kind: RefType = "section" if "#" in ref else "page"
    return ResolvedRef(default_collection, kind, ref)


def _collection_ref(ref: str, raw: str, *, reground: str) -> ResolvedRef:
    """Parse a collection root, which has no unit-id segment."""
    if not raw or "/" in raw:
        _bad_ref(ref, reground=reground)
    return ResolvedRef(raw, "collection", "")


def _group_ref(ref: str, raw: str, *, reground: str) -> ResolvedRef:
    """Parse a grouped collection, keeping its empty bucket and nested tail legal."""
    collection, slash, unit_id = raw.partition("/")
    if not collection or not slash:
        _bad_ref(ref, reground=reground)
    return ResolvedRef(collection, "group", unit_id)


def _unit_ref(ref: str, prefix: str, raw: str, default_collection: str, *, reground: str) -> ResolvedRef:
    """Parse a page, section, or flat unit under either explicit or default ownership."""
    collection, slash, unit_id = raw.partition("/")
    if slash:
        if not collection or not unit_id:
            _bad_ref(ref, reground=reground)
        return ResolvedRef(collection, _unit_type(prefix, reground=reground), unit_id)
    if not raw:
        _bad_ref(ref, reground=reground)
    return ResolvedRef(default_collection, _unit_type(prefix, reground=reground), raw)


def qualify_ref(ref: str, owning_collection: str, *, reground: str = _REGROUND_WITH_SEARCH) -> str:
    """Return `ref` in its canonical collection-qualified form for `owning_collection`.

    Args:
        ref: The handle to canonicalize.
        owning_collection: The knowledge base a ref with no collection segment of its own resolves within.
        reground: How a malformed-ref refusal tells the agent to get a good ref, `` `search` `` (the
            default) unless the caller's surface does not register it.

    Returns:
        The canonical ref, such as `page:<collection>/<id>`.

    Raises:
        BadRefError: `ref` is malformed.
    """
    resolved = parse_ref(ref, owning_collection, reground=reground)
    if resolved.kind == "collection":
        return f"collection:{resolved.collection}"
    if resolved.kind == "group":
        return f"group:{resolved.collection}/{resolved.unit_id}"
    return f"{resolved.kind}:{resolved.collection}/{resolved.unit_id}"


def _bad_ref(ref: str, *, reground: str = _REGROUND_WITH_SEARCH) -> NoReturn:
    """Raise the standard actionable malformed-ref error."""
    msg = f"Could not read ref {ref!r}. Re-ground it with {reground} and try again."
    raise BadRefError(msg)


def _unit_type(prefix: str, *, reground: str) -> Literal["page", "section", "unit"]:
    """Narrow a validated non-container ref prefix to its unit-bearing vocabulary."""
    if prefix == "page":
        return "page"
    if prefix == "section":
        return "section"
    if prefix == "unit":
        return "unit"
    _bad_ref(prefix, reground=reground)

# The most children one envelope lists.
_ROW_CAP = 50

# Thin wrappers over the `api.envelope_*` SECURITY DEFINER functions (migration 0012).
_COLLECTION_CHILDREN_SQL = "SELECT * FROM api.envelope_collection(:collection, cast(:excluded_page_types as text)::text[])"
_COLLECTION_GROUPS_SQL = "SELECT * FROM api.envelope_collection_groups(:collection, cast(:excluded_page_types as text)::text[])"
_GROUP_CHILDREN_SQL = "SELECT * FROM api.envelope_group(:collection, :app, cast(:excluded_page_types as text)::text[])"
_COLLECTION_PAGE_TYPES_SQL = "SELECT * FROM api.envelope_collection_page_types(:collection, cast(:excluded_page_types as text)::text[])"
_PAGE_TYPE_CHILDREN_SQL = "SELECT * FROM api.envelope_page_type(:collection, :page_type, cast(:excluded_page_types as text)::text[])"
_GROUP_PAGE_TYPE_GROUPS_SQL = "SELECT * FROM api.envelope_group_page_types(:collection, :app, cast(:excluded_page_types as text)::text[])"
_GROUP_PAGE_TYPE_CHILDREN_SQL = "SELECT * FROM api.envelope_group_page_type(:collection, :app, :page_type, cast(:excluded_page_types as text)::text[])"
_COLLECTION_ABOUT_SQL = "SELECT * FROM api.collection_about(:collection)"
# A page or section id is unique only within its collection, so these read within the handler's own collection.
_PAGE_ENVELOPE_SQL = "SELECT * FROM api.envelope_page(:collection, :page_id)"
_SECTION_ENVELOPE_SQL = "SELECT * FROM api.envelope_section(:collection, :section_id)"

_DEGRADE_NOTE = "Listing exceeds one screen. Showing {count} page_type groups instead: open one to list its pages."
"""The note appended to `content` when a flat or app-grouped listing degrades to page_type stubs."""


class EnvelopeHandler:
    """Renders one `DisclosureEnvelope` from the indexed store for a ref.

    Every envelope is stamped with `mode`. Excluded page types never appear as collection or group
    children. A `search_only` collection withholds the root map and every group listing, while page
    and section refs still serve. A `collection:` or `group:` ref naming another collection than the
    handler's own raises `WrongCollectionError` before any SQL runs. Page and section refs resolve
    within the handler's own collection only.
    """

    def __init__(
        self,
        gateway: SqlGateway,
        *,
        mode: DisclosureMode = "strict",
        excluded_page_types: frozenset[str] = frozenset(),
        group_by: GroupByKey | None = None,
        orientation: str | None = None,
        collection: str | None = None,
    ) -> None:
        """Bind the handler to a gateway and a knowledge base's disclosure declarations.

        Args:
            gateway: The reader gateway every envelope is read through.
            mode: The collection's disclosure mode, `retrieval.disclosure_mode`.
            excluded_page_types: The page types never listed as children.
            group_by: The spec's `retrieval.group_by`. `None` keeps the map flat, `"app"` or `"page_type"`
                lists group stubs and makes `group:<collection>/<value>` a valid ref.
            orientation: Text appended after the collection's about text on the root map, or served
                alone when there is none. `None` serves the about text alone.
            collection: The handler's own knowledge base. `None` trusts every ref's own collection, and
                page and section reads then match nothing.
        """
        self._gateway = gateway
        self.mode: DisclosureMode = mode
        self._excluded_page_types = excluded_page_types
        self._group_by = group_by
        self._orientation = orientation
        self._collection = collection

    async def __call__(self, ref: str) -> DisclosureEnvelope:
        """Parse `ref` and render its envelope.

        An unknown or empty collection answers an envelope with no children, not an error.

        Args:
            ref: The ref to render.

        Returns:
            The unit's envelope.

        Raises:
            BadRefError: `ref` is malformed, or names a page, section or group the store does not have.
            WrongCollectionError: The handler has its own collection and `ref` names another one.
        """
        kind, name = _parse_ref(ref, self._collection)
        canonical_ref = qualify_ref(ref, self._collection) if self._collection is not None else ref
        if kind == "collection":
            return await self._collection_envelope(canonical_ref, name)
        if kind == "group":
            return await self._group_envelope(canonical_ref, name)
        if kind == "page":
            return await self._page_envelope(canonical_ref, name)
        if kind == "section":
            return await self._section_envelope(canonical_ref, name)
        _bad_ref(ref)

    async def _collection_envelope(self, ref: str, name: str) -> DisclosureEnvelope:
        """Render the root map: the about text and orientation, children the pages or group stubs."""
        if self.mode == "search_only":
            return DisclosureEnvelope(ref=ref, kind="collection", title=name, mode=self.mode)
        params = {"collection": name, "excluded_page_types": _array_literal(self._excluded_page_types)}
        degrade_note: str | None = None
        if self._group_by is None:
            children, degrade_note = await self._flat_or_degraded_children(ref, name, params)
        elif self._group_by == "page_type":
            result = await self._gateway.rows(_COLLECTION_PAGE_TYPES_SQL, params, cap=_ROW_CAP)
            children = _with_truncation_notice(tuple(_group_child(name, row, "page_type") for row in result.rows), ref, result)
        else:
            result = await self._gateway.rows(_COLLECTION_GROUPS_SQL, params, cap=_ROW_CAP)
            children = _with_truncation_notice(tuple(_group_child(name, row, "app") for row in result.rows), ref, result)
        about = await self._gateway.rows(_COLLECTION_ABOUT_SQL, {"collection": name}, cap=1)
        content = _append_note(_voice(about, self._orientation), degrade_note)
        return DisclosureEnvelope(ref=ref, kind="collection", title=name, content=content, children=children, mode=self.mode)

    async def _flat_or_degraded_children(
        self, ref: str, collection: str, params: Mapping[str, object]
    ) -> tuple[tuple[ChildSummary, ...], str | None]:
        """The flat page listing, or past the row cap the page-type groups plus a note of the switch."""
        flat = await self._gateway.rows(_COLLECTION_CHILDREN_SQL, params, cap=_ROW_CAP)
        if flat.truncated_message is None:
            return tuple(_child("page", row, collection) for row in flat.rows), None
        page_types = await self._gateway.rows(_COLLECTION_PAGE_TYPES_SQL, params, cap=_ROW_CAP)
        children = _with_truncation_notice(tuple(_group_child(collection, row, "page_type") for row in page_types.rows), ref, page_types)
        return children, _DEGRADE_NOTE.format(count=len(page_types.rows))

    async def _group_envelope(self, ref: str, name: str) -> DisclosureEnvelope:
        """Render one group's pages, or its page-type sub-groups when an app group exceeds the row cap."""
        segments = name.split("/", 2)
        if len(segments) < _GROUP_REF_SEGMENTS:
            msg = f"ref did not match group:<collection>/<value>: {ref!r}"
            raise BadRefError(msg)
        collection, value = segments[0], segments[1]
        page_type = segments[2] if len(segments) == _APP_PAGE_TYPE_REF_SEGMENTS else None
        title = page_type if page_type is not None else (value or "ungrouped")
        if self.mode == "search_only":
            return DisclosureEnvelope(ref=ref, kind="group", title=title, mode=self.mode)
        excluded_page_types = _array_literal(self._excluded_page_types)
        degrade_note: str | None = None
        if self._group_by == "page_type":
            params = {"collection": collection, "page_type": value, "excluded_page_types": excluded_page_types}
            result = await self._gateway.rows(_PAGE_TYPE_CHILDREN_SQL, params, cap=_ROW_CAP)
            _require_group_exists(result, collection, value)
            children = _with_truncation_notice(tuple(_child("page", row, collection) for row in result.rows), ref, result)
        elif page_type is not None:
            params = {"collection": collection, "app": value, "page_type": page_type, "excluded_page_types": excluded_page_types}
            result = await self._gateway.rows(_GROUP_PAGE_TYPE_CHILDREN_SQL, params, cap=_ROW_CAP)
            _require_group_exists(result, collection, f"{value}/{page_type}")
            children = _with_truncation_notice(tuple(_child("page", row, collection) for row in result.rows), ref, result)
        else:
            params = {"collection": collection, "app": value, "excluded_page_types": excluded_page_types}
            result = await self._gateway.rows(_GROUP_CHILDREN_SQL, params, cap=_ROW_CAP)
            if result.truncated_message is None:
                _require_group_exists(result, collection, value)
                children = tuple(_child("page", row, collection) for row in result.rows)
            else:
                substubs = await self._gateway.rows(_GROUP_PAGE_TYPE_GROUPS_SQL, params, cap=_ROW_CAP)
                children = _with_truncation_notice(
                    tuple(_group_page_type_child(collection, value, row) for row in substubs.rows), ref, substubs
                )
                degrade_note = _DEGRADE_NOTE.format(count=len(substubs.rows))
        return DisclosureEnvelope(ref=ref, kind="group", title=title, content=degrade_note, children=children, mode=self.mode)

    async def _page_envelope(self, ref: str, page_id: str) -> DisclosureEnvelope:
        """Render a page, children its sections, content its whole text when the page is small."""
        result = await self._gateway.rows(
            _PAGE_ENVELOPE_SQL, {"collection": self._collection, "page_id": page_id}, cap=_ROW_CAP + 1
        )
        page_row = next((row for row in result.rows if row["row_type"] == "page"), None)
        if page_row is None:
            msg = f"page ref matched no page in the store: {ref!r}"
            raise BadRefError(msg)
        sections = [row for row in result.rows if row["row_type"] == "section"]
        children = _with_truncation_notice(tuple(_child("section", row, self._collection or "") for row in sections), ref, result)
        return DisclosureEnvelope(
            ref=ref,
            kind=str(page_row["page_type"]),
            title=str(page_row["title"]),
            summary=str(page_row["summary"]),
            content=_inlined_page(sections),
            children=children,
            mode=self.mode,
        )

    async def _section_envelope(self, ref: str, section_id: str) -> DisclosureEnvelope:
        """Render a section, content its body, no children."""
        result = await self._gateway.rows(
            _SECTION_ENVELOPE_SQL, {"collection": self._collection, "section_id": section_id}, cap=_ROW_CAP
        )
        if not result.rows:
            msg = f"section ref matched no section in the store: {ref!r}"
            raise BadRefError(msg)
        row = result.rows[0]
        return DisclosureEnvelope(
            ref=ref,
            kind="section",
            title=str(row["title"]),
            summary=str(row["summary"]),
            content=str(row["body"]),
            mode=self.mode,
        )


def _inlined_page(sections: list[Row]) -> str | None:
    """The whole page's text when every section row carries its body, else `None`."""
    if not sections or any(not row.get("body") for row in sections):
        return None
    return "\n\n".join(f"## {row['title']}\n\n{row['body']}" for row in sections)


_TRUNCATION_SENTINEL_SUMMARY = f"Showing the first {_ROW_CAP}. More exist in this group. Find the rest with `search`."
"""The summary of the sentinel child appended to a truncated listing."""


def _with_truncation_notice(children: tuple[ChildSummary, ...], ref: str, result: SqlResult) -> tuple[ChildSummary, ...]:
    """Append a `truncated:<ref>` sentinel child when `result` was row-capped."""
    if result.truncated_message is None:
        return children
    sentinel = ChildSummary(ref=f"truncated:{ref}", kind="truncated", title="Results truncated", summary=_TRUNCATION_SENTINEL_SUMMARY)
    return (*children, sentinel)


def _require_group_exists(result: SqlResult, collection: str, value: str) -> None:
    """Raise `GroupUnknownError` when a group's own page query returned no rows."""
    if result.rows:
        return
    msg = f"No group '{value}' in collection '{collection}'. Call fetch with no arguments to list the groups this knowledge base has."
    raise GroupUnknownError(msg)


def _array_literal(page_types: frozenset[str]) -> str:
    """`page_types` as a sorted Postgres text[] literal, safe because page types are plain identifiers."""
    return "{" + ",".join(sorted(page_types)) + "}"


def _parse_ref(ref: str, collection: str | None) -> tuple[str, str]:
    """Split `ref` into its `(kind, name)` parts, refusing a ref that names another collection."""
    if collection is None:
        kind, sep, name = ref.partition(":")
        if not sep or not name or kind not in _REF_TYPES - {"unit"}:
            _bad_ref(ref)
        return kind, name
    resolved = parse_ref(ref, collection)
    if resolved.collection != collection:
        raise WrongCollectionError(collection, resolved.collection)
    if resolved.kind == "collection":
        return "collection", resolved.collection
    if resolved.kind == "group":
        return "group", f"{resolved.collection}/{resolved.unit_id}"
    return resolved.kind, resolved.unit_id


def _child(prefix: Literal["page", "section"], row: Row, collection: str) -> ChildSummary:
    """One `ChildSummary` stub: `ref` is `<prefix>:<row id>`, the rest read straight off `row`."""
    ref = f"{prefix}:{row['id']}" if not collection else qualify_ref(f"{prefix}:{row['id']}", collection)
    return ChildSummary(ref=ref, kind=str(row["page_type"]), title=str(row["title"]), summary=str(row["summary"]))


def _group_child(collection: str, row: Row, key: GroupByKey) -> ChildSummary:
    """One group stub for a grouped collection map: `ref` is `group:<collection>/<row[key]>`."""
    value = str(row[key])
    summary = _scent_summary(row)
    return ChildSummary(ref=f"group:{collection}/{value}", kind="group", title=value or "ungrouped", summary=summary)


def _group_page_type_child(collection: str, app: str, row: Row) -> ChildSummary:
    """One page-type sub-stub of an app group: `ref` is `group:<collection>/<app>/<page_type>`."""
    page_type = str(row["page_type"])
    summary = _scent_summary(row)
    return ChildSummary(ref=f"group:{collection}/{app}/{page_type}", kind="group", title=page_type, summary=summary)


def _scent_summary(row: Row) -> str:
    """A group-counting row's summary: `"<N> page(s)"`, plus `"· e.g. <titles>"` when it has sample titles."""
    pages = int(str(row["pages"]))
    base = f"{pages} page" if pages == 1 else f"{pages} pages"
    titles = row.get("sample_titles")
    if not isinstance(titles, (list, tuple)) or not titles:
        return base
    return f"{base} · e.g. {'; '.join(str(title) for title in titles)}"


_ABOUT_MISSION_HEADING = "# Purpose & scope"
"""The heading the purpose text of an indexed about page opens with. Text ahead of it is dropped from the map."""


def _mission_only(body: str) -> str:
    """`body` from its `_ABOUT_MISSION_HEADING` onward, or `body` unchanged when that heading is absent."""
    index = body.find(_ABOUT_MISSION_HEADING)
    return body[index:].strip() if index != -1 else body


def _voice(about: SqlResult, orientation: str | None) -> str | None:
    """The about text, then `orientation`, joined where both exist, or `None` when neither does."""
    body = _mission_only(str(about.rows[0]["body"])) if about.rows else ""
    parts = [part for part in (body, orientation) if part]
    return "\n\n".join(parts) if parts else None


def _append_note(content: str | None, note: str | None) -> str | None:
    """`content` with `note` appended after a blank line, or `note` alone when `content` is absent."""
    if note is None:
        return content
    return f"{content}\n\n{note}" if content else note
