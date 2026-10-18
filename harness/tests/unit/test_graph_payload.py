"""What the dashboard draws: the spec, with the registry's types joined back in.

The spec carries no types on purpose -- a node names its step, the registry holds the real
signature -- which is right for a portable artifact and useless on a screen. A box labelled
`investigate / investigate` says nothing about what goes in or comes out, so the server joins
the two before rendering. These tests pin that join, because the types on screen are only
worth anything if they come from the same introspection the validator checks.
"""

from __future__ import annotations

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    Program,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    prepare,
)
from core.ai.workflows.domain.vocabulary import SameAs, Slot

from harness.rendering import graph_payload
from tests.conftest import CallLog, SalesData, register_quarterly_steps

TOOLS = {"investigate": ("search_text", "walk_graph", "claim_records")}
"""What an agentic step holds. The app derives this from how its agents were built; a test
states it, because the point under test is that the payload reports what it was told."""


def _nodes(spec: Workflow, registry: StepRegistry) -> dict[str, dict[str, object]]:
    payload = graph_payload(spec, registry, TOOLS)
    return {str(node["id"]): node for node in payload["nodes"]}


def test_every_node_shows_its_real_signature(quarterly_report: Workflow, registry: StepRegistry) -> None:
    nodes = _nodes(quarterly_report, registry)
    assert nodes["fetch_sales"]["signature"] == "fetch_sales(quarter: string) → SalesData"
    assert nodes["reconcile"]["signature"] == "reconcile(sales: SalesData, costs: CostData) → Ledger"
    assert nodes["quarter"]["signature"] == "string"


def test_a_brought_step_names_its_titled_shapes_not_object() -> None:
    """A shape declared with a title keeps that name through the contract round trip.

    The contract builder turns the shape into a real model and derives the schema back from it;
    pydantic hands the nested model back as a `$ref`, and the signature must read the name off
    it rather than falling back to `object`.
    """
    listing = Briefing(
        name="pending_designs",
        instructions="List every executed design that no accepted ADR absorbs yet, newest first.",
        inputs={"collection": Shape(json_schema={"type": "string"})},
        output=Shape(
            json_schema={
                "type": "array",
                "items": {
                    "type": "object",
                    "title": "PendingDesign",
                    "required": ["page_id"],
                    "properties": {"page_id": {"type": "string"}},
                },
            }
        ),
    )
    spec = Workflow(
        name="promote",
        version=1,
        description="list, then hand the list back",
        inline=(listing,),
        nodes=(RunNode(id="pending", work="pending_designs", inputs={"collection": FromParameter(parameter="collection")}),),
        parameters={"collection": ParameterSpec(description="the knowledge base", json_schema={"type": "string"})},
        output=FromNode(node="pending"),
    )
    async def perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
        del task, inputs
        return output_type

    prepared = prepare(spec, StepRegistry(perform=perform))
    assert prepared.report.ok, prepared.report.issues
    nodes = {str(node["id"]): node for node in graph_payload(spec, prepared.registry, {})["nodes"]}
    assert nodes["pending"]["signature"] == "pending_designs(collection: string) → list[PendingDesign]"


