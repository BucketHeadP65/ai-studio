r"""Unit specs for the leveled `fetch` MCP tool (an `EnvelopeHandler` behind the ordinary `fetch`).

With an `EnvelopeHandler` wired, `fetch(unit_id)` returns the unit's envelope — its own content
plus one-line summaries of everything directly inside it, each with the id to fetch next — and its
default argument opens the knowledge base map. Without one, `fetch` stays the flat full-text read. There is
no separate tool and no separate gate: the leveled read rides `fetch`'s existing registration.

The tests pin the contract the same DI-friendly, transport-free way the rest of the serve MCP suite
does (`tests/unit/serve/test_mcp_server.py`): build the server from real handlers over a
`FakeSqlGateway`, then drive the tool through the SDK's in-memory client-to-server session
(`connect_in_memory`). A bad id FAILS the call (`ToolError`), never a success shaped like one, and
the session survives it.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.ai.retrieval.contracts import KnowledgeBaseStamp
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory

from engram.retrieval.domain import ColumnInfo, ObjectInfo, SchemaDescription, SqlResult
from engram.retrieval.envelope import EnvelopeHandler
from engram.retrieval.infrastructure.mcp_server import create_mcp_server
from engram.retrieval.queries import DescribeSchemaHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import FETCH
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway

if TYPE_CHECKING:
    from core.ai.disclosure.contracts import DisclosureMode
    from core.ai.retrieval.contracts import StampSource
    from mcp.server.fastmcp import FastMCP
    from mcp.types import CallToolResult


_COLLECTION = "workdocs"
"""The made-up collection every door in this module is built over: there is no default one."""


def _schema() -> SchemaDescription:
    """A one-object M-Schema for the always-on describe handler (mirrors the sibling suite)."""
    return SchemaDescription(
        objects=(
            ObjectInfo(
                name="api.cards",
                kind="view",
                comment="L1 card scan.",
                columns=(ColumnInfo(name="id", data_type="text", comment="Page id."),),
            ),
        )
    )


def _describe_handler() -> DescribeSchemaHandler:
    """The real (pure) describe handler over a fake gateway scripted with a one-object schema."""
    return DescribeSchemaHandler(FakeSqlGateway(schema=_schema()))


def _server(
    envelope_result: SqlResult | None = None,
    *,
    collection: str = _COLLECTION,
    mode: DisclosureMode = "strict",
    stamp: StampSource | None = None,
    flat_result: SqlResult | None = None,
    only: frozenset[str] | None = None,
) -> FastMCP:
    """A server with the full single-source bundle wired, plus an envelope handler when scripted.

    `flat_result` scripts the shared gateway's default (unrouted) read, the one the flat legacy
    fetch falls back to, so a fallback case can succeed rather than reporting an unknown id. `only`
    gates the registered structured tools; `None` (the default) is the fully-open profile.
    """
    gateway = FakeSqlGateway() if flat_result is None else FakeSqlGateway(result=flat_result)
    served = collection
    envelope_handler = None
    if envelope_result is not None:
        envelope_handler = EnvelopeHandler(FakeSqlGateway(routed={"envelope_": envelope_result}), mode=mode, collection=collection)
    return create_mcp_server(
        gateway,
        _describe_handler(),
        search_handler=SearchHandler(gateway, FakeEmbedding()),
        fetch_handler=FetchHandler(gateway, served),
        graph_expand_handler=GraphExpandHandler(gateway, served),
        find_paths_handler=GraphPathsHandler(gateway, served),
        envelope_handler=envelope_handler,
        collection=collection,
        stamp=stamp,
        only=only,
    )


def _text(result: CallToolResult) -> str:
    """The first text block of a tool result (the tools return a single text payload)."""
    block = result.content[0]
    assert block.type == "text"
    return block.text


async def _tool(server: FastMCP, name: str) -> str:
    async with connect_in_memory(server) as client:
        await client.initialize()
        listing = await client.list_tools()
    by_name = {tool.name: tool.description or "" for tool in listing.tools}
    return by_name[name]


class TestDescription:
    async def test_leveled_description_names_the_servers_own_collection(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs")

        description = await _tool(server, FETCH)

        assert "'workdocs' map" in description
        assert "one-line" in description
        assert "every level costs tokens" in description.lower()

    async def test_leveled_description_names_the_served_collection(self):
        server = _server(SqlResult(rows=(), columns=()), collection="notebook")

        description = await _tool(server, FETCH)

        assert "'notebook' map" in description

    async def test_search_only_description_leads_with_search_and_offers_no_map(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs", mode="search_only")

        description = await _tool(server, FETCH)

        assert "map" not in description.split("no browsable map")[0]  # the only "map" mention is the denial
        assert "no browsable map" in description
        assert "`search` first" in description

    async def test_without_an_envelope_handler_fetch_keeps_the_flat_description(self):
        server = _server(None)

        description = await _tool(server, FETCH)

        assert "full text" in description
        assert "map" not in description


class TestDescriptionUnderAGraphOnlyProfile:
    """`only` excludes `search`: the leveled `fetch` description must never send the agent there."""

    async def test_no_search_tool_registered_at_all(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs", only=frozenset({"fetch", "graph_expand", "find_paths"}))

        async with connect_in_memory(server) as client:
            await client.initialize()
            listing = await client.list_tools()

        assert "search" not in {tool.name for tool in listing.tools}

    async def test_the_description_grounds_ids_from_the_task_not_search(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs", only=frozenset({"fetch", "graph_expand", "find_paths"}))

        description = await _tool(server, FETCH)

        assert "'workdocs' map" in description  # the map itself still opens
        assert "search" not in description
        assert "supplied with the task" in description

    async def test_full_profile_still_teaches_search(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs", only=None)

        description = await _tool(server, FETCH)

        assert "`search`" in description


class TestLeveledCall:
    async def test_the_default_argument_opens_the_collection_map(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},)
        server = _server(SqlResult(rows=rows, columns=("id", "page_type", "title", "summary")), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {})

        assert result.isError is False
        envelope = json.loads(_text(result))
        assert envelope["ref"] == "collection:workdocs"
        assert envelope["children"] == [{"ref": "page:workdocs/mu.x", "kind": "memory-unit", "title": "X", "summary": "Indexed summary of X."}]

    async def test_a_bare_page_id_normalizes_to_the_page_envelope(self):
        rows = (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Sum."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "S1", "summary": "Sec1."},
        )
        server = _server(SqlResult(rows=rows, columns=()), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "mu.x"})

        envelope = json.loads(_text(result))
        assert envelope["ref"] == "page:workdocs/mu.x"
        assert [child["ref"] for child in envelope["children"]] == ["section:workdocs/mu.x#s1"]

    async def test_a_listed_child_ref_passes_through_verbatim(self):
        row = {"id": "mu.x#s1", "title": "S1", "summary": "Sec1.", "body": "Full text."}
        server = _server(SqlResult(rows=(row,), columns=tuple(row)), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "section:mu.x#s1"})

        envelope = json.loads(_text(result))
        assert envelope["ref"] == "section:workdocs/mu.x#s1"
        assert envelope["content"] == "Full text."

    async def test_an_unknown_id_fails_the_call_and_leaves_the_session_usable(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "mu.does-not-exist"})
            listing = await client.list_tools()

        assert failed.isError is True
        assert "mu.does-not-exist" in _text(failed)
        assert FETCH in {tool.name for tool in listing.tools}


class TestCrossCollectionFetch:
    """A ref naming a different knowledge base than this server's own refuses with the teaching message.

    The regression the audit caught live: a server built for one knowledge base served another knowledge base's
    map in full, rendered under the wrong spec, because the SAME database can hold several
    knowledge_bases's rows. The refusal must reach the agent as the specific teaching message, not the
    legacy-read's generic "not found" (which is what a naive `except BadRefError` fallback would
    produce for a `collection:` ref that is not a real unit id).
    """

    async def test_a_foreign_collection_ref_refuses_with_the_teaching_message(self):
        server = _server(SqlResult(rows=(), columns=()), collection="field-notes")

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "collection:repo-docs"})

        assert failed.isError is True
        message = _text(failed)
        assert "field-notes" in message
        assert "repo-docs" in message
        assert "Re-ground" in message
        assert "search" in message
        assert "No unit found" not in message  # not the legacy read's generic not-found

    async def test_the_session_survives_a_refused_cross_collection_fetch(self):
        server = _server(SqlResult(rows=(), columns=()), collection="field-notes")

        async with connect_in_memory(server) as client:
            await client.initialize()
            await client.call_tool(FETCH, {"unit_id": "collection:repo-docs"})
            listing = await client.list_tools()

        assert FETCH in {tool.name for tool in listing.tools}

    async def test_a_foreign_qualified_unit_refuses_instead_of_becoming_a_local_bare_id(self):
        flat_row = {"id": "paper.x#c1", "level": "claim", "title": "Claim", "body": "Full text."}
        server = _server(
            SqlResult(rows=(), columns=()),
            collection="research",
            flat_result=SqlResult(rows=(flat_row,), columns=tuple(flat_row)),
        )

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "unit:repo-docs/paper.x#c1"})

        assert failed.isError is True
        message = _text(failed)
        assert "repo-docs" in message
        assert "research" in message
        assert "Re-ground" in message
        assert "search" in message


class TestFetchRefusalsUnderAGraphOnlyProfile:
    """Every `fetch` refusal re-grounds with the task's own ids, never `search`, when `only` excludes it.

    Covers the malformed-ref and the wrong-collection paths (both raised in `leveled_fetch` itself)
    and the unknown-legacy-id path (the flat fallback's own not-found message).
    """

    _GRAPH_ONLY = frozenset({"fetch", "graph_expand", "find_paths"})

    async def test_a_foreign_collection_ref_refuses_without_naming_search(self):
        server = _server(SqlResult(rows=(), columns=()), collection="field-notes", only=self._GRAPH_ONLY)

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "collection:repo-docs"})

        assert failed.isError is True
        message = _text(failed)
        assert "field-notes" in message
        assert "repo-docs" in message
        assert "Re-ground" in message
        assert "supplied with the task" in message
        assert "search" not in message

    async def test_an_unknown_legacy_id_refuses_without_naming_search(self):
        """A `unit:` ref bypasses the leveled surface outright, straight to the legacy flat read."""
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs", only=self._GRAPH_ONLY)

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "unit:workdocs/no-such-claim"})

        assert failed.isError is True
        message = _text(failed)
        assert "no-such-claim" in message
        assert "supplied with the task" in message
        assert "search" not in message


class TestNextSteps:
    """Every leveled `fetch` result carries a `next_steps` block naming the next call."""

    async def test_the_collection_map_points_at_its_first_child(self):
        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},)
        server = _server(SqlResult(rows=rows, columns=("id", "page_type", "title", "summary")), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {})

        envelope = json.loads(_text(result))
        next_steps = envelope["next_steps"]
        assert "1 item" in next_steps["summary"]
        (option,) = next_steps["options"]
        assert "page:workdocs/mu.x" in option

    async def test_a_page_names_a_section_and_graph_expand(self):
        rows = (
            {"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Sum."},
            {"row_type": "section", "id": "mu.x#s1", "page_type": "section", "title": "S1", "summary": "Sec1."},
        )
        server = _server(SqlResult(rows=rows, columns=()), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "mu.x"})

        envelope = json.loads(_text(result))
        options = " ".join(envelope["next_steps"]["options"])
        assert "section:workdocs/mu.x#s1" in options
        assert "graph_expand" in options
        assert "mu.x" in options

    async def test_a_section_names_its_parent_page_and_graph_expand(self):
        row = {"id": "mu.x#s1", "title": "S1", "summary": "Sec1.", "body": "Full text."}
        server = _server(SqlResult(rows=(row,), columns=tuple(row)), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "section:mu.x#s1"})

        envelope = json.loads(_text(result))
        options = " ".join(envelope["next_steps"]["options"])
        assert "page:workdocs/mu.x" in options  # the parent, derived from the section id, not just echoed back
        assert "graph_expand" in options
        assert "mu.x" in options

    async def test_a_page_with_no_sections_still_offers_graph_expand(self):
        rows = ({"row_type": "page", "id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Sum."},)
        server = _server(SqlResult(rows=rows, columns=()), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "mu.x"})

        envelope = json.loads(_text(result))
        assert envelope["children"] == []
        options = " ".join(envelope["next_steps"]["options"])
        assert "graph_expand" in options

    async def test_an_unknown_id_message_still_names_the_fix(self):
        server = _server(SqlResult(rows=(), columns=()), collection="workdocs")

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "mu.does-not-exist"})

        message = _text(failed)
        assert "No unit found with id 'mu.does-not-exist'" in message
        assert "search" in message


class TestUnknownGroupFetch:
    """A group ref this collection never had refuses with its own message, not the legacy fallback's.

    A second instance of the exact bug C1 fixed: `_group_envelope` raising a well-formed-but-refused
    error (here `GroupUnknownError`) must not be caught by `_leveled_fetch`'s generic `except
    BadRefError`, which would otherwise retry the whole ref string as a raw legacy unit id and
    report a content-free "No unit found with id 'group:...'" instead of naming the group and the
    way back.
    """

    async def test_an_unknown_group_ref_refuses_with_its_own_message(self):
        server = _server(SqlResult(rows=(), columns=()), collection="field-notes")

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "group:field-notes/no-such-page_type"})

        assert failed.isError is True
        message = _text(failed)
        assert "no-such-page_type" in message
        assert "field-notes" in message
        assert "fetch with no arguments" in message
        assert "No unit found" not in message  # not the legacy read's generic not-found


class TestStamp:
    """A `stamp` given to `create_mcp_server` rides both leveled fetch result shapes."""

    async def test_the_stamp_rides_the_leveled_envelope(self):
        stamp = KnowledgeBaseStamp(slug="workdocs", changed_since=3)

        async def source() -> KnowledgeBaseStamp:
            return stamp

        rows = ({"id": "mu.x", "page_type": "memory-unit", "title": "X", "summary": "Indexed summary of X."},)
        server = _server(SqlResult(rows=rows, columns=("id", "page_type", "title", "summary")), collection="workdocs", stamp=source)

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {})

        envelope = json.loads(_text(result))
        assert envelope["knowledge_base"]["slug"] == "workdocs"
        assert envelope["knowledge_base"]["changed_since"] == 3

    async def test_the_stamp_rides_the_flat_fallback(self):
        stamp = KnowledgeBaseStamp(slug="workdocs", changed_since=3)

        async def source() -> KnowledgeBaseStamp:
            return stamp

        flat_row = {"id": "paper.lightrag-2025#c1", "level": "claim", "title": "Claim", "body": "Full text."}
        server = _server(
            SqlResult(rows=(), columns=()),  # no matching row in the leveled surface, so it falls back
            collection="workdocs",
            stamp=source,
            flat_result=SqlResult(rows=(flat_row,), columns=tuple(flat_row)),
        )

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "paper.lightrag-2025#c1"})

        unit = json.loads(_text(result))
        assert unit["knowledge_base"]["slug"] == "workdocs"

    async def test_a_routed_flat_fetch_returns_an_exact_qualified_ref(self):
        stamp = KnowledgeBaseStamp(slug="research")

        async def source() -> KnowledgeBaseStamp:
            return stamp

        flat_row = {"id": "paper.x#c1", "level": "claim", "title": "Claim", "body": "Full text."}
        server = _server(
            SqlResult(rows=(), columns=()),
            collection="research",
            stamp=source,
            flat_result=SqlResult(rows=(flat_row,), columns=tuple(flat_row)),
        )

        async with connect_in_memory(server) as client:
            await client.initialize()
            result = await client.call_tool(FETCH, {"unit_id": "unit:research/paper.x#c1"})

        unit = json.loads(_text(result))
        assert unit["id"] == "paper.x#c1"
        assert unit["ref"] == "unit:research/paper.x#c1"
        assert unit["knowledge_base"] == {"slug": "research", "indexed_at": None, "changed_since": None}

    async def test_an_unknown_id_never_reads_the_stamp(self):
        calls = 0

        async def source() -> KnowledgeBaseStamp:
            nonlocal calls
            calls += 1
            return KnowledgeBaseStamp(slug="workdocs", changed_since=3)

        server = _server(SqlResult(rows=(), columns=()), collection="workdocs", stamp=source)

        async with connect_in_memory(server) as client:
            await client.initialize()
            failed = await client.call_tool(FETCH, {"unit_id": "mu.does-not-exist"})

        assert failed.isError is True
        assert calls == 0
