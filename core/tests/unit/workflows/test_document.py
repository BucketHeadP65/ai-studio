"""The document loader: find a spec in frontmatter, keep everything else, own no schema."""

from __future__ import annotations

import pytest

from core.ai.workflows.contracts import (
    DocumentError,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    StepRegistry,
    Workflow,
    WorkflowIntent,
    build_graph,
    load_spec_document,
    run_workflow,
)
from core.ai.workflows.domain.document import dump_spec_document
from tests.unit.workflows.conftest import ApproveAll, CallLog, ReportUrl


def test_round_trip(quarterly_report: Workflow) -> None:
    text = dump_spec_document(quarterly_report, body="## Notes\n\nFetch sales net of returns.\n")
    loaded = load_spec_document(text)
    assert loaded.workflow == quarterly_report
    assert loaded.body.startswith("## Notes")


def test_round_trip_preserves_a_maps_on_error_policy() -> None:
    spec = Workflow(
        name="skip-a-bad-item",
        version=1,
        description="One map, on_error skip, round-tripped through the YAML frontmatter.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array", "items": {"type": "string"}})},
        nodes=(MapNode(id="each", turns=("step",), over=FromParameter(parameter="items"), each="item", on_error="skip"),),
        output=FromNode(node="each"),
    )
    loaded = load_spec_document(dump_spec_document(spec))
    assert loaded.workflow == spec
    node = loaded.workflow.nodes[0]
    assert isinstance(node, MapNode)
    assert node.on_error == "skip"


def test_foreign_frontmatter_is_preserved_not_interpreted(quarterly_report: Workflow) -> None:
    """The core owns no metadata schema.

    A host vocabulary's fields come back untouched and unvalidated, because their meaning
    belongs to whoever authored them.
    """
    foreign = {
        "id": "wf.quarterly-report",
        "status": "synthesized",
        "use_when": "a recurring finance report",
        "generated": {"by": "human:alex", "at": "2026-07-30"},
        "whatever_else": [1, 2, 3],
    }
    text = dump_spec_document(quarterly_report, frontmatter=foreign, body="body")
    loaded = load_spec_document(text)
    assert loaded.frontmatter == foreign
    assert loaded.workflow == quarterly_report


def test_an_intent_rides_in_the_frontmatter_and_comes_back_as_it_went_in(quarterly_report: Workflow) -> None:
    """What was asked for is the host's key, kept beside the spec and never read by the loader.

    The document is where the folder library keeps a card's intent, so the round trip a saved
    workflow takes through a file is this one.
    """
    intent = WorkflowIntent(
        request="Close the books every quarter",
        expected_result="The published report, and the numbers behind it.",
        trigger="scheduled",
        assumptions=("A quarter ends on the last day of March, June, September and December.",),
    )
    text = dump_spec_document(quarterly_report, frontmatter={"intent": intent.model_dump(mode="json")}, body="body")

    loaded = load_spec_document(text)

    assert WorkflowIntent.model_validate(loaded.frontmatter["intent"]) == intent
    assert loaded.workflow == quarterly_report


def test_a_document_written_without_an_intent_carries_no_intent_key(quarterly_report: Workflow) -> None:
    """Absent stays absent: a file saved without an intent carries no intent key."""
    text = dump_spec_document(quarterly_report, frontmatter={"title": "Quarterly report"}, body="body")

    loaded = load_spec_document(text)

    assert "intent" not in text
    assert "intent" not in loaded.frontmatter


def test_spec_key_is_configurable(quarterly_report: Workflow) -> None:
    """A host format may want the spec namespaced.

    The loader takes the key as an argument rather than baking one format's convention into
    the package.
    """
    text = dump_spec_document(quarterly_report, key="okf_x_workflow")
    assert load_spec_document(text, key="okf_x_workflow").workflow == quarterly_report
    with pytest.raises(DocumentError, match="no spec under frontmatter key 'workflow'"):
        load_spec_document(text)


def test_missing_key_names_what_was_present(quarterly_report: Workflow) -> None:
    text = dump_spec_document(quarterly_report, frontmatter={"title": "t"}, key="elsewhere")
    with pytest.raises(DocumentError, match="Keys present: elsewhere, title"):
        load_spec_document(text)


async def test_document_to_gated_run(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    """The whole claim in one test.

    A markdown file becomes a validated, compiled, gated, running workflow -- and nothing on
    the way is skippable.
    """
    loaded = load_spec_document(dump_spec_document(quarterly_report))
    result = await run_workflow(loaded.workflow, registry, params={"quarter": "Q2"}, approval=ApproveAll())
    assert isinstance(result, ReportUrl)
    assert call_log.calls[-1] == "publish"


def test_loading_grants_nothing(quarterly_report: Workflow) -> None:
    """A file is not a permit: a spec referencing unregistered work still does not compile."""
    loaded = load_spec_document(dump_spec_document(quarterly_report))
    with pytest.raises(Exception, match="not registered"):
        build_graph(loaded.workflow, StepRegistry(), approval=ApproveAll())


def test_yaml_boolean_key_trap_is_named_not_mysterious() -> None:
    """The bug that bit the first real artifact.

    YAML 1.1 turns a bare `on:` key into `True`, so the field vanishes and pydantic complains
    about the wrong thing.
    """
    text = (
        "---\n"
        "workflow:\n"
        "  name: x\n  version: 1\n  description: d\n"
        "  nodes:\n"
        "  - kind: gate\n    id: approve\n    on:\n      node: draft\n    rejected_to: draft\n"
        "  output:\n    node: approve\n"
        "---\n"
    )
    with pytest.raises(DocumentError, match="non-string key"):
        load_spec_document(text)


def test_no_frontmatter_refused() -> None:
    with pytest.raises(DocumentError, match="no frontmatter"):
        load_spec_document("# just markdown\n")


def test_unclosed_frontmatter_refused() -> None:
    with pytest.raises(DocumentError, match="not closed"):
        load_spec_document("---\nworkflow: {}\n")


def test_non_mapping_frontmatter_refused() -> None:
    with pytest.raises(DocumentError, match="must be a mapping"):
        load_spec_document("---\n- just\n- a\n- list\n---\n")
