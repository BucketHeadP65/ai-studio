"""Unit tests for the RCA helpers — saved-SQL loader, report scaffold, prompt, client.

All hermetic: no network, no Logfire, no DB. The Logfire Query-API call is exercised through a
hand-written fake that implements the :class:`~engram.evals.rca.internal_contracts.SupportsLogfireQuery` port and
returns scripted rows; the real :class:`LogfireQueryClient` wrapper is only construct-tested with an
`httpx.MockTransport` so a stray query would raise rather than touch the wire.

The deliverable these tests pin down:

* the **saved-SQL on-disk format** — `-- name: <key>` blocks, the SQL
  running until the next `-- name:` or EOF, whitespace stripped, leading non-`name` comments and
  blank lines tolerated, an unnamed/empty key handled;
* the **report scaffold** assembling a structured report from the loaded queries + a query client,
  surfacing the returned rows and tolerating a query name that is absent from the loaded file;
* the **RCA prompt** carrying the RCA instructions (latency-dominating tool calls, recall@5 == 0
  cases and why) grounded in the traces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from core.settings import LogfireSettings

from engram.evals.rca.contracts import (
    build_logfire_query_client,
    build_rca_prompt,
    build_rca_report,
    load_named_queries,
    render_rca_report_markdown,
)
from engram.evals.rca.domain.rca_report import RcaReport

if TYPE_CHECKING:
    from logfire.query_client import RowQueryResults


_NOW = datetime(2026, 10, 20, tzinfo=UTC)


class _FakeQueryClient:
    """A scriptable :class:`~engram.evals.rca.internal_contracts.SupportsLogfireQuery` — returns canned rows, no network.

    `results` maps an exact SQL string to the rows that query should return; `calls` records every
    SQL the scaffold ran (both public per the fakes convention — tests reassign / read them).
    """

    def __init__(self, results: dict[str, list[dict[str, object]]]) -> None:
        self.results = results
        self.calls: list[str] = []
        self.windows: list[datetime] = []

    def query_json_rows(self, sql: str, *, min_timestamp: datetime) -> RowQueryResults:
        self.calls.append(sql)
        self.windows.append(min_timestamp)
        rows = self.results.get(sql, [])
        return {"columns": [], "rows": rows}


# --------------------------------------------------------------------------------------------------
# Saved-SQL loader
# --------------------------------------------------------------------------------------------------


def test_load_named_queries_parses_multiple_named_blocks() -> None:
    text = (
        "-- name: slowest_tool_spans\n"
        "SELECT span_name, duration FROM records\n"
        "ORDER BY duration DESC\n"
        "-- name: token_outliers\n"
        "SELECT trace_id, tokens FROM records\n"
    )

    queries = load_named_queries(text)

    assert set(queries) == {"slowest_tool_spans", "token_outliers"}
    assert queries["slowest_tool_spans"] == "SELECT span_name, duration FROM records\nORDER BY duration DESC"
    assert queries["token_outliers"] == "SELECT trace_id, tokens FROM records"


def test_load_named_queries_ignores_leading_comments_and_blank_lines() -> None:
    text = (
        "-- queries.sql — engram RCA saved SQL (task 2.36)\n"
        "-- this whole header precedes the first named block\n"
        "\n"
        "-- name: lowest_recall_cases\n"
        "\n"
        "SELECT case_name, recall FROM records WHERE recall = 0\n"
        "\n"
    )

    queries = load_named_queries(text)

    assert set(queries) == {"lowest_recall_cases"}
    # Surrounding blank lines inside the block are stripped.
    assert queries["lowest_recall_cases"] == "SELECT case_name, recall FROM records WHERE recall = 0"


def test_load_named_queries_strips_trailing_whitespace_per_block() -> None:
    text = "-- name: a\nSELECT 1   \n   \n-- name: b\n   SELECT 2\n"

    queries = load_named_queries(text)

    assert queries["a"] == "SELECT 1"
    assert queries["b"] == "SELECT 2"


def test_load_named_queries_handles_an_unnamed_name_marker() -> None:
    # A `-- name:` with no key (or only whitespace) must not crash and must not create a real entry.
    text = "-- name:   \nSELECT 1\n-- name: real\nSELECT 2\n"

    queries = load_named_queries(text)

    assert "real" in queries
    assert queries["real"] == "SELECT 2"
    assert "" not in queries


def test_load_named_queries_returns_empty_for_a_file_with_no_named_blocks() -> None:
    assert load_named_queries("-- just a comment\n\nSELECT 1\n") == {}


def test_load_named_queries_drops_a_named_block_with_an_empty_body() -> None:
    text = "-- name: empty\n\n-- name: filled\nSELECT 1\n"

    queries = load_named_queries(text)

    assert "filled" in queries
    assert "empty" not in queries  # no SQL body → not a usable query


def test_load_named_queries_accepts_a_path(tmp_path) -> None:
    path = tmp_path / "queries.sql"
    path.write_text("-- name: from_disk\nSELECT 42\n", encoding="utf-8")

    queries = load_named_queries(path)

    assert queries == {"from_disk": "SELECT 42"}


# --------------------------------------------------------------------------------------------------
# Report scaffold
# --------------------------------------------------------------------------------------------------


def test_build_rca_report_surfaces_rows_per_query() -> None:
    queries = {"slowest_tool_spans": "SELECT 1", "lowest_recall_cases": "SELECT 2"}
    client = _FakeQueryClient(
        {"SELECT 1": [{"span_name": "search_tool", "p95_ms": 812.0}], "SELECT 2": [{"case_name": "paper-a-0", "recall_at_5": 0.0}]}
    )

    report = build_rca_report(client, queries, now=_NOW, query_names=["slowest_tool_spans", "lowest_recall_cases"])

    assert isinstance(report, RcaReport)
    assert [section.name for section in report.sections] == ["slowest_tool_spans", "lowest_recall_cases"]

    by_name = {section.name: section for section in report.sections}
    assert by_name["slowest_tool_spans"].rows == [{"span_name": "search_tool", "p95_ms": 812.0}]
    assert by_name["slowest_tool_spans"].error is None
    assert by_name["lowest_recall_cases"].rows == [{"case_name": "paper-a-0", "recall_at_5": 0.0}]
    # The scaffold ran exactly the SQL behind the requested names, in order.
    assert client.calls == ["SELECT 1", "SELECT 2"]
    # Each query reads the seven days before now.
    assert client.windows == [datetime(2026, 10, 13, tzinfo=UTC)] * 2


def test_build_rca_report_tolerates_a_missing_query_name() -> None:
    client = _FakeQueryClient({"SELECT 1": [{"x": 1}]})

    report = build_rca_report(client, {"present": "SELECT 1"}, now=_NOW, query_names=["present", "absent"])

    by_name = {section.name: section for section in report.sections}
    assert by_name["present"].rows == [{"x": 1}]
    # The absent name yields a section with no rows and a recorded reason — not a crash.
    assert by_name["absent"].rows == []
    assert by_name["absent"].error is not None
    assert "absent" in by_name["absent"].error
    # The scaffold never tried to run SQL for the missing name.
    assert client.calls == ["SELECT 1"]


def test_build_rca_report_defaults_to_all_loaded_queries() -> None:
    client = _FakeQueryClient({"SELECT 1": [{"a": 1}], "SELECT 2": [{"b": 2}]})

    report = build_rca_report(client, {"one": "SELECT 1", "two": "SELECT 2"}, now=_NOW)

    assert {section.name for section in report.sections} == {"one", "two"}


def test_render_rca_report_markdown_includes_query_names_and_row_counts() -> None:
    client = _FakeQueryClient({"SELECT 1": [{"span_name": "fetch_tool", "p95_ms": 410.0}]})
    report = build_rca_report(client, {"slowest_tool_spans": "SELECT 1"}, now=_NOW)

    markdown = render_rca_report_markdown(report)

    assert "slowest_tool_spans" in markdown
    assert "fetch_tool" in markdown  # the row content is rendered, not just the header
    assert "1" in markdown  # a row count surfaces somewhere


def test_render_rca_report_markdown_notes_an_empty_or_errored_section() -> None:
    client = _FakeQueryClient({})
    report = build_rca_report(client, {"present": "SELECT nothing"}, now=_NOW, query_names=["present", "missing"])

    markdown = render_rca_report_markdown(report)

    assert "missing" in markdown  # the absent-name section is still rendered (graceful)
    assert "present" in markdown


# --------------------------------------------------------------------------------------------------
# RCA prompt
# --------------------------------------------------------------------------------------------------


def test_build_rca_prompt_contains_the_key_rca_instructions() -> None:
    prompt = build_rca_prompt()

    lowered = prompt.lower()
    assert "latency" in lowered  # which tool calls dominate latency
    assert "tool" in lowered
    assert "recall" in lowered  # which cases have recall@5 == 0 and why
    assert "logfire" in lowered  # the deep store the model is told to query (MCP path)
    # It must demand grounding in the traces rather than speculation (self-verification).
    assert "trace" in lowered


def test_build_rca_prompt_can_embed_a_focus_and_known_query_names() -> None:
    prompt = build_rca_prompt(focus="the nightly regression run", query_names=["slowest_tool_spans", "token_outliers"])

    assert "the nightly regression run" in prompt
    assert "slowest_tool_spans" in prompt
    assert "token_outliers" in prompt


def test_build_rca_prompt_is_pure() -> None:
    # A pure function: same inputs → identical text, no hidden state.
    assert build_rca_prompt() == build_rca_prompt()


# --------------------------------------------------------------------------------------------------
# Query-client wrapper (construct-only / offline)
# --------------------------------------------------------------------------------------------------


def test_build_logfire_query_client_builds_a_client_without_network() -> None:
    # An explicit base_url avoids logfire's token-parsing path; a MockTransport guarantees no real
    # socket is opened even if a query were issued. Construct-only per the task spec — we do not drive
    # a query through the real client (that would re-test logfire's own response decoder). The client
    # is returned as the SupportsLogfireQuery port, so we assert that one-method surface is present.
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    resource = LogfireSettings(read_token="r", base_url="https://logfire-eu.pydantic.dev")

    client = build_logfire_query_client(resource, transport=transport)

    assert callable(client.query_json_rows)


def test_build_logfire_query_client_requires_a_read_token() -> None:
    # The Query API is read-token authed; a bare resource (empty token) is a misconfiguration the
    # factory should reject loudly rather than build a client that 401s at query time.
    with pytest.raises(ValueError, match="read_token"):
        build_logfire_query_client(LogfireSettings(read_token=""))
