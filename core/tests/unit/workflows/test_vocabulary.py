"""The grammar's own promises, including the one it used to break.

A spec is authored in two ways -- YAML by a person, JSON arguments by a model reading a tool
schema -- and both read the same JSON Schema to learn the shape. So the schema is part of the
contract, not documentation of it, and a schema that disagrees with the validator is a bug
even when every Python caller is happy.
"""

from __future__ import annotations

import pytest

from pydantic import TypeAdapter, ValidationError

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    JudgeGate,
    MapNode,
    OrchestratorGate,
    PersonGate,
    ResultArtifactSpec,
    RunNode,
    Shape,
    Wire,
    Workflow,
    WorkflowResultSpec,
    referenced_steps,
)
from core.ai.workflows.domain.vocabulary import Slot


def test_the_schema_requires_the_discriminator_the_validator_demands() -> None:
    """`kind` has a Python default, which used to make it optional in the schema.

    A model authoring a node from that schema legitimately omitted `kind` and got
    `union_tag_not_found` -- an error the schema said was impossible.
    """
    for node in (RunNode, PersonGate):
        assert "kind" in node.model_json_schema()["required"], node.__name__


def test_omitting_the_discriminator_still_fails_loudly() -> None:
    """The other half of the pair: the schema is honest because this really is strict."""
    with pytest.raises(ValidationError, match="discriminator"):
        Workflow.model_validate(
            {"name": "x-y", "version": 1, "description": "d", "nodes": [{"id": "step", "work": "step"}], "output": {"node": "step"}}
        )


def test_a_key_the_grammar_does_not_know_is_refused() -> None:
    """Silently dropping an unknown key is how an author's typo becomes a mystery later."""
    with pytest.raises(ValidationError, match="applicability"):
        Workflow.model_validate(
            {
                "name": "x-y",
                "version": 1,
                "description": "d",
                "applicability": "this belongs to the library's card, not to the graph",
                "nodes": [{"kind": "run", "id": "step", "work": "step"}],
                "output": {"node": "step"},
            }
        )


def test_python_callers_keep_the_convenient_default() -> None:
    """Requiring it in the schema must not make Python authoring noisier."""
    assert RunNode(id="step", work="step").kind == "run"
    assert PersonGate(id="approve", reviews=FromNode(node="step"), rejected_to="step").kind == "gate"


# --- a mistake in a tagless union used to be reported four times ----------------------------------


def test_a_wire_with_a_stray_key_is_told_about_that_key_and_nothing_else() -> None:
    """`Wire` and `Slot` had no discriminator, so every branch reported and one typo became four errors.

    Three of the four described a branch the author was plainly not writing: `{node: draft, field: x}`
    is a `FromNode` with one key too many, and pydantic also said `FromParameter` was missing
    `parameter` and had two extras. The useful sentence -- there is no `field` on a wire -- arrived
    fourth, and `check_draft` truncates at 900 characters, so in the live authoring loop it arrived
    cut in half or not at all.

    `Node` has been discriminated on `kind` since the schema ruling. These two have no natural tag,
    so the tag is derived from which key is present -- the same thing a reader does.
    """
    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Wire).validate_python({"node": "draft", "field": "x"})
    assert refused.value.error_count() == 1
    (error,) = refused.value.errors()
    assert error["loc"] == ("from_node", "field"), "reported against the branch it was plainly writing"


def test_a_slot_with_a_stray_key_is_told_about_that_key_and_nothing_else() -> None:
    """The case that actually happened: JSON-Schema `definitions` beside a `json_schema`.

    `build_type` refuses `$ref` and friends BY NAME so a model can recover, but that politeness
    only applies once the YAML already parses as a spec. This one failed a step earlier, and the
    four-error dump is what the agent had to work from.
    """
    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Slot).validate_python({"json_schema": {"type": "object"}, "definitions": {"Item": {}}})
    assert refused.value.error_count() == 1
    (error,) = refused.value.errors()
    assert error["loc"][-1] == "definitions"


def test_a_slot_that_is_neither_shape_nor_reference_says_which_two_it_could_be() -> None:
    """A tagless union whose tag cannot be derived must name the choice, not fall back to noise."""
    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Slot).validate_python({"typ": "string"})
    (error,) = refused.value.errors()
    assert error["type"] == "slot_not_recognised"
    assert "json_schema" in error["msg"], "the message says what to write instead"


def test_a_slot_merging_both_branches_gets_the_one_line_law_not_four_field_errors() -> None:
    """The shape a live agent actually sent: `json_schema` and `like` side by side in one slot.

    Tagging it as a shape because `json_schema` came first reported four stray-field errors and
    never the real problem -- two answers to one question. Both keys present derives no tag, so
    the same one-line law as an unrecognisable slot answers.
    """
    with pytest.raises(ValidationError) as refused:
        TypeAdapter(Slot).validate_python({"json_schema": None, "like": "recent_digests", "element": True, "many": False})
    (error,) = refused.value.errors()
    assert error["type"] == "slot_not_recognised"
    assert "json_schema" in error["msg"]
    assert "like" in error["msg"]
    assert "like" in error["msg"], "the message says what to write instead"


# --- a map node speaks turns ----------------------------------------------------------------


def test_a_map_node_speaks_turns() -> None:
    node = MapNode(id="sources", turns=("digest", "relate"), over=FromNode(node="undigested"), each="source")
    assert node.turns == ("digest", "relate")
    assert node.abort_after_repeats == 3


