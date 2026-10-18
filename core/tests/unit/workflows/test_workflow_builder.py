"""`WorkflowBuilder`: a fluent pen over the five node verbs, refusing broken wiring cold.

The builder is not a second way to run a workflow -- it is a second way to *write* one, and
the claim under test is that writing it in code produces exactly the spec writing it in YAML
would: same fields, same equality, same downstream validation. The refusals are the other
half: a duplicate id, a wire to a node that does not exist yet, an output wire to nowhere,
and a judge whose step cannot possibly produce a verdict are all caught at the call that
introduced them, not fifteen fields later at `build()`.
"""

from __future__ import annotations

import re

import pytest
import yaml

from pydantic import BaseModel

from core.ai.workflows.authoring import WorkflowBuilder
from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    MapNode,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    validate,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.exceptions import WiringRefusedError


class RunEvidence(BaseModel):
    claim: str
    found: str


class Verdict(BaseModel):
    approved: bool
    why: str


class Score(BaseModel):
    value: int


async def split_claims(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


async def investigate(question: str) -> RunEvidence:
    return RunEvidence(claim=question, found=f"found: {question}")


async def summarise(findings: list[RunEvidence]) -> str:
    return " | ".join(item.found for item in findings)


async def judge_brief(text: str) -> bool:
    return "good" in text


async def judge_evidence(evidence: RunEvidence) -> Verdict:
    return Verdict(approved="good" in evidence.claim, why="contains good")


async def score_it(evidence: RunEvidence) -> Score:
    return Score(value=1)


async def _perform_stub(task: Briefing, output_type: type, /, **inputs: object) -> object:
    """Never called: this file only validates a briefed spec, it never runs one."""
    raise NotImplementedError


def _registry() -> StepRegistry:
    reg = StepRegistry()
    for step in (split_claims, investigate, summarise, judge_brief, judge_evidence, score_it):
        reg.register(step)
    return reg


RESEARCH_BRIEF_YAML = """
name: research-brief
version: 1
description: Investigate a topic's claims, human-check the evidence, judge the brief.
parameters:
  topic:
    description: the topic to investigate
    json_schema:
      type: string
nodes:
  - kind: run
    id: claims
    work: split_claims
    inputs:
      text: {parameter: topic}
  - kind: map
    id: investigated
    turns: [investigate]
    over: {node: claims}
    each: question
  - kind: gate
    id: approved_evidence
    reviews: {node: investigated}
    rejected_to: investigated
  - kind: run
    id: brief
    work: summarise
    inputs:
      findings: {node: approved_evidence}
  - kind: judge
    id: quality
    work: judge_brief
    reviews: {node: brief}
    rejected_to: claims
output: {node: quality}
"""


def _built_research_brief(registry: StepRegistry | None = None) -> Workflow:
    return (
        WorkflowBuilder("research-brief", 1, "Investigate a topic's claims, human-check the evidence, judge the brief.", registry=registry)
        .parameter("topic", ParameterSpec(description="the topic to investigate", json_schema={"type": "string"}))
        .run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")})
        .map("investigated", turns=["investigate"], over=FromNode(node="claims"), each="question")
        .gate("approved_evidence", reviews=FromNode(node="investigated"), rejected_to="investigated")
        .run("brief", work="summarise", inputs={"findings": FromNode(node="approved_evidence")})
        .judge("quality", work="judge_brief", reviews=FromNode(node="brief"), rejected_to="claims")
        .output(FromNode(node="quality"))
        .build()
    )


UNATTENDED_BRIEF_YAML = """
name: unattended-brief
version: 1
description: Investigate a topic's claims and let this run's own orchestrator clear the brief.
parameters:
  topic:
    description: the topic to investigate
    json_schema:
      type: string
nodes:
  - kind: run
    id: claims
    work: split_claims
    inputs:
      text: {parameter: topic}
  - kind: map
    id: investigated
    turns: [investigate]
    over: {node: claims}
    each: question
  - kind: orchestrator-gate
    id: vetted
    charge: Every claim must carry evidence naming its source; reject the set if any of them is bare.
    reviews: {node: investigated}
    rejected_to: investigated
  - kind: run
    id: brief
    work: summarise
    inputs:
      findings: {node: vetted}
output: {node: brief}
"""

CHARGE = "Every claim must carry evidence naming its source; reject the set if any of them is bare."


def test_the_builder_chain_emits_the_same_spec_as_the_hand_written_yaml() -> None:
    """The whole design in one assertion: code and YAML produce equal models, not just similar ones."""
    from_builder = _built_research_brief(_registry())
    from_yaml = Workflow.model_validate(yaml.safe_load(RESEARCH_BRIEF_YAML))
    assert from_builder == from_yaml


def test_the_orchestrator_door_writes_the_same_gate_the_yaml_declares() -> None:
    """The pen reaches every verb the grammar has, including the one that names no person."""
    from_builder = (
        WorkflowBuilder("unattended-brief", 1, "Investigate a topic's claims and let this run's own orchestrator clear the brief.")
        .parameter("topic", ParameterSpec(description="the topic to investigate", json_schema={"type": "string"}))
        .run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")})
        .map("investigated", turns=["investigate"], over=FromNode(node="claims"), each="question")
        .orchestrator("vetted", charge=CHARGE, reviews=FromNode(node="investigated"), rejected_to="investigated")
        .run("brief", work="summarise", inputs={"findings": FromNode(node="vetted")})
        .output(FromNode(node="brief"))
        .build()
    )

    assert from_builder == Workflow.model_validate(yaml.safe_load(UNATTENDED_BRIEF_YAML))


def test_the_emitted_spec_validates_clean_against_a_registry() -> None:
    report = validate(_built_research_brief(_registry()), _registry())
    assert report.ok, report.issues


def test_the_map_door_reaches_max_parallel_and_turns_the_way_the_field_surface_does() -> None:
    """`.map()` promises "same fields, same spec" like every other verb -- including the new ones."""
    from_builder = (
        WorkflowBuilder("w", 1, "d")
        .parameter("topic", ParameterSpec(description="the topic to investigate", json_schema={"type": "string"}))
        .run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")})
        .map(
            "investigated",
            turns=["investigate", "judge_evidence"],
            over=FromNode(node="claims"),
            each="question",
            max_parallel=3,
        )
        .output(FromNode(node="investigated"))
        .build()
    )
    direct = MapNode(
        id="investigated",
        turns=("investigate", "judge_evidence"),
        over=FromNode(node="claims"),
        each="question",
        max_parallel=3,
    )

    assert from_builder.nodes[1] == direct


def test_the_run_and_map_doors_reach_tier_the_way_the_field_surface_does() -> None:
    """`.run()` and `.map()` promise "same fields, same spec" like every other verb -- including `tier`."""
    from_builder = (
        WorkflowBuilder("w", 1, "d")
        .parameter("topic", ParameterSpec(description="the topic to investigate", json_schema={"type": "string"}))
        .run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")}, tier="orchestrator")
        .map("investigated", turns=["investigate", "judge_evidence"], over=FromNode(node="claims"), each="question", tier="helper")
        .output(FromNode(node="investigated"))
        .build()
    )
    direct_run = RunNode(id="claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")}, tier="orchestrator")
    direct_map = MapNode(
        id="investigated", turns=("investigate", "judge_evidence"), over=FromNode(node="claims"), each="question", tier="helper"
    )

    assert from_builder.nodes[0] == direct_run
    assert from_builder.nodes[1] == direct_map

    # A multi-turn map runs briefings only, so validating it clean needs `investigate` and
    # `judge_evidence` brought as tasks, not the plain steps `_registry()` registers.
    briefed = from_builder.model_copy(
        update={
            "inline": (
                Briefing(
                    name="investigate",
                    instructions="Investigate the one question this turn was handed, and report what evidence was found.",
                    inputs={"question": Shape(json_schema={"type": "string"})},
                    output=Shape(json_schema={"type": "object", "properties": {"found": {"type": "string"}}, "required": ["found"]}),
                ),
                Briefing(
                    name="judge_evidence",
                    instructions="Judge whether the evidence the previous turn found is good enough to approve the claim.",
                    inputs={"evidence": SameAs(like="investigate")},
                    output=Shape(
                        json_schema={"type": "object", "properties": {"approved": {"type": "boolean"}}, "required": ["approved"]}
                    ),
                ),
            )
        }
    )
    registry = StepRegistry(perform=_perform_stub)
    registry.register(split_claims)
    report = validate(briefed, registry)
    assert report.ok, report.issues