def test_a_gate_shows_what_passes_through_it(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A gate has no signature of its own; it forwards its source's type, so say which."""
    assert _nodes(quarterly_report, registry)["approve"]["signature"] == "a human approves · ReportDraft passes through"


def test_the_workflow_result_is_marked(quarterly_report: Workflow, registry: StepRegistry) -> None:
    nodes = _nodes(quarterly_report, registry)
    assert nodes["publish"]["is_output"] is True
    assert nodes["draft_report"]["is_output"] is False


def test_a_node_carries_its_own_step_name_distinct_from_its_id(call_log: CallLog) -> None:
    """A node still carries the step name it runs, distinct from its own id, for display."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="renamed-node",
        version=1,
        description="a node id distinct from the step it runs",
        parameters={"quarter": ParameterSpec(description="fiscal quarter", json_schema={"type": "string"})},
        nodes=(RunNode(id="grab_the_numbers", work="fetch_sales", inputs={"quarter": FromParameter(parameter="quarter")}),),
        output=FromNode(node="grab_the_numbers"),
    )
    node = _nodes(spec, registry)["grab_the_numbers"]
    assert node["id"] == "grab_the_numbers"
    assert node["work"] == "fetch_sales"


def test_a_human_gate_carries_no_work_a_person_decides_not_a_step(quarterly_report: Workflow, registry: StepRegistry) -> None:
    assert _nodes(quarterly_report, registry)["approve"]["work"] == ""


def test_every_wire_is_labelled_with_the_type_flowing_on_it(quarterly_report: Workflow, registry: StepRegistry) -> None:
    payload = graph_payload(quarterly_report, registry, TOOLS)
    labels = {(str(edge["from"]), str(edge["to"])): str(edge["label"]) for edge in payload["edges"]}
    assert labels[("approve", "publish")] == "report: ReportDraft"
    assert labels[("param:quarter", "fetch_sales")] == "quarter: string"
    assert labels[("draft_report", "approve")] == "reviews: ReportDraft"
    assert labels[("approve", "draft_report")] == "rejected"


def test_a_step_that_searches_for_itself_names_its_tools() -> None:
    """The label used to read "holds tools", which tells a reader nothing at all.

    Naming them says what the step can go and find out on its own -- the difference between
    a step handed a blob of context and a step that can ask a second question.
    """
    registry = StepRegistry()

    async def investigate(question: str) -> SalesData:
        """Investigate a question against the knowledge base."""
        return SalesData(quarter=question, total=0)

    registry.register(investigate)
    spec = Workflow(
        name="one-step",
        version=1,
        description="investigate and stop",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        nodes=(RunNode(id="investigate", work="investigate", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="investigate"),
    )
    node = _nodes(spec, registry)["investigate"]
    assert node["tools"] == ["search_text", "walk_graph", "claim_records"]
    assert node["is_output"] is True


def test_a_step_this_run_lacks_is_flagged_not_faked(call_log: CallLog) -> None:
    """The UI paints these amber; inventing a signature for missing work would be a lie."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log, exclude=frozenset({"publish"}))
    spec = Workflow(
        name="missing-step",
        version=1,
        description="names work this run does not have",
        nodes=(RunNode(id="publish", work="publish"),),
        output=FromNode(node="publish"),
    )
    node = _nodes(spec, registry)["publish"]
    assert node["unknown"] is True
    # Not `publish(?) → ?`: a box full of question marks reads as broken software rather than
    # as "this app does not have that step", which is what it actually means.
    assert node["signature"] == "publish, not something this app can do"
    assert "no step called 'publish'" in str(node["detail"])
    assert node["output_schema"] is None
    assert node["input_schemas"] == {}


def test_every_step_carries_the_schemas_behind_its_signature(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A signature names a result; the schema says what fields it has, which is what a reader opens the step for."""
    nodes = _nodes(quarterly_report, registry)
    assert nodes["fetch_sales"]["input_schemas"] == {"quarter": {"type": "string"}}
    output = nodes["fetch_sales"]["output_schema"]
    assert isinstance(output, dict)
    assert output["title"] == "SalesData"
    assert set(output["properties"]) == {"quarter", "total"}
    assert nodes["fetch_sales"]["source"] == ""


def test_a_run_that_hands_a_parameter_straight_back_marks_that_parameter_as_its_result() -> None:
    """The result is a node on screen; when the spec's output is a parameter, that parameter is the one."""
    spec = Workflow(
        name="echo",
        version=1,
        description="hand the question straight back",
        parameters={"question": ParameterSpec(description="what was asked", json_schema={"type": "string"})},
        nodes=(),
        output=FromParameter(parameter="question"),
    )
    assert _nodes(spec, StepRegistry())["question"]["is_output"] is True


def test_a_parameter_carries_its_own_schema(quarterly_report: Workflow, registry: StepRegistry) -> None:
    nodes = _nodes(quarterly_report, registry)
    assert nodes["quarter"]["output_schema"] == {"type": "string"}
    assert nodes["quarter"]["input_schemas"] == {}


def test_a_parameter_says_whether_a_launch_must_supply_it() -> None:
    """Whether a caller may omit a parameter is a fact of its own, not something read back out of prose."""
    spec = Workflow(
        name="decide",
        version=1,
        description="answer one question",
        parameters={
            "question": ParameterSpec(description="what to decide", json_schema={"type": "string"}),
            "constraints": ParameterSpec(description="what must hold", json_schema={"type": "string"}, required=False),
        },
        nodes=(RunNode(id="investigate", work="investigate", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="investigate"),
    )
    nodes = _nodes(spec, StepRegistry())
    assert nodes["question"]["required"] is True
    assert nodes["constraints"]["required"] is False


def test_a_gate_carries_the_schema_of_what_passes_through_it(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """A gate produces nothing of its own, so its schema is the reviewed step's, resolved the same way its signature is."""
    output = _nodes(quarterly_report, registry)["approve"]["output_schema"]
    assert isinstance(output, dict)
    assert output["title"] == "ReportDraft"


def test_a_map_reports_the_collected_list_as_its_output(call_log: CallLog) -> None:
    """What flows out of a map is the list, and the schema has to say so as plainly as the wire label does."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter",
        version=1,
        description="fetch sales for every quarter named",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="sales", turns=("fetch_sales",), over=FromParameter(parameter="quarters"), each="quarter"),),
        output=FromNode(node="sales"),
    )
    node = _nodes(spec, registry)["sales"]
    output = node["output_schema"]
    assert isinstance(output, dict)
    assert output["type"] == "array"
    assert isinstance(output["items"], dict)
    assert output["items"]["title"] == "SalesData"
    assert node["input_schemas"] == {"quarter": {"type": "string"}}


def test_a_map_hoists_its_item_schema_defs_so_every_ref_still_resolves(call_log: CallLog) -> None:
    """Wrapping a schema under `items` would strand its `$defs`: a `$ref` resolves from the root alone."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)

    async def fetch_history(quarter: str) -> list[SalesData]:
        """Every sales record on file for the quarter."""
        return [SalesData(quarter=quarter, total=0)]

    registry.register(fetch_history)
    spec = Workflow(
        name="every-history",
        version=1,
        description="fetch the history of every quarter named",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="histories", turns=("fetch_history",), over=FromParameter(parameter="quarters"), each="quarter"),),
        output=FromNode(node="histories"),
    )
    output = _nodes(spec, registry)["histories"]["output_schema"]
    assert isinstance(output, dict)
    assert output["type"] == "array"
    assert "SalesData" in output["$defs"]
    assert isinstance(output["items"], dict)
    assert "$defs" not in output["items"]
    assert output["items"]["items"] == {"$ref": "#/$defs/SalesData"}


def test_a_single_turn_maps_turns_entry_keeps_node_id_equal_to_the_map(call_log: CallLog) -> None:
    """One turn means the same node the client already drew -- proven by pinning the whole entry."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter",
        version=1,
        description="fetch sales for every quarter named",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="sales", turns=("fetch_sales",), over=FromParameter(parameter="quarters"), each="quarter"),),
        output=FromNode(node="sales"),
    )
    node = _nodes(spec, registry)["sales"]
    assert node["signature"] == "fetch_sales once per item · list[string] → list[SalesData]"
    assert node["detail"] == (
        "Runs 'fetch_sales' once for every item of parameter='quarters', feeding each one as 'quarter', and "
        "collects the results as they finish, not in item order. An empty collection is fine: nothing runs and "
        "the list is empty."
    )
    assert node["unknown"] is False
    assert node["tools"] == []
    turns = node["turns"]
    assert isinstance(turns, list)
    assert len(turns) == 1
    turn = turns[0]
    assert isinstance(turn, dict)
    assert turn["work"] == "fetch_sales"
    assert turn["node_id"] == "sales"


def test_a_map_with_on_error_skip_says_so_in_its_detail(call_log: CallLog) -> None:
    """`on_error` reads into the existing sentence -- no new payload key, nothing for the SPA's schema to learn."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter",
        version=1,
        description="fetch sales for every quarter named, skipping one that fails",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="sales", turns=("fetch_sales",), over=FromParameter(parameter="quarters"), each="quarter", on_error="skip"),),
        output=FromNode(node="sales"),
    )
    node = _nodes(spec, registry)["sales"]
    assert node["detail"] == (
        "Runs 'fetch_sales' once for every item of parameter='quarters', feeding each one as 'quarter', and "
        "collects the results as they finish, not in item order, and an item that fails is skipped while the "
        "rest carry on. An empty collection is fine: nothing runs and the list is empty."
    )
    assert set(node) == {
        "key",
        "id",
        "kind",
        "signature",
        "detail",
        "tools",
        "unknown",
        "brought",
        "briefing",
        "work",
        "input_schemas",
        "output_schema",
        "source",
        "turns",
        "is_output",
    }, "on_error changes the sentence, never the payload's own keys"