@pytest.mark.parametrize("field", ["work", "then", "then_each", "then_inputs"])
def test_the_old_map_fields_are_refused(field: str) -> None:
    payload = {"kind": "map", "id": "m", "turns": ["digest"], "over": {"node": "u"}, "each": "source", field: "digest"}
    with pytest.raises(ValidationError):
        MapNode.model_validate(payload)


def test_an_empty_turn_list_is_refused() -> None:
    with pytest.raises(ValidationError):
        MapNode(id="m", turns=(), over=FromNode(node="u"), each="source")


def test_a_wire_may_name_a_turn() -> None:
    wire = FromNode.model_validate({"node": "sources", "turn": "relate"})
    assert wire.turn == "relate"


def test_referenced_steps_walks_run_map_and_judge_work_in_first_appearance_order() -> None:
    spec = Workflow(
        name="reviewed-drafts",
        version=1,
        description="Collect drafts, repair each one, then review the completed collection.",
        nodes=(
            RunNode(id="collect", work="collect"),
            MapNode(id="drafts", turns=("draft", "repair"), over=FromNode(node="collect"), each="item"),
            JudgeGate(id="review", work="review", reviews=FromNode(node="drafts"), rejected_to="drafts"),
            PersonGate(id="approve", reviews=FromNode(node="review"), rejected_to="review"),
        ),
        output=FromNode(node="approve"),
    )

    assert referenced_steps(spec) == ("collect", "draft", "repair", "review")


def test_referenced_steps_keeps_a_repeated_work_at_its_first_appearance() -> None:
    spec = Workflow(
        name="reused-work",
        version=1,
        description="A graph may reuse one registered work at more than one node.",
        nodes=(
            RunNode(id="collect", work="draft"),
            MapNode(id="drafts", turns=("draft", "repair"), over=FromNode(node="collect"), each="item"),
            JudgeGate(id="review", work="review", reviews=FromNode(node="drafts"), rejected_to="drafts"),
        ),
        output=FromNode(node="review"),
    )

    assert referenced_steps(spec) == ("draft", "repair", "review")


def test_a_result_omits_absent_optional_fields_from_its_serialised_shape() -> None:
    artifact = ResultArtifactSpec(id="brief", label="Brief", source=FromNode(node="render"), media_type="text/markdown")
    result = WorkflowResultSpec(summary="A reviewed draft package.", primary_work="drafts", artifacts=(artifact,))
    spec = Workflow(
        name="result-shape",
        version=1,
        description="A workflow that declares the values a caller can select after it runs.",
        nodes=(RunNode(id="render", work="render"),),
        output=FromNode(node="render"),
    )

    assert "field" not in artifact.model_dump(mode="json")
    assert "result" not in spec.model_dump(mode="json")
    assert spec.model_copy(update={"result": result}).model_dump(mode="json")["result"] == result.model_dump(mode="json")


# --- a node or a step may name its tier ------------------------------------------------------


def test_a_run_node_parses_with_a_named_tier() -> None:
    assert RunNode(id="step", work="step", tier="helper").tier == "helper"


def test_a_run_node_defaults_to_no_tier() -> None:
    assert RunNode(id="step", work="step").tier is None


def test_a_run_node_refuses_an_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        RunNode.model_validate({"kind": "run", "id": "step", "work": "step", "tier": "boss"})


def test_a_map_node_parses_with_a_named_tier() -> None:
    node = MapNode(id="sources", turns=("digest",), over=FromNode(node="undigested"), each="source", tier="helper")
    assert node.tier == "helper"


def test_a_map_node_refuses_an_unknown_tier() -> None:
    with pytest.raises(ValidationError):
        MapNode.model_validate(
            {"kind": "map", "id": "sources", "turns": ["digest"], "over": {"node": "u"}, "each": "source", "tier": "boss"}
        )


def test_a_task_spec_parses_with_a_named_tier() -> None:
    task = Briefing(
        name="dig",
        instructions="Find what the corpus says about the question and report it in a single note, nothing more.",
        output=Shape(json_schema={"type": "string"}),
        tier="orchestrator",
    )
    assert task.tier == "orchestrator"


def test_a_task_spec_defaults_to_no_tier() -> None:
    task = Briefing(
        name="dig",
        instructions="Find what the corpus says about the question and report it in a single note, nothing more.",
        output=Shape(json_schema={"type": "string"}),
    )
    assert task.tier is None


@pytest.mark.parametrize("kind", ["gate", "judge", "orchestrator-gate"])
def test_every_gate_kind_carries_the_same_ceiling_of_three_total_reaches(kind: str) -> None:
    """Three reaches including the first review, absent from the dump, and never zero.

    Absent from the dump is the load-bearing half: a workflow saved before gates had a ceiling
    and one saved now are the same document, so evidence filed under either still matches.
    """
    gate_type: type[PersonGate | JudgeGate | OrchestratorGate] = {
        "gate": PersonGate, "judge": JudgeGate, "orchestrator-gate": OrchestratorGate
    }[kind]
    document: dict[str, object] = {"id": "review", "reviews": {"node": "draft"}, "rejected_to": "draft"}
    if kind == "judge":
        document["work"] = "judge"
    if kind == "orchestrator-gate":
        document["charge"] = "Review the draft against every constraint the original request named."

    gate = gate_type.model_validate(document)

    assert gate.max_attempts == 3
    assert "max_attempts" not in gate.model_dump()
    assert gate_type.model_validate({**document, "max_attempts": 1}).model_dump()["max_attempts"] == 1
    with pytest.raises(ValidationError):
        gate_type.model_validate({**document, "max_attempts": 0})