def test_a_wire_naming_an_unknown_node_is_refused_with_the_offending_id() -> None:
    builder = WorkflowBuilder("w", 1, "d")
    with pytest.raises(WiringRefusedError, match=re.escape("brief.inputs[findings]: no node named 'nope'")):
        builder.run("brief", work="summarise", inputs={"findings": FromNode(node="nope")})


def test_a_gates_reviews_wire_naming_an_unknown_node_is_refused() -> None:
    """The check applies to every wire slot a node has, not just `inputs` -- `gate` has none of those."""
    builder = WorkflowBuilder("w", 1, "d")
    with pytest.raises(WiringRefusedError, match=re.escape("no node named 'ghost'")):
        builder.gate("approve", reviews=FromNode(node="ghost"), rejected_to="approve")


def test_a_duplicate_node_id_is_refused_with_its_id() -> None:
    builder = WorkflowBuilder("w", 1, "d").run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")})
    with pytest.raises(WiringRefusedError, match=re.escape("node id 'claims' appears more than once")):
        builder.run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")})


def test_a_duplicate_parameter_name_is_refused() -> None:
    builder = WorkflowBuilder("w", 1, "d").parameter("topic", ParameterSpec(description="one", json_schema={"type": "string"}))
    with pytest.raises(WiringRefusedError, match=re.escape("parameter 'topic' is already declared")):
        builder.parameter("topic", ParameterSpec(description="two", json_schema={"type": "string"}))


