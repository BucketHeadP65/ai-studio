"""Tests for core.ai.format_maps."""

from __future__ import annotations

import logging

from typing import TYPE_CHECKING, Annotated, Any

import pytest

from pydantic import AfterValidator, TypeAdapter, ValidationInfo
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from core.ai.format_maps.contracts import (
    CsvOutliner,
    DiffOutliner,
    FileContentCache,
    FormatMapResult,
    FormatMaps,
    JsonOutliner,
    ListingOutliner,
    MappedTool,
    MarkdownOutliner,
    command_starts,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _document() -> str:
    setup = "The setup steps are documented here in enough detail to matter.\n" * 20
    usage = "The usage flow is documented here in enough detail to matter.\n" * 20
    install = "Install with pip, then configure the environment as described.\n" * 10
    return (
        f"Intro line before any heading.\n\n# Guide\n\nOverview paragraph.\n\n"
        f"## Setup\n\n{setup}\n### Install\n\n{install}\n## Usage\n\n{usage}"
    )


def _csv(rows: int) -> str:
    lines = ["order_id,region,total"]
    lines.extend(f"{n},EMEA,{n * 1.5:.2f}" for n in range(1, rows + 1))
    return "\n".join(lines)


def _driver(tool_name: str, calls: Sequence[dict[str, Any] | str]) -> FunctionModel:
    """A model that issues the scripted tool calls in order, then says done."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = sum(1 for m in messages if isinstance(m, ModelResponse))
        if step < len(calls):
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=calls[step], tool_call_id=f"c{step}")])
        return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(respond)


def _docs_agent(cap: FormatMaps[object], page: str, calls: Sequence[dict[str, Any] | str]) -> Agent[object, str]:
    agent: Agent[object, str] = Agent(_driver("read_docs", calls), capabilities=[cap])

    @agent.tool_plain
    def read_docs(topic: str) -> str:
        return page

    return agent


def _suffixed(value: str, info: ValidationInfo) -> str:
    """Appends the validation context's `suffix`, so a test sees whether the context reached the tool's validator."""
    context = info.context if isinstance(info.context, dict) else {}
    return value + str(context.get("suffix", ""))


def _returns(messages: list[ModelMessage], name: str) -> list[ToolReturnPart]:
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart) and p.tool_name == name]


def _retries(messages: list[ModelMessage], name: str) -> list[RetryPromptPart]:
    return [p for m in messages for p in m.parts if isinstance(p, RetryPromptPart) and p.tool_name == name]


def _map_content(part: ToolReturnPart) -> dict[str, Any]:
    """The part's map envelope, validated at the boundary so tests read it typed."""
    return TypeAdapter(dict[str, Any]).validate_python(part.content)


# ---------------------------------------------------------------------------
# Markdown outliner
# ---------------------------------------------------------------------------


class TestMarkdownOutliner:
    def test_a_document_maps_to_heading_parts(self):
        out = MarkdownOutliner().map(_document())

        assert out is not None
        parts = out["parts"]
        assert isinstance(parts, list)
        assert [stub["part"] for stub in parts] == ["(intro)", "Guide", "Setup", "Install", "Usage"]
        assert [stub.get("level") for stub in parts] == [1, 1, 2, 3, 2]
        assert all(isinstance(stub.get("size_chars"), int) and stub.get("size_chars", 0) > 0 for stub in parts)

    def test_snippets_are_first_body_lines_clamped_with_a_visible_marker(self):
        out = MarkdownOutliner(snippet_chars=20).map(_document())

        assert out is not None
        parts = out["parts"]
        setup = next(stub for stub in parts if stub["part"] == "Setup")
        assert setup.get("snippet") == "The setup steps are ..."

    def test_a_bodyless_section_carries_no_snippet(self):
        body = "A body line under the full heading, said several ways.\n" * 5
        out = MarkdownOutliner().map(f"# Doc\n## Empty\n## Full\n{body}")

        assert out is not None
        parts = out["parts"]
        assert "snippet" not in next(stub for stub in parts if stub["part"] == "Empty")
        assert "snippet" in next(stub for stub in parts if stub["part"] == "Full")

    def test_slicing_a_section_includes_its_subsections(self):
        section = MarkdownOutliner().slice(_document(), "Setup")

        assert section is not None
        assert section.startswith("## Setup")
        assert "### Install" in section
        assert "## Usage" not in section

    def test_slicing_the_intro_returns_text_before_the_first_heading(self):
        section = MarkdownOutliner().slice(_document(), "(intro)")

        assert section is not None
        assert section.startswith("Intro line")
        assert "# Guide" not in section

    def test_duplicate_titles_get_numbered_parts_that_slice_apart(self):
        notes = "First notes body with plenty of words to look real.\n" * 5
        other = "Second notes body, different words entirely here.\n" * 5
        doc = f"# Doc\n\nOpening.\n\n## Notes\n\n{notes}\n## Notes\n\n{other}"
        outliner = MarkdownOutliner()

        out = outliner.map(doc)

        assert out is not None
        assert [stub["part"] for stub in out["parts"]] == ["Doc", "Notes", "Notes (2)"]
        first = outliner.slice(doc, "Notes")
        second = outliner.slice(doc, "Notes (2)")
        assert first is not None
        assert "First notes" in first
        assert "Second notes" not in first
        assert second is not None
        assert "Second notes" in second

    def test_headings_inside_code_fences_are_code(self):
        fenced = "```bash\n# not a heading\n## also code\n```\nAfter the fence.\n"
        out = MarkdownOutliner().map(f"{_document()}\n## Appendix\n\n{fenced}")

        assert out is not None
        assert "not a heading" not in [stub["part"] for stub in out["parts"]]

    def test_an_all_top_level_document_still_maps(self):
        body_a = "Alpha body text line.\n" * 5
        body_b = "Beta body text line.\n" * 5
        out = MarkdownOutliner().map(f"# Alpha\n{body_a}\n# Beta\n{body_b}")

        assert out is not None
        assert [stub["part"] for stub in out["parts"]] == ["Alpha", "Beta"]

    def test_fewer_than_two_headings_is_no_map(self):
        assert MarkdownOutliner().map("# Only\n\nProse under a single heading.\n") is None
        assert MarkdownOutliner().map("Nothing but linear prose at length. " * 40) is None

    def test_an_unknown_part_is_none(self):
        assert MarkdownOutliner().slice(_document(), "Nonexistent") is None

    def test_setext_headings_are_structure(self):
        body_a = "Alpha body text line, said at honest length.\n" * 5
        body_b = "Beta body text line, said at honest length.\n" * 5
        doc = f"Alpha\n=====\n{body_a}\nBeta\n=====\n{body_b}"
        outliner = MarkdownOutliner()

        out = outliner.map(doc)

        assert out is not None
        assert [stub["part"] for stub in out["parts"]] == ["Alpha", "Beta"]
        assert [stub.get("level") for stub in out["parts"]] == [1, 1]
        alpha = outliner.slice(doc, "Alpha")
        assert alpha is not None
        assert alpha.startswith("Alpha\n=====")
        assert "Beta" not in alpha
        assert next(stub for stub in out["parts"] if stub["part"] == "Alpha").get("snippet", "").startswith("Alpha body")

    def test_indented_code_hashes_are_not_headings(self):
        body = "Prose line at honest length for the section body.\n" * 5
        doc = f"# Doc\n{body}\n    # indented code, not a heading\n\n## Real\n{body}"

        out = MarkdownOutliner().map(doc)

        assert out is not None
        assert [stub["part"] for stub in out["parts"]] == ["Doc", "Real"]

    def test_slicing_unmappable_content_is_none(self):
        assert MarkdownOutliner().slice("plain prose with no headings", "Setup") is None

    def test_frontmatter_is_intro_text_and_its_comments_are_not_headings(self):
        doc = f"---\nid: doc.guide\n# a yaml comment, not a heading\n---\n{_document()}"
        outliner = MarkdownOutliner()

        out = outliner.map(doc)

        assert out is not None
        parts = [stub["part"] for stub in out["parts"]]
        assert "a yaml comment, not a heading" not in parts
        intro = outliner.slice(doc, "(intro)")
        assert intro is not None
        assert "id: doc.guide" in intro
        assert next(stub for stub in out["parts"] if stub["part"] == "(intro)").get("snippet") == "Intro line before any heading."

    def test_an_unterminated_frontmatter_fence_is_ordinary_text(self):
        out = MarkdownOutliner().map(f"---\nnever closed\n\n{_document()}")

        assert out is not None
        assert next(stub for stub in out["parts"] if stub["part"] == "(intro)").get("snippet") == "---"


# ---------------------------------------------------------------------------
# CSV outliner
# ---------------------------------------------------------------------------


class TestCsvOutliner:
    def test_a_csv_maps_to_columns_rows_and_range_parts(self):
        out = CsvOutliner().map(_csv(450))

        assert out is not None
        assert out.get("row_count") == 450
        columns = out.get("columns")
        assert columns is not None
        assert [col["name"] for col in columns] == ["order_id", "region", "total"]
        assert [col["type"] for col in columns] == ["int", "text", "float"]
        sample = out.get("sample_rows")
        assert sample is not None
        assert sample[0] == "order_id,region,total"
        assert len(sample) == 6
        assert [stub["part"] for stub in out["parts"]] == ["rows 1-200", "rows 201-400", "rows 401-450"]
        assert all(isinstance(stub.get("size_chars"), int) and stub.get("size_chars", 0) > 0 for stub in out["parts"])

    def test_slicing_rows_always_reincludes_the_header(self):
        section = CsvOutliner().slice(_csv(10), "rows 2-3")

        assert section == "order_id,region,total\n2,EMEA,3.00\n3,EMEA,4.50"

    def test_an_arbitrary_in_range_slice_is_served_with_the_end_clamped(self):
        section = CsvOutliner().slice(_csv(10), "rows 9-999")

        assert section is not None
        assert section.splitlines()[0] == "order_id,region,total"
        assert len(section.splitlines()) == 3

    def test_malformed_or_out_of_range_parts_are_none(self):
        outliner = CsvOutliner()
        assert outliner.slice(_csv(10), "rows eleven-twelve") is None
        assert outliner.slice(_csv(10), "rows 11-12") is None
        assert outliner.slice(_csv(10), "rows 3-2") is None
        assert outliner.slice(_csv(10), "Setup") is None

    def test_a_quoted_field_with_a_newline_is_one_row(self):
        content = 'id,note\n1,"first line\nsecond line"\n2,plain'
        outliner = CsvOutliner()

        out = outliner.map(content)

        assert out is not None
        assert out.get("row_count") == 2
        section = outliner.slice(content, "rows 1-1")
        assert section is not None
        assert "first line" in section
        assert "second line" in section
        assert "plain" not in section

    def test_a_header_only_or_empty_csv_is_no_map(self):
        assert CsvOutliner().map("order_id,region,total") is None
        assert CsvOutliner().map("") is None
        assert CsvOutliner().slice("order_id,region,total", "rows 1-1") is None

    def test_empty_values_do_not_break_type_inference(self):
        out = CsvOutliner().map("a,b\n,\n1,x")

        assert out is not None
        assert [col["type"] for col in out.get("columns", [])] == ["int", "text"]


# ---------------------------------------------------------------------------
# Listing outliner
# ---------------------------------------------------------------------------


def _tickets(count: int) -> list[dict[str, str]]:
    return [{"summary": f"Ticket {n} headline", "body": f"Long body of ticket {n}. " * 10} for n in range(1, count + 1)]


def _tickets_json(count: int) -> str:
    import json

    return json.dumps(_tickets(count))


class TestListingOutliner:
    def test_a_listing_maps_to_item_parts_with_the_source_summaries(self):
        out = ListingOutliner().map(_tickets_json(3))

        assert out is not None
        assert out.get("item_count") == 3
        parts = out["parts"]
        assert [stub["part"] for stub in parts] == ["item 1", "item 2", "item 3"]
        assert [stub.get("summary") for stub in parts] == ["Ticket 1 headline", "Ticket 2 headline", "Ticket 3 headline"]
        assert all(isinstance(stub.get("size_chars"), int) and stub.get("size_chars", 0) > 0 for stub in parts)

    def test_slicing_an_item_returns_its_json(self):
        section = ListingOutliner().slice(_tickets_json(3), "item 2")

        assert section is not None
        assert '"Ticket 2 headline"' in section
        assert "Ticket 1" not in section

    def test_slicing_a_range_returns_a_json_array(self):
        section = ListingOutliner().slice(_tickets_json(5), "items 2-3")

        assert section is not None
        assert section.startswith("[")
        assert "Ticket 2" in section
        assert "Ticket 3" in section
        assert "Ticket 4" not in section

    def test_an_out_of_range_or_malformed_part_is_none(self):
        outliner = ListingOutliner()
        assert outliner.slice(_tickets_json(3), "item 4") is None
        assert outliner.slice(_tickets_json(3), "items 3-2") is None
        assert outliner.slice(_tickets_json(3), "Setup") is None

    def test_an_item_without_the_summary_field_is_no_map(self):
        import json

        items = [*_tickets(2), {"body": "no summary here"}]
        assert ListingOutliner().map(json.dumps(items)) is None

    def test_non_listing_json_and_non_json_are_no_map(self):
        outliner = ListingOutliner()
        assert outliner.map('{"summary": "one object, not a listing"}') is None
        assert outliner.map("[]") is None
        assert outliner.map("[1, 2]") is None
        assert outliner.map("not json at all") is None
        assert outliner.slice("not json at all", "item 1") is None

    def test_a_custom_summary_field(self):
        import json

        items = [{"title": "First", "body": "x"}, {"title": "Second", "body": "y"}]
        out = ListingOutliner(summary_field="title").map(json.dumps(items))

        assert out is not None
        assert [stub.get("summary") for stub in out["parts"]] == ["First", "Second"]


# ---------------------------------------------------------------------------
# Diff outliner
# ---------------------------------------------------------------------------


def _patch(*files: tuple[str, int]) -> str:
    sections: list[str] = []
    for path, adds in files:
        body = "\n".join(f"+added line {n} of {path}" for n in range(adds))
        sections.append(
            f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n"
            f"--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,{adds + 2} @@ def anchor():\n-removed line\n{body}"
        )
    return "\n".join(sections) + "\n"


class TestDiffOutliner:
    def test_a_patch_maps_to_file_parts_with_change_counts(self):
        out = DiffOutliner().map(_patch(("src/billing/invoice.py", 4), ("tests/test_invoice.py", 2)))

        assert out is not None
        assert out.get("file_count") == 2
        parts = out["parts"]
        assert [stub["part"] for stub in parts] == ["src/billing/invoice.py", "tests/test_invoice.py"]
        assert [stub.get("changes") for stub in parts] == ["+4 -1", "+2 -1"]
        assert all(isinstance(stub.get("size_chars"), int) and stub.get("size_chars", 0) > 0 for stub in parts)

    def test_text_before_the_first_marker_is_the_intro_part(self):
        doc = "commit abc123\nAuthor: Dev <dev@example.com>\n\n    fix totals\n\n" + _patch(("a.py", 2), ("b.py", 2))
        outliner = DiffOutliner()

        out = outliner.map(doc)

        assert out is not None
        assert [stub["part"] for stub in out["parts"]][:1] == ["(intro)"]
        intro = outliner.slice(doc, "(intro)")
        assert intro is not None
        assert "commit abc123" in intro
        assert "diff --git" not in intro

    def test_slicing_a_file_returns_only_its_section(self):
        doc = _patch(("src/billing/invoice.py", 4), ("tests/test_invoice.py", 2))

        section = DiffOutliner().slice(doc, "tests/test_invoice.py")

        assert section is not None
        assert section.startswith("diff --git a/tests/test_invoice.py")
        assert "invoice.py b/src" not in section

    def test_a_single_file_patch_is_no_map(self):
        assert DiffOutliner().map(_patch(("only.py", 3))) is None
        assert DiffOutliner().map("no markers here at all\n") is None

    def test_an_unparseable_header_falls_back_to_a_positional_token(self):
        doc = 'diff --git "a/we ird.py" "b/we ird.py"\n+x\n' + _patch(("normal.py", 2))

        out = DiffOutliner().map(doc)

        assert out is not None
        assert [stub["part"] for stub in out["parts"]] == ["file 1", "normal.py"]

    def test_duplicate_paths_get_numbered_parts(self):
        doc = _patch(("same.py", 2), ("same.py", 3))

        out = DiffOutliner().map(doc)

        assert out is not None
        assert [stub["part"] for stub in out["parts"]] == ["same.py", "same.py (2)"]

    def test_an_unknown_part_is_none(self):
        assert DiffOutliner().slice(_patch(("a.py", 2), ("b.py", 2)), "missing.py") is None
        assert DiffOutliner().slice("no markers here at all", "a.py") is None


# ---------------------------------------------------------------------------
# Json outliner
# ---------------------------------------------------------------------------


def _api_payload() -> str:
    import json

    return json.dumps(
        {
            "meta": {"query": "unpaid invoices", "took_ms": 41},
            "description": "This report covers every invoice issued in Q3 across all regions. " * 12,
            "items": [{"id": f"inv_{n:04d}", "amount": n * 13.5} for n in range(1, 41)],
            "config": {"currency": "EUR", "tax_rate": 0.24},
            "flags": ["beta_totals"],
        }
    )


class TestJsonOutliner:
    def test_small_values_arrive_whole_and_large_ones_adaptively(self):
        out = JsonOutliner().map(_api_payload())

        assert out is not None
        stubs = {stub["part"]: stub for stub in out["parts"]}
        assert stubs["meta"].get("value") == {"query": "unpaid invoices", "took_ms": 41}
        assert "type" not in stubs["meta"], "a whole value answers every question type and size exist for"
        assert "size_chars" not in stubs["meta"], "a whole value answers every question type and size exist for"
        assert stubs["config"].get("value") == {"currency": "EUR", "tax_rate": 0.24}
        assert stubs["flags"].get("value") == ["beta_totals"]
        description = stubs["description"]
        assert description.get("snippet", "").startswith("This report covers")
        assert description.get("snippet", "").endswith("...")
        assert isinstance(description.get("size_chars"), int)
        assert description.get("type") == "str"
        items = stubs["items"]
        assert items.get("element_keys") == ["id", "amount"]
        assert items.get("first_item") == {"id": "inv_0001", "amount": 13.5}
        assert items.get("type") == "array[40]"
        assert isinstance(items.get("size_chars"), int)

    def test_a_large_object_value_shows_its_keys(self):
        import json

        payload = json.dumps({"config": {f"key_{n}": "x" * 30 for n in range(10)}})
        out = JsonOutliner(small_chars=50).map(payload)

        assert out is not None
        assert out["parts"][0].get("keys") == [f"key_{n}" for n in range(10)]

    def test_a_large_array_of_scalars_carries_a_sample_without_element_keys(self):
        import json

        out = JsonOutliner(small_chars=20).map(json.dumps({"values": list(range(50))}))

        assert out is not None
        stub = out["parts"][0]
        assert stub.get("first_item") == 0
        assert "element_keys" not in stub

    def test_non_object_json_and_non_json_are_no_map(self):
        outliner = JsonOutliner()
        assert outliner.map("[1, 2]") is None
        assert outliner.map('"just a string"') is None
        assert outliner.map("{}") is None
        assert outliner.map("not json") is None

    def test_slicing_a_key_returns_its_json(self):
        section = JsonOutliner().slice(_api_payload(), "config")

        assert section is not None
        import json

        assert json.loads(section) == {"currency": "EUR", "tax_rate": 0.24}

    def test_unknown_keys_and_unsliceable_content_are_none(self):
        outliner = JsonOutliner()
        assert outliner.slice(_api_payload(), "missing") is None
        assert outliner.slice("not json", "meta") is None

    def test_a_large_scalar_value_carries_type_and_size_alone(self):
        import json

        out = JsonOutliner(small_chars=20).map(json.dumps({"big": int("9" * 40)}))

        assert out is not None
        stub = out["parts"][0]
        assert stub.get("type") == "int"
        assert isinstance(stub.get("size_chars"), int)
        assert "value" not in stub
        assert "snippet" not in stub


# ---------------------------------------------------------------------------
# The wrapper through an agent
# ---------------------------------------------------------------------------


class TestFormatMapsAgent:
    async def test_the_runs_validation_context_reaches_a_mapped_tools_own_validator(self):
        cap: FormatMaps[object] = FormatMaps(tools={"echo": "markdown"})
        agent: Agent[object, str] = Agent(_driver("echo", [{"text": "hi"}]), capabilities=[cap], validation_context={"suffix": "!"})

        @agent.tool_plain
        def echo(text: Annotated[str, AfterValidator(_suffixed)]) -> str:
            return text

        result = await agent.run("go")

        assert _returns(result.all_messages(), "echo")[0].content == "hi!"

    async def test_a_small_return_passes_through_whole(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"})
        page = "# A\n\nshort\n\n# B\n\nalso short"
        agent = _docs_agent(cap, page, [{"topic": "x"}])

        result = await agent.run("go")

        assert _returns(result.all_messages(), "read_docs")[0].content == page

    async def test_a_large_return_answers_with_the_map_envelope(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200)
        agent = _docs_agent(cap, _document(), [{"topic": "x"}])

        result = await agent.run("go")

        content = _map_content(_returns(result.all_messages(), "read_docs")[0])
        assert content["format"] == "markdown"
        assert isinstance(content["size_chars"], int)
        assert content["how"] == (
            "This result is large, so this is its map. Call this tool again with the same arguments plus part='<part>' to get one part."
        )
        assert [stub["part"] for stub in content["parts"]][:2] == ["(intro)", "Guide"]

    async def test_calling_with_part_slices_the_cached_content_without_reinvoking(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200, cache=FileContentCache(tmp_path))
        calls: list[dict[str, Any] | str] = [{"topic": "x"}, {"topic": "x", "part": "Usage"}, {"topic": "x", "part": "Setup"}]
        agent: Agent[object, str] = Agent(_driver("read_docs", calls), capabilities=[cap])
        invocations = 0

        @agent.tool_plain
        def read_docs(topic: str) -> str:
            nonlocal invocations
            invocations += 1
            return _document()

        result = await agent.run("go")

        returned = _returns(result.all_messages(), "read_docs")
        assert isinstance(returned[1].content, str)
        assert returned[1].content.startswith("## Usage")
        assert isinstance(returned[2].content, str)
        assert returned[2].content.startswith("## Setup")
        assert invocations == 1, "both part calls must slice the cached content, not re-run the tool"

    async def test_part_works_with_json_string_args(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200, cache=FileContentCache(tmp_path))
        agent = _docs_agent(cap, _document(), ['{"topic": "x"}', '{"topic": "x", "part": "Setup"}'])

        result = await agent.run("go")

        content = _returns(result.all_messages(), "read_docs")[1].content
        assert isinstance(content, str)
        assert content.startswith("## Setup")

    async def test_an_unknown_part_asks_the_model_to_retry_with_the_available_parts(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200, cache=FileContentCache(tmp_path))
        calls: list[dict[str, Any] | str] = [{"topic": "x"}, {"topic": "x", "part": "Bogus"}, {"topic": "x", "part": "Usage"}]
        agent = _docs_agent(cap, _document(), calls)

        result = await agent.run("go")

        retry = _retries(result.all_messages(), "read_docs")
        assert retry
        assert "Bogus" in retry[0].model_response()
        assert "Usage" in retry[0].model_response()

    async def test_a_part_call_without_cached_content_teaches_the_refresh(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200, cache=FileContentCache(tmp_path))
        agent = _docs_agent(cap, _document(), [{"topic": "x", "part": "Usage"}, {"topic": "x"}])

        result = await agent.run("go")

        retry = _retries(result.all_messages(), "read_docs")
        assert retry
        assert (
            "No fetched content is available to take a part of. Call this tool again without `part` to fetch it fresh. "
            "The map comes back, and then you can select a part."
        ) in retry[0].model_response()

    async def test_a_part_miss_is_logged_for_telemetry(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200, cache=FileContentCache(tmp_path))
        agent = _docs_agent(cap, _document(), [{"topic": "x", "part": "Usage"}, {"topic": "x"}])

        with caplog.at_level(logging.INFO, logger="core.ai.format_maps"):
            await agent.run("go")

        misses = [record for record in caplog.records if "part miss" in record.getMessage()]
        assert misses
        assert "read_docs" in misses[0].getMessage()
        assert "refetch=False" in misses[0].getMessage()

    async def test_refetch_on_miss_reinvokes_silently(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(
            tools={"read_docs": "markdown"}, over=200, cache=FileContentCache(tmp_path), refetch_on_miss=True
        )
        agent = _docs_agent(cap, _document(), [{"topic": "x", "part": "Usage"}])

        result = await agent.run("go")

        content = _returns(result.all_messages(), "read_docs")[0].content
        assert isinstance(content, str)
        assert content.startswith("## Usage")
        assert not _retries(result.all_messages(), "read_docs")

    async def test_a_refetched_non_string_result_has_no_parts(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(tools={"lookup": "markdown"}, over=200, cache=FileContentCache(tmp_path), refetch_on_miss=True)
        calls: list[dict[str, Any] | str] = [{"key": "x", "part": "Setup"}, {"key": "x"}]
        agent: Agent[object, str] = Agent(_driver("lookup", calls), capabilities=[cap])

        @agent.tool_plain
        def lookup(key: str) -> int:
            return 42

        result = await agent.run("go")

        retry = _retries(result.all_messages(), "lookup")
        assert retry
        assert "no parts" in retry[0].model_response()

    async def test_a_per_tool_refetch_override_beats_the_capability_default(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(
            tools={"read_docs": MappedTool(format="markdown", over=200, refetch_on_miss=True)}, cache=FileContentCache(tmp_path)
        )
        agent = _docs_agent(cap, _document(), [{"topic": "x", "part": "Usage"}])

        result = await agent.run("go")

        content = _returns(result.all_messages(), "read_docs")[0].content
        assert isinstance(content, str)
        assert content.startswith("## Usage")

    async def test_part_on_an_unmappable_result_asks_the_model_to_call_without_it(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(
            tools={"read_docs": "markdown"}, over=10, cache=FileContentCache(tmp_path), refetch_on_miss=True
        )
        page = "No headings here, just enough prose to cross the threshold either way."
        agent = _docs_agent(cap, page, [{"topic": "x", "part": "Setup"}, {"topic": "x"}])

        result = await agent.run("go")

        retry = _retries(result.all_messages(), "read_docs")
        assert retry
        assert "This result has no parts to select. Call again without `part`." in retry[0].model_response()
        assert _returns(result.all_messages(), "read_docs")[0].content == page

    async def test_construction_rejects_an_unknown_format_name(self):
        with pytest.raises(ValueError, match=r"unknown format 'markdwon'.*markdown"):
            FormatMaps[object](tools={"read_docs": "markdwon"})
        with pytest.raises(ValueError, match="unknown format 'text'"):
            FormatMaps[object](tools={"run_command": MappedTool(format="text")})

    async def test_construction_accepts_a_custom_outliner_name(self):
        class NullOutliner:
            def map(self, content: str) -> FormatMapResult | None:
                return None

            def slice(self, content: str, part: str) -> str | None:
                return None

        FormatMaps[object](tools={"read_notes": "notes"}, outliners={"notes": NullOutliner()})
        assert NullOutliner().map("anything") is None
        assert NullOutliner().slice("anything", "part") is None

    async def test_a_large_unmappable_return_passes_through(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=10)
        page = "Linear prose without a single heading, long enough to trip the threshold. " * 4
        agent = _docs_agent(cap, page, [{"topic": "x"}])

        result = await agent.run("go")

        assert _returns(result.all_messages(), "read_docs")[0].content == page

    async def test_over_zero_always_maps(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": MappedTool(format="markdown", over=0)})
        page = "# A\n\nshort\n\n# B\n\nalso short"
        agent = _docs_agent(cap, page, [{"topic": "x"}])

        result = await agent.run("go")

        content = _map_content(_returns(result.all_messages(), "read_docs")[0])
        assert content["format"] == "markdown"

    async def test_a_per_tool_over_overrides_the_capability_default(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": MappedTool(format="markdown", over=100_000)}, over=10)
        agent = _docs_agent(cap, _document(), [{"topic": "x"}])

        result = await agent.run("go")

        assert _returns(result.all_messages(), "read_docs")[0].content == _document()

    async def test_a_non_string_return_passes_through(self):
        cap: FormatMaps[object] = FormatMaps(tools={"lookup": "markdown"}, over=0)
        agent: Agent[object, str] = Agent(_driver("lookup", [{"key": "x"}]), capabilities=[cap])

        @agent.tool_plain
        def lookup(key: str) -> dict[str, int]:
            return {"a": 1}

        result = await agent.run("go")

        assert _returns(result.all_messages(), "lookup")[0].content == {"a": 1}

    async def test_a_structured_listing_maps_and_slices_on_the_same_verb(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(tools={"list_tickets": "listing"}, over=200, cache=FileContentCache(tmp_path))
        calls: list[dict[str, Any] | str] = [{"state": "open"}, {"state": "open", "part": "item 2"}]
        agent: Agent[object, str] = Agent(_driver("list_tickets", calls), capabilities=[cap])
        invocations = 0

        @agent.tool_plain
        def list_tickets(state: str) -> list[dict[str, str]]:
            nonlocal invocations
            invocations += 1
            return _tickets(8)

        result = await agent.run("go")

        first, second = _returns(result.all_messages(), "list_tickets")
        envelope = _map_content(first)
        assert envelope["format"] == "listing"
        assert envelope["item_count"] == 8
        assert isinstance(second.content, str)
        assert '"Ticket 2 headline"' in second.content
        assert invocations == 1

    async def test_a_dict_return_maps_under_the_json_format(self, tmp_path: Path):
        import json

        cap: FormatMaps[object] = FormatMaps(tools={"get_report": "json"}, over=200, cache=FileContentCache(tmp_path))
        calls: list[dict[str, Any] | str] = [{"name": "q3"}, {"name": "q3", "part": "config"}]
        agent: Agent[object, str] = Agent(_driver("get_report", calls), capabilities=[cap])

        @agent.tool_plain
        def get_report(name: str) -> dict[str, object]:
            return json.loads(_api_payload())

        result = await agent.run("go")

        first, second = _returns(result.all_messages(), "get_report")
        envelope = _map_content(first)
        assert envelope["format"] == "json"
        assert isinstance(second.content, str)
        assert json.loads(second.content) == {"currency": "EUR", "tax_rate": 0.24}

    async def test_a_small_dict_return_passes_through_as_the_original_object(self):
        cap: FormatMaps[object] = FormatMaps(tools={"get_report": "json"}, over=100_000)
        agent: Agent[object, str] = Agent(_driver("get_report", [{"name": "q3"}]), capabilities=[cap])
        payload: dict[str, object] = {"a": 1, "b": [2, 3]}

        @agent.tool_plain
        def get_report(name: str) -> dict[str, object]:
            return payload

        result = await agent.run("go")

        assert _returns(result.all_messages(), "get_report")[0].content == payload

    async def test_command_starts_matches_stripped_prefixes(self):
        matches = command_starts("git diff", "git show")

        assert matches({"command": "git diff HEAD~1"}) is True
        assert matches({"command": "  git show abc123"}) is True
        assert matches({"command": "git status"}) is False
        assert matches({"other": "git diff"}) is False
        assert matches({"command": 42}) is False

    async def test_command_starts_takes_a_custom_argument_name(self):
        matches = command_starts("git log -p", arg="cmd")

        assert matches({"cmd": "git log -p -3"}) is True
        assert matches({"cmd": "git diff"}) is False

    async def test_a_when_gate_keeps_unmatched_calls_unmapped(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(
            tools={"run_command": MappedTool(format="diff", when=command_starts("git diff"), over=100)}, cache=FileContentCache(tmp_path)
        )
        patch = _patch(("src/a.py", 6), ("src/b.py", 6))
        calls: list[dict[str, Any] | str] = [
            {"command": "git diff HEAD~1"},
            {"command": "git diff HEAD~1", "part": "src/b.py"},
            {"command": "ls -la"},
        ]
        agent: Agent[object, str] = Agent(_driver("run_command", calls), capabilities=[cap])

        @agent.tool_plain
        def run_command(command: str) -> str:
            return patch if command.startswith("git") else "total 0\n" * 40

        result = await agent.run("go")

        first, second, third = _returns(result.all_messages(), "run_command")
        assert _map_content(first)["format"] == "diff"
        assert isinstance(second.content, str)
        assert second.content.startswith("diff --git a/src/b.py")
        assert isinstance(third.content, str)
        assert third.content.startswith("total 0")

    async def test_a_part_call_gated_off_by_when_teaches_no_parts(self, tmp_path: Path):
        cap: FormatMaps[object] = FormatMaps(
            tools={"run_command": MappedTool(format="diff", when=command_starts("git diff"), over=100)}, cache=FileContentCache(tmp_path)
        )
        calls: list[dict[str, Any] | str] = [{"command": "ls -la", "part": "src/a.py"}, {"command": "ls -la"}]
        agent: Agent[object, str] = Agent(_driver("run_command", calls), capabilities=[cap])

        @agent.tool_plain
        def run_command(command: str) -> str:
            return "total 0"

        result = await agent.run("go")

        retry = _retries(result.all_messages(), "run_command")
        assert retry
        assert "no parts" in retry[0].model_response()

    async def test_a_small_structured_listing_passes_through_unserialized(self):
        cap: FormatMaps[object] = FormatMaps(tools={"list_tickets": "listing"}, over=100_000)
        agent: Agent[object, str] = Agent(_driver("list_tickets", [{"state": "open"}]), capabilities=[cap])

        @agent.tool_plain
        def list_tickets(state: str) -> list[dict[str, str]]:
            return _tickets(2)

        result = await agent.run("go")

        assert _returns(result.all_messages(), "list_tickets")[0].content == _tickets(2)

    async def test_non_mapping_arguments_are_rejected_by_the_inner_validator(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200)
        agent = _docs_agent(cap, _document(), ["[1, 2]", {"topic": "x"}])

        result = await agent.run("go")

        assert _retries(result.all_messages(), "read_docs")
        assert _map_content(_returns(result.all_messages(), "read_docs")[0])["format"] == "markdown"

    async def test_a_non_string_part_asks_the_model_to_retry(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200)
        agent = _docs_agent(cap, _document(), [{"topic": "x", "part": 5}, {"topic": "x"}])

        result = await agent.run("go")

        retry = _retries(result.all_messages(), "read_docs")
        assert retry
        assert "must be a string" in retry[0].model_response()

    async def test_a_custom_args_validator_never_sees_part(self):
        from pydantic_ai import FunctionToolset, RunContext, Tool

        seen_kwargs: list[dict[str, Any]] = []

        def read_docs(topic: str) -> str:
            return _document()

        def check_args(ctx: RunContext[object], topic: str) -> None:
            seen_kwargs.append({"topic": topic})

        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200, refetch_on_miss=True)
        agent: Agent[object, str] = Agent(
            _driver("read_docs", [{"topic": "x", "part": "Usage"}]),
            toolsets=[FunctionToolset([Tool(read_docs, args_validator=check_args)])],
            capabilities=[cap],
        )

        result = await agent.run("go")

        assert seen_kwargs == [{"topic": "x"}]
        content = _returns(result.all_messages(), "read_docs")[0].content
        assert isinstance(content, str)
        assert content.startswith("## Usage")

    async def test_a_custom_outliner_overrides_the_stock_registry(self, tmp_path: Path):
        class UpperOutliner:
            def map(self, content: str) -> FormatMapResult | None:
                return {"parts": [{"part": "ALL", "size_chars": len(content)}]}

            def slice(self, content: str, part: str) -> str | None:
                return content.upper() if part == "ALL" else None

        cap: FormatMaps[object] = FormatMaps(
            tools={"read_docs": "markdown"}, over=0, outliners={"markdown": UpperOutliner()}, cache=FileContentCache(tmp_path)
        )
        agent = _docs_agent(cap, "hello page", [{"topic": "x"}, {"topic": "x", "part": "ALL"}])

        result = await agent.run("go")

        first, second = _returns(result.all_messages(), "read_docs")
        assert _map_content(first)["parts"] == [{"part": "ALL", "size_chars": 10}]
        assert second.content == "HELLO PAGE"

    async def test_the_mapped_tool_advertises_part_and_others_are_untouched(self):
        cap: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"})
        seen: dict[str, Mapping[str, Any]] = {}

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            for tool_def in info.function_tools:
                seen[tool_def.name] = tool_def.parameters_json_schema
            step = sum(1 for m in messages if isinstance(m, ModelResponse))
            if step == 0:
                return ModelResponse(parts=[ToolCallPart(tool_name="read_docs", args={"topic": "x"}, tool_call_id="c0")])
            if step == 1:
                return ModelResponse(parts=[ToolCallPart(tool_name="other", args={"key": "x"}, tool_call_id="c1")])
            return ModelResponse(parts=[TextPart(content="done")])

        agent: Agent[object, str] = Agent(FunctionModel(respond), capabilities=[cap])

        @agent.tool_plain
        def read_docs(topic: str) -> str:
            return "page"

        @agent.tool_plain
        def other(key: str) -> str:
            return "x"

        result = await agent.run("go")

        assert seen["read_docs"]["properties"]["part"]["description"] == (
            "Optional: return only this part of the result. A large result answers with a map of named parts. "
            "Pass a part token from that map. Omit it to get the full result or the map."
        )
        assert "part" not in seen["read_docs"].get("required", [])
        assert "part" not in seen["other"]["properties"]
        assert _returns(result.all_messages(), "other")[0].content == "x"

    async def test_an_oversized_map_is_caught_by_tool_output_limits(self, tmp_path: Path):
        from pydantic_ai_harness.tool_output_limits import Band, LocalFileStore, Spill, ToolOutputLimits

        maps: FormatMaps[object] = FormatMaps(tools={"read_docs": "markdown"}, over=200)
        limits: ToolOutputLimits[object] = ToolOutputLimits(
            bands=[Band(over=100, action=Spill(preview_chars=80))], store=LocalFileStore(base_dir=tmp_path)
        )
        agent: Agent[object, str] = Agent(_driver("read_docs", [{"topic": "x"}]), capabilities=[maps, limits])

        @agent.tool_plain
        def read_docs(topic: str) -> str:
            return _document()

        result = await agent.run("go")

        content = _returns(result.all_messages(), "read_docs")[0].content
        assert isinstance(content, str)
        assert "too large" in content


# ---------------------------------------------------------------------------
# The content cache
# ---------------------------------------------------------------------------


class TestFileContentCache:
    async def test_round_trips_across_instances(self, tmp_path: Path):
        await FileContentCache(tmp_path).put("run-1/read_docs.abcd1234", "the page text")

        assert await FileContentCache(tmp_path).get("run-1/read_docs.abcd1234") == "the page text"

    async def test_a_missing_key_is_none(self, tmp_path: Path):
        assert await FileContentCache(tmp_path).get("run-1/read_docs.ffff0000") is None