def test_a_multi_turn_map_collects_the_last_turns_output(call_log: CallLog) -> None:
    """A multi-turn map produces `list[<last turn's output>]`, the same resolution the indexer uses."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter-drafted",
        version=1,
        description="fetch sales for every quarter, then draft a report from each",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="drafts", turns=("fetch_sales", "draft_report"), over=FromParameter(parameter="quarters"), each="quarter"),),
        output=FromNode(node="drafts"),
    )
    node = _nodes(spec, registry)["drafts"]
    output = node["output_schema"]
    assert isinstance(output, dict)
    assert output["type"] == "array"
    assert isinstance(output["items"], dict)
    # `list[ReportDraft]`, not `list[SalesData]`: the last turn's output replaces the first turn's.
    assert output["items"]["title"] == "ReportDraft"


def test_a_multi_turn_maps_turns_list_carries_each_turns_own_shape(call_log: CallLog) -> None:
    """The client draws "each item then runs X" off the `turns` list, not off the top-level fields."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter-drafted",
        version=1,
        description="fetch sales for every quarter, then draft a report from each",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="drafts", turns=("fetch_sales", "draft_report"), over=FromParameter(parameter="quarters"), each="quarter"),),
        output=FromNode(node="drafts"),
    )
    node = _nodes(spec, registry)["drafts"]
    turns = node["turns"]
    assert isinstance(turns, list)
    first, second = turns
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["work"] == "fetch_sales"
    assert second["work"] == "draft_report"
    # Every turn of a multi-turn map is observed under its own dotted id, turn 1 included -- the
    # indexer records (and reports live events for) it that way once there is more than one
    # turn to tell apart, so the map's own plain id is reserved for the single-turn case alone.
    assert first["node_id"] == "drafts.fetch_sales"
    assert second["node_id"] == "drafts.draft_report"
    assert second["briefing"] == ""
    assert second["tools"] == []
    assert second["source"] == ""
    second_output = second["output_schema"]
    assert isinstance(second_output, dict)
    assert second_output["title"] == "ReportDraft"
    # The top-level box still names the first turn -- the one the fan-out itself runs.
    assert node["work"] == "fetch_sales"
    assert node["signature"] == "fetch_sales then draft_report once per item · list[string] → list[ReportDraft]"
    assert "then runs 'draft_report'" in str(node["detail"])


