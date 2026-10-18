"""Unit specs for `EnvelopeHandler` (`src/engram/retrieval/envelope.py`).

Driven entirely through :class:`FakeSqlGateway` (records the SQL + bound params, returns a
canned :class:`SqlResult`), so there is no database and no network. These specs pin the
progressive-disclosure ref grammar:

* `collection:<name>` → `page_type="collection"`, `content=None`, children are the collection's pages
  (id/page_type/title/summary) when the spec declares no grouping, or group stubs (`page_type="group"`,
  one per distinct app or page_type value) when it does. When ungrouped and the page listing would
  exceed the row cap, the map degrades to the page_type-grouped form instead of truncating (never when
  a `group_by` is declared — that stays the owner's spine choice);
* `group:<collection>/<value>` → children are that group's pages, exactly as an ungrouped
  `collection:<name>` lists them today. Under `group_by="app"`, an app group's own page listing
  that would exceed the cap degrades one rung to page_type sub-stubs, addressed by the three-segment
  form `group:<collection>/<app>/<page_type>`; that sub-group's own page listing has no further rung
  and truncates with the sentinel, same as an ungrouped `group_by="page_type"` group;
* `page:<id>` → children are its sections ordered by `ord`; `content` is the whole page's text
  when every section row carried its body (the small-page inline), else `None`;
* `section:<id>` → `content=body`, no children;
* a ref that matches none of the four forms raises `BadRefError` before any SQL runs.

Every group-counting result row carries a `sample_titles` scent column, rendered into the stub
summary; a collection's `content` is its indexed about text with the `orientation` constructor
argument appended (own words first, structure after), `orientation` alone when about is absent or
empty, and `None` when neither exists.

The handler's SQL constants are thin `SELECT * FROM api.envelope_*(:param)` wrappers over
`SECURITY DEFINER` functions (the reader role cannot see `kb.*` directly — see `envelope.py`'s
module docstring and `alembic/versions/0012_envelope_api_surface.py`), so the SQL-shape assertions
here only pin "goes through `api.envelope_*`, parameterized". The
`WHERE`/`LEFT JOIN`/`COALESCE`/page-status-filter/summary-fallback shape lives in the migration's
function bodies and is proven against a migrated database + the reader role in
`tests/integration/serve/test_envelope_api_surface.py`.
"""

from __future__ import annotations

import pytest

from core.ai.disclosure.contracts import ChildSummary, DisclosureEnvelope

from engram.retrieval.domain import SqlResult
from engram.retrieval.envelope import (
    BadRefError,
    EnvelopeHandler,
    ResolvedRef,
    WrongCollectionError,
    fetch_description,
    normalize_ref,
    parse_ref,
    qualify_ref,
)
from engram.retrieval.exceptions import GroupUnknownError, RefRefusedError
from engram.testing.fakes.gateway import FakeSqlGateway