def test_an_output_wire_to_nowhere_is_refused() -> None:
    builder = WorkflowBuilder("w", 1, "d")
    with pytest.raises(WiringRefusedError, match=re.escape("no node named 'nowhere'")):
        builder.output(FromNode(node="nowhere"))


def test_build_without_an_output_is_refused() -> None:
    builder = WorkflowBuilder("w", 1, "d").run("claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")})
    with pytest.raises(WiringRefusedError, match="no output wired"):
        builder.build()


def test_a_judge_returning_a_model_must_say_which_field_is_the_verdict() -> None:
    """`judge_evidence` returns a `Verdict`, not a bare bool -- caught the same way the validator would."""
    builder = WorkflowBuilder("w", 1, "d", registry=_registry()).run(
        "looked", work="investigate", inputs={"question": FromParameter(parameter="topic")}
    )
    with pytest.raises(WiringRefusedError, match=re.escape("set `approved_when`")):
        builder.judge("quality", work="judge_evidence", reviews=FromNode(node="looked"), rejected_to="looked")


def test_a_judge_whose_named_field_is_not_a_bool_is_refused() -> None:
    builder = (
        WorkflowBuilder("w", 1, "d", registry=_registry())
        .run("looked", work="investigate", inputs={"question": FromParameter(parameter="topic")})
        .run("scored", work="score_it", inputs={"evidence": FromNode(node="looked")})
    )
    with pytest.raises(WiringRefusedError, match="not bool"):
        builder.judge("quality", work="score_it", reviews=FromNode(node="scored"), rejected_to="looked", approved_when="value")


def test_a_judge_on_a_bare_bool_step_is_accepted() -> None:
    builder = WorkflowBuilder("w", 1, "d", registry=_registry()).run(
        "claims", work="split_claims", inputs={"text": FromParameter(parameter="topic")}
    )
    # Should not raise: `judge_brief` returns a bare bool, which needs no `approved_when`.
    builder.judge("quality", work="judge_brief", reviews=FromNode(node="claims"), rejected_to="claims")


def test_a_judge_shape_check_is_deferred_without_a_registry() -> None:
    """No registry means no way to know a step's return shape -- so the builder does not guess."""
    builder = WorkflowBuilder("w", 1, "d").run(  # no registry
        "looked", work="investigate", inputs={"question": FromParameter(parameter="topic")}
    )
    # Does not raise, even though `judge_evidence` would fail the shape check with a registry.
    builder.judge("quality", work="judge_evidence", reviews=FromNode(node="looked"), rejected_to="looked")