def test_a_multi_turn_map_with_an_unknown_later_turn_is_flagged_not_faked(call_log: CallLog) -> None:
    """A missing later turn is as honest a gap as a missing first one, and its neighbour is unaffected."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter-unchained",
        version=1,
        description="fetch sales for every quarter, then chain into a step this app never registered",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="drafts", turns=("fetch_sales", "summarise"), over=FromParameter(parameter="quarters"), each="quarter"),),
        output=FromNode(node="drafts"),
    )
    node = _nodes(spec, registry)["drafts"]
    assert node["unknown"] is True
    assert node["output_schema"] is None
    turns = node["turns"]
    assert isinstance(turns, list)
    first, second = turns
    assert isinstance(first, dict)
    # The known first turn's own shape is untouched by its neighbour's absence, and it is still
    # observed under its own dotted id -- this map has two turns, so turn 1 is not exempt either.
    assert first["node_id"] == "drafts.fetch_sales"
    first_output = first["output_schema"]
    assert isinstance(first_output, dict)
    assert first_output["title"] == "SalesData"
    assert second == {"work": "summarise", "node_id": "drafts.summarise", "briefing": "", "tools": [], "output_schema": None, "source": ""}


def test_a_wire_from_a_multi_turn_map_carries_the_last_turns_type(call_log: CallLog) -> None:
    """A downstream node reading a multi-turn map's output sees the LAST turn's type on its wire label."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter-drafted-then-published",
        version=1,
        description="fetch sales for every quarter, draft a report from each, publish every draft",
        parameters={"quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(
            MapNode(id="drafts", turns=("fetch_sales", "draft_report"), over=FromParameter(parameter="quarters"), each="quarter"),
            RunNode(id="publish", work="publish", inputs={"report": FromNode(node="drafts")}),
        ),
        output=FromNode(node="publish"),
    )
    payload = graph_payload(spec, registry, TOOLS)
    labels = {(str(edge["from"]), str(edge["to"])): str(edge["label"]) for edge in payload["edges"]}
    assert labels[("drafts", "publish")] == "report: list[ReportDraft]"