class TestCollectionRef:
    async def test_lists_the_collections_pages_as_children(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},)
        gateway = FakeSqlGateway(routed={"envelope_collection": SqlResult(rows=rows, columns=("id", "page_type", "title", "summary"))})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:workdocs")

        assert envelope == DisclosureEnvelope(
            ref="collection:workdocs",
            kind="collection",
            title="workdocs",
            children=(ChildSummary(ref="page:workdocs/mu.x", kind="memory-unit", title="X", summary="Indexed summary of X."),),
        )

    async def test_binds_the_collection_name_through_the_api_envelope_function(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        await handler("collection:workdocs")

        children = gateway.rows_calls[0]
        assert children.params == {"collection": "workdocs", "excluded_page_types": "{}"}
        assert children.cap == 50
        assert "api.envelope_collection(:collection, cast(:excluded_page_types as text)::text[])" in children.sql
        assert "memory." not in children.sql  # the reader role cannot see kb.* directly

    async def test_no_matching_pages_yields_no_children(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:empty")

        assert envelope.children == ()

    async def test_flat_listing_is_unaffected_when_group_by_is_none(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},)
        gateway = FakeSqlGateway(routed={"envelope_collection(": SqlResult(rows=rows, columns=("id", "page_type", "title", "summary"))})
        handler = EnvelopeHandler(gateway, group_by=None)

        envelope = await handler("collection:workdocs")

        assert envelope.children == (ChildSummary(ref="page:workdocs/mu.x", kind="memory-unit", title="X", summary="Indexed summary of X."),)

    async def test_lists_app_groups_as_children_when_group_by_is_set(self):
        rows = ({"app": "engram", "pages": 5}, {"app": "", "pages": 2})
        gateway = FakeSqlGateway(routed={"envelope_collection_groups": SqlResult(rows=rows, columns=("app", "pages"))})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:repo-docs")

        assert envelope.children == (
            ChildSummary(ref="group:repo-docs/engram", kind="group", title="engram", summary="5 pages"),
            ChildSummary(ref="group:repo-docs/", kind="group", title="ungrouped", summary="2 pages"),
        )

    async def test_a_single_page_group_summary_is_singular(self):
        rows = ({"app": "engram", "pages": 1},)
        gateway = FakeSqlGateway(routed={"envelope_collection_groups": SqlResult(rows=rows, columns=("app", "pages"))})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:repo-docs")

        assert envelope.children == (ChildSummary(ref="group:repo-docs/engram", kind="group", title="engram", summary="1 page"),)

    async def test_binds_the_collection_name_through_the_groups_function_when_grouped(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        await handler("collection:repo-docs")

        children = gateway.rows_calls[0]
        assert children.params == {"collection": "repo-docs", "excluded_page_types": "{}"}
        assert children.cap == 50
        assert "api.envelope_collection_groups(:collection, cast(:excluded_page_types as text)::text[])" in children.sql
        assert "memory." not in children.sql  # the reader role cannot see kb.* directly

    async def test_no_app_groups_yields_no_children_when_grouped(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:empty")

        assert envelope.children == ()

    async def test_lists_kind_groups_as_children_when_group_by_is_kind(self):
        rows = ({"page_type": "adr", "pages": 5}, {"page_type": "reference", "pages": 2})
        gateway = FakeSqlGateway(routed={"envelope_collection_page_types": SqlResult(rows=rows, columns=("page_type", "pages"))})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("collection:repo-docs")

        assert envelope.children == (
            ChildSummary(ref="group:repo-docs/adr", kind="group", title="adr", summary="5 pages"),
            ChildSummary(ref="group:repo-docs/reference", kind="group", title="reference", summary="2 pages"),
        )

    async def test_a_single_page_kind_group_summary_is_singular(self):
        rows = ({"page_type": "adr", "pages": 1},)
        gateway = FakeSqlGateway(routed={"envelope_collection_page_types": SqlResult(rows=rows, columns=("page_type", "pages"))})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("collection:repo-docs")

        assert envelope.children == (ChildSummary(ref="group:repo-docs/adr", kind="group", title="adr", summary="1 page"),)

    async def test_binds_the_collection_name_through_the_kinds_function_when_kind_grouped(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="page_type")

        await handler("collection:repo-docs")

        children = gateway.rows_calls[0]
        assert children.params == {"collection": "repo-docs", "excluded_page_types": "{}"}
        assert children.cap == 50
        assert "api.envelope_collection_page_types(:collection, cast(:excluded_page_types as text)::text[])" in children.sql
        assert "memory." not in children.sql  # the reader role cannot see kb.* directly

    async def test_no_kind_groups_yields_no_children_when_kind_grouped(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("collection:empty")

        assert envelope.children == ()


class TestAutoDegradeFlatToPageTypeGroups:
    """`group_by is None`: a flat page listing over the row cap degrades to the page_type-grouped map.

    The switch is decided by `SqlResult.truncated_message` on the FLAT query alone (set exactly
    when the underlying listing has more rows than the cap, `ReadOnlySqlGateway._apply_cap`) — the
    same signal `_with_truncation_notice` reads elsewhere, so "at the cap" and "one over the cap"
    are the two sides of one boundary, not two separate mechanisms.
    """

    _NOTE = "Listing exceeds one screen. Showing 2 page_type groups instead: open one to list its pages."

    def _page_type_rows(self) -> tuple[dict[str, object], ...]:
        return ({"page_type": "adr", "pages": 5}, {"page_type": "reference", "pages": 2})

    async def test_at_the_cap_the_map_stays_flat(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},)
        gateway = FakeSqlGateway(routed={"envelope_collection(": SqlResult(rows=rows, columns=(), truncated_message=None)})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:repo-docs")

        assert envelope.children == (ChildSummary(ref="page:repo-docs/mu.x", kind="memory-unit", title="X", summary="S."),)

    async def test_one_over_the_cap_degrades_to_kind_group_stubs(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_collection(": flat, "envelope_collection_page_types(": SqlResult(rows=self._page_type_rows(), columns=())})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:repo-docs")

        assert envelope.children == (
            ChildSummary(ref="group:repo-docs/adr", kind="group", title="adr", summary="5 pages"),
            ChildSummary(ref="group:repo-docs/reference", kind="group", title="reference", summary="2 pages"),
        )

    async def test_the_degrade_note_names_the_kind_group_count(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_collection(": flat, "envelope_collection_page_types(": SqlResult(rows=self._page_type_rows(), columns=())})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:repo-docs")

        assert envelope.content == self._NOTE

    async def test_the_kinds_query_binds_the_same_collection_and_excluded_page_types(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_collection(": flat, "envelope_collection_page_types(": SqlResult(rows=(), columns=())})
        handler = EnvelopeHandler(gateway, excluded_page_types=frozenset({"hub"}))

        await handler("collection:repo-docs")

        page_types_call = next(call for call in gateway.rows_calls if "envelope_collection_page_types(" in call.sql)
        assert page_types_call.params == {"collection": "repo-docs", "excluded_page_types": "{hub}"}
        assert page_types_call.cap == 50

    async def test_a_declared_app_group_by_is_never_auto_overridden(self):
        """`group_by="app"` never calls the flat query at all — its own listing truncates with the sentinel, not a switch."""
        rows = ({"app": "engram", "pages": 5},)
        truncated = SqlResult(rows=rows, columns=("app", "pages"), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_collection_groups": truncated})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:repo-docs")

        assert all("envelope_collection(" not in call.sql or "envelope_collection_page_types(" not in call.sql for call in gateway.rows_calls)
        assert envelope.children[-1].kind == "truncated"
        assert envelope.children[0] == ChildSummary(ref="group:repo-docs/engram", kind="group", title="engram", summary="5 pages")

    async def test_a_declared_kind_group_by_is_never_auto_overridden(self):
        """`group_by="page_type"` already IS the page_type-grouped map — its own listing truncates with the sentinel, not a second switch."""
        rows = ({"page_type": "adr", "pages": 5},)
        truncated = SqlResult(rows=rows, columns=("page_type", "pages"), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_collection_page_types": truncated})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("collection:repo-docs")

        assert envelope.content is None  # no degrade note — this already IS the degraded form
        assert envelope.children[-1].kind == "truncated"
        assert envelope.children[0] == ChildSummary(ref="group:repo-docs/adr", kind="group", title="adr", summary="5 pages")


class TestGroupRef:
    """`group:<collection>/<app>` → that app's pages, exactly as a flat collection lists pages today."""

    async def test_lists_the_apps_pages_as_children(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},)
        gateway = FakeSqlGateway(routed={"envelope_group(": SqlResult(rows=rows, columns=("id", "page_type", "title", "summary"))})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram")

        assert envelope == DisclosureEnvelope(
            ref="group:repo-docs/engram",
            kind="group",
            title="engram",
            children=(ChildSummary(ref="page:repo-docs/mu.x", kind="memory-unit", title="X", summary="Indexed summary of X."),),
        )

    async def test_the_ungrouped_bucket_titles_itself_ungrouped(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/")

        assert envelope.title == "ungrouped"

    async def test_binds_the_collection_and_app_through_the_api_envelope_group_function(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        await handler("group:repo-docs/engram")

        children = gateway.last
        assert children.params == {"collection": "repo-docs", "app": "engram", "excluded_page_types": "{}"}
        assert children.cap == 50
        assert "api.envelope_group(:collection, :app, cast(:excluded_page_types as text)::text[])" in children.sql
        assert "memory." not in children.sql  # the reader role cannot see kb.* directly

    async def test_an_unknown_group_value_raises_no_such_group_error(self):
        """A group only exists because the map's own counting query offered it — zero rows means a typo, not "empty"."""
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        with pytest.raises(GroupUnknownError) as excinfo:
            await handler("group:repo-docs/no-such-app")

        message = str(excinfo.value)
        assert "no-such-app" in message
        assert "repo-docs" in message
        assert "fetch with no arguments" in message

    def test_no_such_group_error_is_a_refused_ref_error(self):
        """A subtype `_leveled_fetch` catches before the legacy fallback (same category as `WrongCollectionError`)."""
        assert issubclass(GroupUnknownError, RefRefusedError)

    async def test_a_group_ref_missing_the_app_segment_raises_bad_ref_error(self):
        gateway = FakeSqlGateway()
        handler = EnvelopeHandler(gateway, group_by="app")

        with pytest.raises(BadRefError):
            await handler("group:repo-docs")

        assert gateway.rows_calls == []  # grammar is validated before any SQL runs


class TestKindGroupRef:
    """`group:<collection>/<page_type>` when `group_by="page_type"` → that page_type's pages."""

    async def test_lists_the_kinds_pages_as_children(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "Summary of A."},)
        gateway = FakeSqlGateway(routed={"envelope_page_type": SqlResult(rows=rows, columns=("id", "page_type", "title", "summary"))})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("group:repo-docs/adr")

        assert envelope == DisclosureEnvelope(
            ref="group:repo-docs/adr",
            kind="group",
            title="adr",
            children=(ChildSummary(ref="page:repo-docs/adr.a", kind="adr", title="A", summary="Summary of A."),),
        )

    async def test_binds_the_collection_and_kind_through_the_api_envelope_kind_function(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "Summary of A."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "page_type", "title", "summary")))
        handler = EnvelopeHandler(gateway, group_by="page_type")

        await handler("group:repo-docs/adr")

        children = gateway.last
        assert children.params == {"collection": "repo-docs", "page_type": "adr", "excluded_page_types": "{}"}
        assert children.cap == 50
        assert "api.envelope_page_type(:collection, :page_type, cast(:excluded_page_types as text)::text[])" in children.sql
        assert "memory." not in children.sql  # the reader role cannot see kb.* directly

    async def test_an_unknown_kind_value_raises_no_such_group_error(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="page_type")

        with pytest.raises(GroupUnknownError):
            await handler("group:repo-docs/no-such-page_type")


class TestAppKindSubGroupRef:
    """`group:<collection>/<app>/<page_type>` under `group_by="app"` → that app AND page_type's pages.

    The tail after `group:` splits on `/` at most twice, so the existing two-segment forms —
    including the empty-app ungrouped bucket `group:<collection>/` — keep parsing exactly as
    before; a third segment is the page_type, and an empty app with a page_type segment
    (`group:<collection>//<page_type>`) is the ungrouped bucket's own page_type sub-group.
    """

    async def test_lists_the_apps_and_kinds_pages_as_children(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "Summary of A."},)
        gateway = FakeSqlGateway(routed={"envelope_group_page_type(": SqlResult(rows=rows, columns=("id", "page_type", "title", "summary"))})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram/adr")

        assert envelope == DisclosureEnvelope(
            ref="group:repo-docs/engram/adr",
            kind="group",
            title="adr",
            children=(ChildSummary(ref="page:repo-docs/adr.a", kind="adr", title="A", summary="Summary of A."),),
        )

    async def test_binds_the_collection_app_and_kind_through_the_api_envelope_group_kind_function(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "Summary of A."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=("id", "page_type", "title", "summary")))
        handler = EnvelopeHandler(gateway, group_by="app")

        await handler("group:repo-docs/engram/adr")

        children = gateway.last
        assert children.params == {"collection": "repo-docs", "app": "engram", "page_type": "adr", "excluded_page_types": "{}"}
        assert children.cap == 50
        assert "api.envelope_group_page_type(:collection, :app, :page_type, cast(:excluded_page_types as text)::text[])" in children.sql
        assert "memory." not in children.sql  # the reader role cannot see kb.* directly

    async def test_the_empty_app_with_a_kind_segment_is_the_ungrouped_buckets_kind_subgroup(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        await handler("group:repo-docs//adr")

        assert gateway.last.params == {"collection": "repo-docs", "app": "", "page_type": "adr", "excluded_page_types": "{}"}

    async def test_an_unknown_app_and_kind_combination_raises_no_such_group_error(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        with pytest.raises(GroupUnknownError):
            await handler("group:repo-docs/engram/no-such-page_type")

    async def test_existing_two_segment_forms_still_parse_exactly_as_before(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway, group_by="app")

        await handler("group:repo-docs/engram")
        await handler("group:repo-docs/")

        assert gateway.rows_calls[0].params == {"collection": "repo-docs", "app": "engram", "excluded_page_types": "{}"}
        assert gateway.rows_calls[1].params == {"collection": "repo-docs", "app": "", "excluded_page_types": "{}"}


class TestAutoDegradeGroupToPageTypeSubGroups:
    """`group_by="app"`: one app's page listing over the row cap degrades to that app's page_type sub-stubs."""

    def _page_type_rows(self) -> tuple[dict[str, object], ...]:
        return ({"page_type": "adr", "pages": 3}, {"page_type": "reference", "pages": 1})

    async def test_at_the_cap_the_group_stays_flat(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        gateway = FakeSqlGateway(routed={"envelope_group(": SqlResult(rows=rows, columns=(), truncated_message=None)})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram")

        assert envelope.children == (ChildSummary(ref="page:repo-docs/adr.a", kind="adr", title="A", summary="S."),)

    async def test_one_over_the_cap_degrades_to_kind_substubs(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_group(": flat, "envelope_group_page_types(": SqlResult(rows=self._page_type_rows(), columns=())})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram")

        assert envelope.children == (
            ChildSummary(ref="group:repo-docs/engram/adr", kind="group", title="adr", summary="3 pages"),
            ChildSummary(ref="group:repo-docs/engram/reference", kind="group", title="reference", summary="1 page"),
        )

    async def test_the_degrade_carries_a_note_naming_the_substub_count_and_the_way_forward(self):
        """The app-group's own degrade to page_type sub-stubs is no longer silent (same note the root map's degrade carries)."""
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_group(": flat, "envelope_group_page_types(": SqlResult(rows=self._page_type_rows(), columns=())})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram")

        assert envelope.content == "Listing exceeds one screen. Showing 2 page_type groups instead: open one to list its pages."

    async def test_the_kind_substubs_query_binds_the_collection_and_app(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_group(": flat, "envelope_group_page_types(": SqlResult(rows=(), columns=())})
        handler = EnvelopeHandler(gateway, group_by="app", excluded_page_types=frozenset({"hub"}))

        await handler("group:repo-docs/engram")

        substubs_call = next(call for call in gateway.rows_calls if "envelope_group_page_types(" in call.sql)
        assert substubs_call.params == {"collection": "repo-docs", "app": "engram", "excluded_page_types": "{hub}"}
        assert substubs_call.cap == 50

    async def test_a_kind_groups_own_listing_has_no_further_rung_and_truncates_with_the_sentinel(self):
        """`group_by="page_type"` groups sit one rung below the root map already — truncation is the floor there."""
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        truncated = SqlResult(rows=rows, columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_page_type(": truncated})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("group:repo-docs/adr")

        assert envelope.children[-1].kind == "truncated"
        assert all("envelope_group_page_types(" not in call.sql for call in gateway.rows_calls)

    async def test_an_app_and_kind_subgroups_own_listing_has_no_further_rung_and_truncates_with_the_sentinel(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        truncated = SqlResult(rows=rows, columns=(), truncated_message="50+ more rows — narrow your filters")
        gateway = FakeSqlGateway(routed={"envelope_group_page_type(": truncated})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram/adr")

        assert envelope.children[-1].kind == "truncated"
        assert len(envelope.children) == 2  # the one real page child plus the sentinel


class TestGroupStubScent:
    """Every group stub's summary carries a few sample titles when the SQL provided them."""

    async def test_app_group_stub_includes_sample_titles(self):
        rows = ({"app": "engram", "pages": 5, "sample_titles": ["Alpha", "Beta", "Gamma"]},)
        gateway = FakeSqlGateway(routed={"envelope_collection_groups": SqlResult(rows=rows, columns=())})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:repo-docs")

        assert envelope.children[0].summary == "5 pages · e.g. Alpha; Beta; Gamma"

    async def test_kind_group_stub_includes_sample_titles(self):
        rows = ({"page_type": "adr", "pages": 1, "sample_titles": ["Alpha"]},)
        gateway = FakeSqlGateway(routed={"envelope_collection_page_types": SqlResult(rows=rows, columns=())})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("collection:repo-docs")

        assert envelope.children[0].summary == "1 page · e.g. Alpha"

    async def test_app_kind_substub_includes_sample_titles(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        page_types = SqlResult(rows=({"page_type": "adr", "pages": 3, "sample_titles": ["Alpha", "Beta"]},), columns=())
        gateway = FakeSqlGateway(routed={"envelope_group(": flat, "envelope_group_page_types(": page_types})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("group:repo-docs/engram")

        assert envelope.children[0].summary == "3 pages · e.g. Alpha; Beta"

    async def test_omits_the_tail_when_no_sample_titles_are_given(self):
        rows = ({"app": "engram", "pages": 5},)  # no "sample_titles" key at all
        gateway = FakeSqlGateway(routed={"envelope_collection_groups": SqlResult(rows=rows, columns=())})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:repo-docs")

        assert envelope.children[0].summary == "5 pages"

    async def test_omits_the_tail_when_sample_titles_is_an_empty_list(self):
        rows = ({"app": "engram", "pages": 5, "sample_titles": []},)
        gateway = FakeSqlGateway(routed={"envelope_collection_groups": SqlResult(rows=rows, columns=())})
        handler = EnvelopeHandler(gateway, group_by="app")

        envelope = await handler("collection:repo-docs")

        assert envelope.children[0].summary == "5 pages"


class TestCollectionAbout:
    async def test_the_maps_content_is_the_collections_own_about_text(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=({"body": "Covers X; excludes Y."},), columns=("body",))})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:workdocs")

        assert envelope.content == "Covers X; excludes Y."

    async def test_a_collection_without_an_about_serves_a_bare_map(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:workdocs")

        assert envelope.content is None

    async def test_binds_the_collection_name_through_the_about_function(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        await handler("collection:workdocs")

        about = gateway.rows_calls[1]
        assert about.params == {"collection": "workdocs"}
        assert about.cap == 1
        assert "api.collection_about(:collection)" in about.sql
        assert "memory." not in about.sql  # the reader role cannot see kb.* directly

    async def test_search_only_reads_nothing(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, mode="search_only")

        envelope = await handler("collection:workdocs")

        assert envelope.content is None
        assert gateway.rows_calls == []


class TestOrientationFallback:
    r"""`EnvelopeHandler(orientation=...)`: the collection map's structural voice, framing its about text.

    The knowledge base's own words come first, the structure follows: `content` is `about + "\n\n" +
    orientation` when the collection indexed an about page, `orientation` alone when it did not
    (or indexed an empty one), and left off entirely (today's plain about-or-`None` map) when
    `orientation` itself is `None`. It never touches `group:`/`page:`/`section:` envelopes — those
    either carry their own content or, for groups, none at all.
    """

    _ORIENTATION = "Structure: this map lists pages; a page lists its sections."

    async def test_about_text_comes_first_and_orientation_follows(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=({"body": "Covers X."},), columns=("body",))})
        handler = EnvelopeHandler(gateway, orientation=self._ORIENTATION)

        envelope = await handler("collection:workdocs")

        assert envelope.content == f"Covers X.\n\n{self._ORIENTATION}"

    async def test_orientation_serves_when_about_is_absent(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=(), columns=())})
        handler = EnvelopeHandler(gateway, orientation=self._ORIENTATION)

        envelope = await handler("collection:workdocs")

        assert envelope.content == self._ORIENTATION

    async def test_orientation_serves_when_about_is_an_empty_string(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=({"body": ""},), columns=("body",))})
        handler = EnvelopeHandler(gateway, orientation=self._ORIENTATION)

        envelope = await handler("collection:workdocs")

        assert envelope.content == self._ORIENTATION

    async def test_no_orientation_and_no_about_leaves_content_none(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=(), columns=())})
        handler = EnvelopeHandler(gateway)  # orientation defaults to None

        envelope = await handler("collection:workdocs")

        assert envelope.content is None

    async def test_orientation_frames_the_kind_grouped_map_too(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=(), columns=())})
        handler = EnvelopeHandler(gateway, group_by="page_type", orientation=self._ORIENTATION)

        envelope = await handler("collection:repo-docs")

        assert envelope.content == self._ORIENTATION

    async def test_group_envelopes_never_carry_orientation(self):
        """A group's own (non-degraded) listing carries no `content` at all — orientation frames only the root map."""
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway, group_by="app", orientation=self._ORIENTATION)

        envelope = await handler("group:repo-docs/engram")

        assert envelope.content is None

    async def test_the_degrade_note_appends_after_about_and_orientation_when_both_present(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        page_types = SqlResult(rows=({"page_type": "adr", "pages": 5},), columns=())
        gateway = FakeSqlGateway(
            routed={
                "envelope_collection(": flat,
                "envelope_collection_page_types(": page_types,
                "collection_about": SqlResult(rows=({"body": "Covers X."},), columns=("body",)),
            }
        )
        handler = EnvelopeHandler(gateway, orientation=self._ORIENTATION)

        envelope = await handler("collection:repo-docs")

        assert envelope.content == (
            f"Covers X.\n\n{self._ORIENTATION}\n\n"
            "Listing exceeds one screen. Showing 1 page_type groups instead: open one to list its pages."
        )

    async def test_the_degrade_note_appends_to_orientation_when_about_is_absent(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        page_types = SqlResult(rows=({"page_type": "adr", "pages": 5},), columns=())
        gateway = FakeSqlGateway(
            routed={"envelope_collection(": flat, "envelope_collection_page_types(": page_types, "collection_about": SqlResult(rows=(), columns=())}
        )
        handler = EnvelopeHandler(gateway, orientation=self._ORIENTATION)

        envelope = await handler("collection:repo-docs")

        assert envelope.content == (
            f"{self._ORIENTATION}\n\nListing exceeds one screen. Showing 1 page_type groups instead: open one to list its pages."
        )

    async def test_the_degrade_note_stands_alone_with_no_about_and_no_orientation(self):
        flat = SqlResult(rows=(), columns=(), truncated_message="50+ more rows — narrow your filters")
        page_types = SqlResult(rows=({"page_type": "adr", "pages": 5},), columns=())
        gateway = FakeSqlGateway(
            routed={"envelope_collection(": flat, "envelope_collection_page_types(": page_types, "collection_about": SqlResult(rows=(), columns=())}
        )
        handler = EnvelopeHandler(gateway)  # no orientation

        envelope = await handler("collection:repo-docs")

        assert envelope.content == "Listing exceeds one screen. Showing 1 page_type groups instead: open one to list its pages."


class TestPageRef:
    def _rows(self) -> tuple[dict[str, object], ...]:
        return (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "S1", "summary": "Sec1 summary."},
            {"row_type": "section", "id": "mu.x#s2", "page_type": "section", "title": "S2", "summary": ""},
        )

    async def test_content_is_none_and_children_are_its_sections_ordered_by_ord(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=self._rows(), columns=()))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("page:mu.x")

        assert envelope == DisclosureEnvelope(
            ref="page:mu.x",
            kind="memory-unit",
            title="X",
            summary="Indexed summary of X.",
            content=None,
            children=(
                ChildSummary(ref="section:mu.x#s1", kind="section", title="S1", summary="Sec1 summary."),
                ChildSummary(ref="section:mu.x#s2", kind="section", title="S2", summary=""),
            ),
        )

    async def test_binds_the_page_id_through_the_api_envelope_function(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=self._rows(), columns=()))
        handler = EnvelopeHandler(gateway)

        await handler("page:mu.x")

        # `collection` leads (0025): a bare page id is unique only paired with its collection. This
        # handler was built with none (the permissive default), so it binds `None`.
        assert gateway.last.params == {"collection": None, "page_id": "mu.x"}
        # `_ROW_CAP + 1`, NOT `_ROW_CAP`: `api.envelope_page`'s row set includes the page's own
        # `row_type == "page"` row alongside its sections, so a plain `_ROW_CAP` cap would let that
        # one row displace a section and silently cap the returned children at `_ROW_CAP - 1`.
        assert gateway.last.cap == 51
        assert "api.envelope_page(:collection, :page_id)" in gateway.last.sql
        assert "memory." not in gateway.last.sql  # the reader role cannot see kb.* directly

    async def test_a_small_page_arrives_whole_with_its_sections_still_addressable(self):
        rows = (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Summary."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "Statement", "summary": "", "body": "The fact."},
            {"row_type": "section", "id": "mu.x#s2", "page_type": "section", "title": "Provenance", "summary": "", "body": "Where from."},
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("page:mu.x")

        assert envelope.content == "## Statement\n\nThe fact.\n\n## Provenance\n\nWhere from."
        assert [child.ref for child in envelope.children] == ["section:mu.x#s1", "section:mu.x#s2"]

    async def test_a_page_over_the_inline_cap_stays_stub_only(self):
        # The SQL returns NULL bodies past the cap; any absent body means stub-only, never a partial page.
        rows = (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Summary."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "S1", "summary": "", "body": "present"},
            {"row_type": "section", "id": "mu.x#s2", "page_type": "section", "title": "S2", "summary": "", "body": None},
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("page:mu.x")

        assert envelope.content is None

    async def test_unknown_page_id_raises_bad_ref_error(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        with pytest.raises(BadRefError):
            await handler("page:does-not-exist")


class TestSectionRef:
    async def test_content_is_body_with_no_children(self):
        row = {"id": "mu.x#s1", "title": "S1", "body": "Full section body.", "summary": "Sec1 summary."}
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("section:mu.x#s1")

        assert envelope == DisclosureEnvelope(
            ref="section:mu.x#s1",
            kind="section",
            title="S1",
            summary="Sec1 summary.",
            content="Full section body.",
            children=(),
        )

    async def test_summary_falls_back_to_empty_string_with_no_active_summary(self):
        row = {"id": "mu.x#s1", "title": "S1", "body": "Full section body.", "summary": ""}
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("section:mu.x#s1")

        assert envelope.summary == ""  # sections carry no frontmatter fallback, unlike pages

    async def test_binds_the_section_id_through_the_api_envelope_function(self):
        row = {"id": "mu.x#s1", "title": "S1", "body": "Body.", "summary": ""}
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = EnvelopeHandler(gateway)

        await handler("section:mu.x#s1")

        # `collection` leads (0025), bound `None` here — this handler was built without one.
        assert gateway.last.params == {"collection": None, "section_id": "mu.x#s1"}
        assert gateway.last.cap == 50
        assert "api.envelope_section(:collection, :section_id)" in gateway.last.sql
        assert "memory." not in gateway.last.sql  # the reader role cannot see kb.* directly

    async def test_unknown_section_id_raises_bad_ref_error(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        with pytest.raises(BadRefError):
            await handler("section:does-not-exist")


class TestBadRef:
    @pytest.mark.parametrize(
        "ref",
        [
            "nonsense",
            "foo:bar",
            "collection:",
            "group:",
            "page:",
            "section:",
            "",
            ":mu.x",
        ],
    )
    async def test_a_ref_matching_none_of_the_four_forms_raises_bad_ref_error(self, ref: str):
        gateway = FakeSqlGateway()
        handler = EnvelopeHandler(gateway)

        with pytest.raises(BadRefError):
            await handler(ref)

        assert gateway.rows_calls == []  # grammar is validated before any SQL runs

    def test_bad_ref_error_is_a_value_error(self):
        assert issubclass(BadRefError, ValueError)


class TestTruncation:
    """`SqlResult.truncated_message` must surface as a sentinel child at every listing's floor rung.

    Dropping it would leave a descending agent with no signal that a large collection or page had
    more children than the row cap returned. The handler appends a synthetic `ChildSummary`
    (`ref="truncated:<ref>"`, `page_type="truncated"`) carrying THIS surface's own wording, not the SQL
    gateway's "narrow your filters" (`fetch` has no filter argument to narrow) — `_MESSAGE` is only
    the trigger `SqlGateway.rows` stamps to signal a capped result; the sentinel's own `summary` is
    fixed, independent of it. A flat, ungrouped collection map and an app group's own page listing
    are NOT floor rungs any more — both degrade instead (`TestAutoDegradeFlatToPageTypeGroups`,
    `TestAutoDegradeGroupToPageTypeSubGroups`); the sentinel remains the floor for the page_type-grouped
    root map, `group_by="page_type"` group listings, and app+page_type sub-groups.
    """

    _MESSAGE = "… 12 more rows, narrow your filters"  # the gateway's raw trigger — never shown to the agent
    _SENTINEL_SUMMARY = "Showing the first 50. More exist in this group. Find the rest with `search`."

    def _sentinel(self, ref: str) -> ChildSummary:
        return ChildSummary(ref=f"truncated:{ref}", kind="truncated", title="Results truncated", summary=self._SENTINEL_SUMMARY)

    async def test_page_envelope_appends_the_sentinel_child_when_truncated(self):
        rows = (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "S1", "summary": "Sec1."},
        )
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=(), truncated_message=self._MESSAGE))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("page:mu.x")

        assert envelope.children[-1] == self._sentinel("page:mu.x")
        assert len(envelope.children) == 2  # the one real section child plus the sentinel

    async def test_kind_group_envelope_appends_the_sentinel_child_when_truncated(self):
        rows = ({"id": "adr.a", "page_type": "adr", "title": "A", "summary": "S."},)
        truncated = SqlResult(rows=rows, columns=("id", "page_type", "title", "summary"), truncated_message=self._MESSAGE)
        gateway = FakeSqlGateway(routed={"envelope_page_type": truncated})
        handler = EnvelopeHandler(gateway, group_by="page_type")

        envelope = await handler("group:repo-docs/adr")

        assert envelope.children[-1] == self._sentinel("group:repo-docs/adr")
        assert len(envelope.children) == 2  # the one real page child plus the sentinel


class TestSpecDeclarations:
    """`mode` and `excluded_page_types` — the knowledge base spec's disclosure declarations, enforced."""

    async def test_excluded_page_types_are_bound_into_the_collection_query(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, excluded_page_types=frozenset({"hub", "brainstorming-doc"}))

        await handler("collection:workdocs")

        assert gateway.rows_calls[0].params == {"collection": "workdocs", "excluded_page_types": "{brainstorming-doc,hub}"}

    async def test_search_only_withholds_the_root_map_without_querying(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, mode="search_only")

        envelope = await handler("collection:workdocs")

        assert envelope.children == ()
        assert envelope.mode == "search_only"
        assert gateway.rows_calls == []  # the root map is withheld, not queried-and-dropped

    async def test_search_only_withholds_group_envelopes_without_querying(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway, mode="search_only", group_by="app")

        envelope = await handler("group:repo-docs/engram")

        assert envelope.children == ()
        assert envelope.mode == "search_only"
        assert gateway.rows_calls == []  # the group listing is withheld, not queried-and-dropped

    async def test_search_only_still_serves_page_and_section_refs(self):
        row = {"id": "mu.x#s1", "title": "S1", "summary": "", "body": "Body of S1."}
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = EnvelopeHandler(gateway, mode="search_only")

        envelope = await handler("section:mu.x#s1")

        assert envelope.content == "Body of S1."
        assert envelope.mode == "search_only"  # hits re-enter as disclosable refs, stamped with the mode

    async def test_the_mode_is_stamped_on_every_envelope(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:workdocs")

        assert envelope.mode == "strict"


class TestCollectionOwnership:
    """`EnvelopeHandler(collection=...)`: a `collection:`/`group:` ref naming another knowledge base refuses.

    Guards against the exact failure a shared store makes possible: a server built for one knowledge base
    (e.g. `field-notes`) asked for another knowledge base's map (`collection:repo-docs`) and serving it in
    full, rendered under the WRONG spec. `collection=None` (every construction site above this
    class) keeps today's permissive behavior — nothing here changes for a caller that never
    threads it.
    """

    async def test_a_foreign_collection_ref_raises_wrong_collection_error_before_any_sql_runs(self):
        gateway = FakeSqlGateway()
        handler = EnvelopeHandler(gateway, collection="field-notes")

        with pytest.raises(WrongCollectionError):
            await handler("collection:repo-docs")

        assert gateway.rows_calls == []

    async def test_the_refusal_names_both_knowledge_bases_and_the_way_back(self):
        gateway = FakeSqlGateway()
        handler = EnvelopeHandler(gateway, collection="field-notes")

        with pytest.raises(WrongCollectionError) as excinfo:
            await handler("collection:repo-docs")

        message = str(excinfo.value)
        assert "field-notes" in message
        assert "repo-docs" in message
        assert "Re-ground" in message
        assert "search" in message

    async def test_a_foreign_group_ref_also_raises_wrong_collection_error(self):
        gateway = FakeSqlGateway()
        handler = EnvelopeHandler(gateway, collection="field-notes", group_by="page_type")

        with pytest.raises(WrongCollectionError):
            await handler("group:repo-docs/adr")

        assert gateway.rows_calls == []

    @pytest.mark.parametrize("ref", ["page:research/paper.x", "section:research/paper.x#findings"])
    async def test_a_foreign_qualified_unit_refuses_before_any_repository_read(self, ref: str):
        gateway = FakeSqlGateway()
        handler = EnvelopeHandler(gateway, collection="field-notes")

        with pytest.raises(WrongCollectionError) as excinfo:
            await handler(ref)

        assert gateway.rows_calls == []
        assert str(excinfo.value) == (
            "This server serves the 'field-notes' knowledge base only. 'research' is a different knowledge base. "
            "Re-ground it with `search` and fetch a result from this knowledge base."
        )

    def test_wrong_collection_error_is_a_refused_ref_error(self):
        """`RefRefusedError` (a `BadRefError`) is what `_leveled_fetch` catches before the legacy fallback."""
        assert issubclass(WrongCollectionError, RefRefusedError)
        assert issubclass(RefRefusedError, BadRefError)

    async def test_the_handlers_own_collection_still_serves_normally(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},)
        gateway = FakeSqlGateway(routed={"envelope_collection(": SqlResult(rows=rows, columns=())})
        handler = EnvelopeHandler(gateway, collection="field-notes")

        envelope = await handler("collection:field-notes")

        assert envelope.children == (ChildSummary(ref="page:field-notes/mu.x", kind="memory-unit", title="X", summary="S."),)

    async def test_page_and_section_refs_are_never_subject_to_the_ownership_check(self):
        """Ids carry no collection segment to check — ownership only applies to `collection:`/`group:` refs."""
        row = {"id": "mu.x#s1", "title": "S1", "body": "Body.", "summary": ""}
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = EnvelopeHandler(gateway, collection="field-notes")

        envelope = await handler("section:mu.x#s1")

        assert envelope.content == "Body."

    async def test_a_page_ref_still_binds_the_handlers_own_collection_though_unchecked(self):
        """Never subject to the ownership CHECK is not the same as collection-blind at the SQL level.

        The bind is what closes the cross-knowledge-base leak: a page/section ref carries no
        collection segment of its own, so the isolation property rests entirely on this handler
        always binding ITS OWN served collection — never the ref's — into `api.envelope_page`. A real
        DB then refuses a bare id owned by another collection (proven at the integration tier,
        `test_envelope_api_surface.py`); this fake-gateway test pins the mechanism that makes that
        refusal possible: the bind itself.
        """
        rows = ({"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "S."},)
        gateway = FakeSqlGateway(result=SqlResult(rows=rows, columns=()))
        handler = EnvelopeHandler(gateway, collection="field-notes")

        await handler("page:mu.x")

        assert gateway.last.params["collection"] == "field-notes"

    async def test_a_section_ref_still_binds_the_handlers_own_collection_though_unchecked(self):
        row = {"id": "mu.x#s1", "title": "S1", "body": "Body.", "summary": ""}
        gateway = FakeSqlGateway(result=SqlResult(rows=(row,), columns=tuple(row)))
        handler = EnvelopeHandler(gateway, collection="field-notes")

        await handler("section:mu.x#s1")

        assert gateway.last.params["collection"] == "field-notes"

    async def test_with_no_collection_declared_a_foreign_looking_ref_is_served_as_before(self):
        gateway = FakeSqlGateway(result=SqlResult(rows=(), columns=()))
        handler = EnvelopeHandler(gateway)  # collection defaults to None: no ownership enforced

        envelope = await handler("collection:anything")

        assert envelope.children == ()  # not refused — matches every pre-existing construction site


class TestAboutAuthoringContractFilter:
    """`_voice` drops any authoring-contract material an indexed about page carries ahead of its mission.

    A knowledge base indexes `meta.about` from every `_meta/` orientation document declaring no
    `audience` (or `audience: query`), in a fixed source order — a schema table and frontmatter
    contract (authoring paperwork, already served at `api.authoring_rules`) ahead of the mission
    prose leaked onto the retrieval map's first line. The filter keeps only the mission/scope text
    onward, regardless of which existing knowledge base's already-indexed about produced the leak.
    """

    _LEAKED_ABOUT = (
        "# Field-notes schema: the contract\n\n"
        "Spec version: `0.1` · Generated by engram `0.0.1`.\n\n"
        "## Page types\n\n| type | id prefix |\n|---|---|\n\n"
        "## Frontmatter contract\n\n"
        "Universal fields on every note: `id`, `type`, `title`.\n\n"
        "# Purpose & scope\n\n## Mission\n\nDomain knowledge base for a field notebook."
    )

    async def test_the_schema_table_and_frontmatter_contract_are_dropped_ahead_of_the_mission(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=({"body": self._LEAKED_ABOUT},), columns=("body",))})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:field-notes")

        assert envelope.content is not None
        assert "schema: the contract" not in envelope.content
        assert "Frontmatter contract" not in envelope.content
        assert envelope.content.startswith("# Purpose & scope")
        assert "Domain knowledge base for a field notebook." in envelope.content

    async def test_an_about_with_no_mission_heading_is_served_unchanged(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=({"body": "Covers X; excludes Y."},), columns=("body",))})
        handler = EnvelopeHandler(gateway)

        envelope = await handler("collection:workdocs")

        assert envelope.content == "Covers X; excludes Y."

    async def test_the_filtered_mission_still_frames_correctly_with_orientation(self):
        gateway = FakeSqlGateway(routed={"collection_about": SqlResult(rows=({"body": self._LEAKED_ABOUT},), columns=("body",))})
        handler = EnvelopeHandler(gateway, orientation="Structure: this map lists page types.")

        envelope = await handler("collection:field-notes")

        assert envelope.content is not None
        assert envelope.content.startswith("# Purpose & scope")
        assert envelope.content.endswith("Structure: this map lists page types.")


class TestFetchDescription:
    def test_strict_names_the_collection_map(self):
        text = fetch_description("workdocs")

        assert "'workdocs' map" in text

    def test_search_only_offers_no_map_and_leads_with_search(self):
        text = fetch_description("workdocs", mode="search_only")

        assert "no browsable map" in text
        assert "`search` first" in text

    def test_can_search_true_is_the_default_and_still_mentions_search(self):
        text = fetch_description("workdocs")

        assert "search" in text

    def test_can_search_false_drops_every_mention_of_search_in_strict_mode(self):
        text = fetch_description("workdocs", can_search=False)

        assert "search" not in text
        assert "'workdocs' map" in text  # the map itself is unaffected, only the search mention drops

    def test_can_search_false_drops_the_directive_to_search_in_search_only_mode(self):
        """`search_only` mode with no `search` tool registered is a genuinely unreachable map.

        The text still names `search` to explain WHY (no other tool exists to reach the map), but
        never directs the agent to use it, and always offers the way out that does exist.
        """
        text = fetch_description("workdocs", mode="search_only", can_search=False)

        assert "find things with `search` first" not in text
        assert "no browsable map" in text
        assert "supplied with the task" in text


class TestNormalizeRef:
    def test_explicit_refs_pass_through(self):
        assert normalize_ref("page:mu.x", "workdocs") == "page:workdocs/mu.x"
        assert normalize_ref("section:mu.x#s1", "workdocs") == "section:workdocs/mu.x#s1"
        assert normalize_ref("collection:workdocs", "workdocs") == "collection:workdocs"
        assert normalize_ref("group:workdocs/engram", "workdocs") == "group:workdocs/engram"

    def test_the_bare_collection_name_opens_the_map(self):
        assert normalize_ref("workdocs", "workdocs") == "collection:workdocs"

    def test_a_hash_id_is_a_section_and_anything_else_a_page(self):
        assert normalize_ref("mu.x#s1", "workdocs") == "section:workdocs/mu.x#s1"
        assert normalize_ref("mu.x", "workdocs") == "page:workdocs/mu.x"


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [
        ("paper.x", "research", ResolvedRef("research", "page", "paper.x")),
        ("paper.x#findings", "research", ResolvedRef("research", "section", "paper.x#findings")),
        ("page:paper.x", "research", ResolvedRef("research", "page", "paper.x")),
        ("section:paper.x#findings", "research", ResolvedRef("research", "section", "paper.x#findings")),
        ("page:research/paper.x", "repo-docs", ResolvedRef("research", "page", "paper.x")),
        ("section:research/paper.x#findings", "repo-docs", ResolvedRef("research", "section", "paper.x#findings")),
        ("unit:research/paper.x#c1", "repo-docs", ResolvedRef("research", "unit", "paper.x#c1")),
        ("collection:research", "repo-docs", ResolvedRef("research", "collection", "")),
        ("group:research/", "repo-docs", ResolvedRef("research", "group", "")),
        ("group:research//reference", "repo-docs", ResolvedRef("research", "group", "/reference")),
    ],
)
def test_parse_ref_accepts_legacy_and_qualified_forms(raw: str, default: str, expected: ResolvedRef) -> None:
    assert parse_ref(raw, default) == expected


def test_qualified_ref_round_trips() -> None:
    ref = qualify_ref("section:paper.x#findings", "research")
    assert ref == "section:research/paper.x#findings"
    assert parse_ref(ref, "repo-docs").unit_id == "paper.x#findings"
    assert qualify_ref(ref, "research") == ref
    assert qualify_ref("collection:research", "research") == "collection:research"
    assert qualify_ref("group:research/", "research") == "group:research/"
    assert qualify_ref("group:research//reference", "research") == "group:research//reference"


@pytest.mark.parametrize(("ref", "collection"), [("page:research/", "repo-docs"), ("unit:research/", "repo-docs"), ("paper.x", "")])
def test_malformed_qualified_refs_and_an_empty_collection_teach_regrounding(ref: str, collection: str) -> None:
    with pytest.raises(BadRefError, match=r"[Rr]e-ground.*search"):
        parse_ref(ref, collection)


@pytest.mark.parametrize(("ref", "collection"), [("page:research/", "repo-docs"), ("unit:research/", "repo-docs"), ("paper.x", "")])
def test_a_caller_supplied_reground_phrase_replaces_search_everywhere(ref: str, collection: str) -> None:
    """A `graph-only` caller passes its own phrase; the refusal never falls back to `search`."""
    with pytest.raises(BadRefError, match=r"ids supplied with the task") as excinfo:
        parse_ref(ref, collection, reground="the ids supplied with the task")

    assert "with `search`" not in str(excinfo.value)


class TestWrongCollectionErrorReground:
    def test_default_names_search(self):
        message = str(WrongCollectionError("workdocs", "repo-docs"))

        assert "`search`" in message

    def test_a_caller_supplied_phrase_replaces_it(self):
        message = str(WrongCollectionError("workdocs", "repo-docs", reground="the ids supplied with the task"))

        assert "ids supplied with the task" in message
        assert "with `search`" not in message