def test_a_shared_input_wire_feeding_a_later_turn_draws_an_edge(call_log: CallLog) -> None:
    """`inputs` are resolved once and shared by every turn, so a producer feeding a LATER turn must be drawn."""
    registry = StepRegistry()
    register_quarterly_steps(registry, call_log)
    spec = Workflow(
        name="every-quarter-reconciled",
        version=1,
        description="fetch sales for every quarter, then reconcile each against one shared quarter's costs",
        parameters={
            "quarters": ParameterSpec(description="fiscal quarters", json_schema={"type": "array", "items": {"type": "string"}}),
            "costs_quarter": ParameterSpec(description="the quarter costs are reconciled against", json_schema={"type": "string"}),
        },
        nodes=(
            RunNode(id="fetch_costs", work="fetch_costs", inputs={"quarter": FromParameter(parameter="costs_quarter")}),
            MapNode(
                id="ledgers",
                turns=("fetch_sales", "reconcile"),
                over=FromParameter(parameter="quarters"),
                each="quarter",
                inputs={"costs": FromNode(node="fetch_costs")},
            ),
        ),
        output=FromNode(node="ledgers"),
    )
    payload = graph_payload(spec, registry, TOOLS)
    labels = {(str(edge["from"]), str(edge["to"])): str(edge["label"]) for edge in payload["edges"]}
    assert labels[("fetch_costs", "ledgers")] == "costs: CostData"


async def _stub_perform(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Never called: `graph_payload` only reads a briefing's declared shape, not its behaviour."""
    raise NotImplementedError


def _briefing(name: str, inputs: dict[str, Slot], output: Shape, *, tools: tuple[str, ...] = ()) -> Briefing:
    instructions = f"Perform the turn named {name!r}, start to finish, exactly as briefed here, for this test only."
    return Briefing(name=name, instructions=instructions, inputs=inputs, output=output, tools=tools)


_UNDIGESTED_SCHEMA = {"type": "object", "title": "Undigested", "properties": {"source": {"type": "string"}}, "required": ["source"]}
_DIGEST_SCHEMA = {"type": "object", "title": "Digest", "properties": {"text": {"type": "string"}}, "required": ["text"]}
_RELATE_RESULT_SCHEMA = {"type": "object", "title": "RelateResult", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
_REFERENCE_SCHEMA = {"type": "object", "title": "Reference", "properties": {"url": {"type": "string"}}, "required": ["url"]}


def _digest_relate_extract_spec() -> Workflow:
    """A three-turn map -- digest each item, relate it to the base, extract what it references -- plus a consumer.

    A multi-turn map's grammar runs briefings only, so every turn is a `Briefing`.
    """
    digest_source = _briefing("digest_source", {"source": Shape(json_schema=_UNDIGESTED_SCHEMA)}, Shape(json_schema=_DIGEST_SCHEMA))
    # `SameAs`, not a second `Shape` repeating the schema: types are nominal, so a fresh `Shape`
    # here would build a second, unequal class even with an identical schema.
    relate = _briefing("relate", {"digest": SameAs(like="digest_source")}, Shape(json_schema=_RELATE_RESULT_SCHEMA), tools=("search_text",))
    extract_references = _briefing("extract_references", {"relation": SameAs(like="relate")}, Shape(json_schema=_REFERENCE_SCHEMA))
    return Workflow(
        name="digest-relate-extract",
        version=1,
        description="Digest each item, relate it to the base, then extract what it references.",
        parameters={"items": ParameterSpec(description="undigested items", json_schema={"type": "array", "items": _UNDIGESTED_SCHEMA})},
        inline=(digest_source, relate, extract_references),
        nodes=(
            MapNode(
                id="sources", turns=("digest_source", "relate", "extract_references"), over=FromParameter(parameter="items"), each="source"
            ),
        ),
        output=FromNode(node="sources"),
    )


def test_a_three_turn_maps_signature_and_turns_list() -> None:
    """A multi-turn map's box names the whole chain; its `turns` list carries each turn's own shape."""
    spec = _digest_relate_extract_spec()
    prepared = prepare(spec, StepRegistry(perform=_stub_perform, grants=("search_text",)))
    assert prepared.report.ok, prepared.report.issues
    node = _nodes(spec, prepared.registry)["sources"]

    assert node["signature"] == "digest_source then relate then extract_references once per item · list[Undigested] → list[Reference]"
    turns = node["turns"]
    assert isinstance(turns, list)
    digest_turn, relate_turn, extract_turn = turns
    assert isinstance(digest_turn, dict)
    assert isinstance(relate_turn, dict)
    assert isinstance(extract_turn, dict)
    assert [t["work"] for t in (digest_turn, relate_turn, extract_turn)] == ["digest_source", "relate", "extract_references"]
    # A multi-turn map observes every turn under its own dotted id, turn 1 included -- the
    # indexer records it that way once there is more than one turn to tell apart.
    assert [t["node_id"] for t in (digest_turn, relate_turn, extract_turn)] == [
        "sources.digest_source",
        "sources.relate",
        "sources.extract_references",
    ]

    digest_briefing = digest_turn["briefing"]
    assert isinstance(digest_briefing, str)
    assert digest_briefing.startswith("Perform the turn named 'digest_source'")
    assert digest_turn["tools"] == []
    digest_output = digest_turn["output_schema"]
    assert isinstance(digest_output, dict)
    assert digest_output["title"] == "Digest"
    assert relate_turn["tools"] == ["search_text"]
    relate_output = relate_turn["output_schema"]
    assert isinstance(relate_output, dict)
    assert relate_output["title"] == "RelateResult"
    extract_output = extract_turn["output_schema"]
    assert isinstance(extract_output, dict)
    assert extract_output["title"] == "Reference"
    for turn in (digest_turn, relate_turn, extract_turn):
        assert turn["source"] == ""


def test_a_multi_turn_maps_own_tools_is_the_union_across_turns() -> None:
    """At runtime every turn of a multi-turn map holds the union of all turns' tools -- one conversation, one tool surface.

    The map's own `tools` says so, in first-appearance order and without repeats, while each
    turn under `turns[]` keeps only what it declared.
    """
    digest_source = _briefing(
        "digest_source", {"source": Shape(json_schema=_UNDIGESTED_SCHEMA)}, Shape(json_schema=_DIGEST_SCHEMA), tools=("search_text",)
    )
    relate = _briefing(
        "relate",
        {"digest": SameAs(like="digest_source")},
        Shape(json_schema=_RELATE_RESULT_SCHEMA),
        tools=("walk_graph", "search_text"),
    )
    extract_references = _briefing(
        "extract_references", {"relation": SameAs(like="relate")}, Shape(json_schema=_REFERENCE_SCHEMA), tools=("claim_records",)
    )
    spec = Workflow(
        name="digest-relate-extract-tools",
        version=1,
        description="Digest each item, relate it to the base, then extract what it references, tools overlapping.",
        parameters={"items": ParameterSpec(description="undigested items", json_schema={"type": "array", "items": _UNDIGESTED_SCHEMA})},
        inline=(digest_source, relate, extract_references),
        nodes=(
            MapNode(
                id="sources", turns=("digest_source", "relate", "extract_references"), over=FromParameter(parameter="items"), each="source"
            ),
        ),
        output=FromNode(node="sources"),
    )
    prepared = prepare(spec, StepRegistry(perform=_stub_perform, grants=("search_text", "walk_graph", "claim_records")))
    assert prepared.report.ok, prepared.report.issues
    node = _nodes(spec, prepared.registry)["sources"]

    assert node["tools"] == ["search_text", "walk_graph", "claim_records"]
    turns = node["turns"]
    assert isinstance(turns, list)
    digest_turn, relate_turn, extract_turn = turns
    assert isinstance(digest_turn, dict)
    assert isinstance(relate_turn, dict)
    assert isinstance(extract_turn, dict)
    assert digest_turn["tools"] == ["search_text"]
    assert relate_turn["tools"] == ["walk_graph", "search_text"]
    assert extract_turn["tools"] == ["claim_records"]


def test_a_turn_wire_resolves_to_that_turns_own_type() -> None:
    """A wire naming `turn` reads an earlier turn's own collected list, not the map's last one."""
    spec = _digest_relate_extract_spec()
    base = StepRegistry(perform=_stub_perform, grants=("search_text",))

    async def notify_relation(relation: str) -> str:
        return relation

    base.register(notify_relation)
    spec = spec.model_copy(
        update={
            "nodes": (
                *spec.nodes,
                RunNode(id="notify", work="notify_relation", inputs={"relation": FromNode(node="sources", turn="relate")}),
            )
        }
    )
    # Not asserted clean: `notify`'s own declared shape does not matter here, only that the map's
    # three turns build -- which `graph_payload` needs regardless of what else this spec wires.
    prepared = prepare(spec, base)
    payload = graph_payload(spec, prepared.registry, TOOLS)
    labels = {(str(edge["from"]), str(edge["to"])): str(edge["label"]) for edge in payload["edges"]}
    assert labels[("sources", "notify")] == "relation: list[RelateResult]"


def test_a_carried_program_shows_its_source_even_where_this_app_cannot_run_it() -> None:
    """The program text is what the step does; a reader gets it whether or not this run has a sandbox for it."""
    source = "{'net': sales['total'] - 1}"
    spec = Workflow(
        name="carried-program",
        version=1,
        description="one program the artifact brought",
        parameters={"sales": ParameterSpec(description="a sales record", json_schema={"type": "object"})},
        inline=(
            Program(
                name="net_of",
                source=source,
                inputs={"sales": Shape(json_schema={"type": "object"})},
                output=Shape(json_schema={"type": "object", "title": "Net"}),
            ),
        ),
        nodes=(RunNode(id="net", work="net_of", inputs={"sales": FromParameter(parameter="sales")}),),
        output=FromNode(node="net"),
    )
    node = _nodes(spec, StepRegistry())["net"]
    assert node["source"] == source
    assert node["unknown"] is True
