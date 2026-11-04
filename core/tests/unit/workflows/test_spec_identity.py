"""Content addressing: an artifact's identity is its spec, never the address a store gave it.

The symptom these pin: trust keyed on a store address is lost the moment the artifact moves.
`wf.audit-a-claim.v1` in a collection and `audit-a-claim@1` in a folder are the same artifact,
the graduation path (personal store -> team -> production) changes the address at every
promotion, and this repo's own collection already re-keyed once (one page per name -> one page
per version). Every one of those turns runs with evidence into an empty lookup, which folds to
`runs_with_evidence=0` and reports `unverified` with full confidence.

Canonicalisation is the whole risk, so these pin it from both sides: two specs that are equal
must hash equally however they travelled, and any single field moving must move the hash.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import create_model

from core.ai.workflows.contracts import (
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    MapNode,
    OrchestratorGate,
    ParameterSpec,
    PersonGate,
    Program,
    ResultArtifactSpec,
    RunNode,
    Shape,
    Workflow,
    WorkflowResultSpec,
    spec_identity,
)
from core.ai.workflows.domain.fingerprint import _canonical_document
from core.ai.workflows.domain.vocabulary import SameAs

_PINNED = "5ce08f4154bd3698f092bd4b7a69afbfb6f5e7b6f3d907fde9dac1cb3d475162"
"""The digest of the fixed spec below, under this pydantic and this grammar, with defaults left out. See the test."""


def test_a_spec_that_crossed_a_store_keeps_its_identity(quarterly_report: Workflow) -> None:
    """A move must not orphan history: the spec goes over the wire as JSON and comes back keyed the same.

    This is the defect in one line. If the round trip changed the hash, content addressing would
    move the orphaning bug rather than fix it.
    """
    revived = Workflow.model_validate_json(quarterly_report.model_dump_json())
    assert revived == quarterly_report
    assert spec_identity(revived) == spec_identity(quarterly_report)


def test_a_spec_whose_mapping_keys_came_back_reordered_keeps_its_identity() -> None:
    """The concrete store that does this: `LibraryCollection` keeps the spec in a jsonb column.

    jsonb does not preserve object key order, so `parameters` and a node's `inputs` come back in
    whatever order Postgres chose. Pydantic equality over mappings ignores order and so must the
    hash -- otherwise every artifact in that store re-keys itself on the way out.
    """
    first = Workflow(
        name="reorder",
        version=1,
        description="Two parameters and a two-input node.",
        parameters={
            "alpha": ParameterSpec(description="a", json_schema={"type": "string"}),
            "beta": ParameterSpec(description="b", json_schema={"type": "string"}),
        },
        nodes=(RunNode(id="one", work="one", inputs={"a": FromParameter(parameter="alpha"), "b": FromParameter(parameter="beta")}),),
        output=FromNode(node="one"),
    )
    reordered = Workflow(
        name="reorder",
        version=1,
        description="Two parameters and a two-input node.",
        parameters={
            "beta": ParameterSpec(description="b", json_schema={"type": "string"}),
            "alpha": ParameterSpec(description="a", json_schema={"type": "string"}),
        },
        nodes=(RunNode(id="one", work="one", inputs={"b": FromParameter(parameter="beta"), "a": FromParameter(parameter="alpha")}),),
        output=FromNode(node="one"),
    )
    assert reordered == first
    assert spec_identity(reordered) == spec_identity(first)


def test_a_field_omitted_and_a_field_set_to_its_default_are_one_identity(quarterly_report: Workflow) -> None:
    """A store that writes `inline: []` back for a spec that never mentioned it must not re-key it.

    "Unset" is a property of how the object was built, not of what the artifact is, and it does not
    survive a store. The identity leaves every default out, so written and unwritten hash the same.
    """
    written = quarterly_report.model_dump(mode="json")
    assert written["inline"] == [], "the empty container is written, so it must also be hashed"
    assert spec_identity(Workflow.model_validate(written)) == spec_identity(quarterly_report)


def test_an_absent_result_stays_out_of_the_dump_and_an_explicit_result_moves_identity(quarterly_report: Workflow) -> None:
    undeclared_dump = quarterly_report.model_dump(mode="json")
    declared = quarterly_report.model_copy(update={"result": WorkflowResultSpec(summary="A reconciled quarterly report.", primary_work="draft_report")})

    assert "result" not in undeclared_dump
    assert declared.model_dump(mode="json")["result"] == {"summary": "A reconciled quarterly report.", "primary_work": "draft_report", "empty_primary": "unknown", "required_gates": [], "artifacts": []}
    assert spec_identity(declared) != spec_identity(quarterly_report)


def test_a_field_none_artifact_has_the_whole_value_identity() -> None:
    result = WorkflowResultSpec(
        summary="The rendered brief.",
        primary_work="render",
        artifacts=(ResultArtifactSpec(id="brief", label="Brief", source=FromNode(node="render"), media_type="text/markdown"),),
    )
    whole = Workflow(
        name="whole-result",
        version=1,
        description="A workflow whose artifact is the full value produced by its render node.",
        nodes=(RunNode(id="render", work="render"),),
        output=FromNode(node="render"),
        result=result,
    )
    with_explicit_none = whole.model_copy(
        update={
            "result": result.model_copy(
                update={"artifacts": (result.artifacts[0].model_copy(update={"field": None}),)}
            )
        }
    )

    assert "field" not in result.artifacts[0].model_dump(mode="json")
    assert whole == with_explicit_none
    assert spec_identity(whole) == spec_identity(with_explicit_none)


def test_every_field_of_the_spec_moves_the_identity(quarterly_report: Workflow) -> None:
    """A hash blind to a field would let a changed artifact inherit the evidence of the old one.

    `version` is in the list on purpose: it is a field of the spec, so version-scoped trust falls
    out of content addressing rather than needing the separate filter.
    """
    task = Briefing(
        name="summarise",
        instructions="Summarise the reconciled ledger in at most three sentences for a reader in a hurry.",
        output=Shape(json_schema={"type": "string"}),
    )
    variants = {
        "baseline": quarterly_report,
        "name": quarterly_report.model_copy(update={"name": "quarterly-summary"}),
        "version": quarterly_report.model_copy(update={"version": 4}),
        "description": quarterly_report.model_copy(update={"description": "Something else entirely."}),
        "parameters": quarterly_report.model_copy(
            update={"parameters": {"quarter": ParameterSpec(description="Calendar quarter", json_schema={"type": "string"})}}
        ),
        "inline": quarterly_report.model_copy(update={"inline": (task,)}),
        "nodes": quarterly_report.model_copy(
            update={"nodes": (*quarterly_report.nodes[:-1], RunNode(id="publish", work="publish", inputs={}))}
        ),
        "output": quarterly_report.model_copy(update={"output": FromNode(node="approve")}),
    }
    hashes = {field: spec_identity(spec) for field, spec in variants.items()}
    assert len(set(hashes.values())) == len(hashes), f"two variants share an identity: {hashes}"


def test_a_reordered_graph_is_a_different_identity(quarterly_report: Workflow) -> None:
    """Deliberate, not a canonicalisation failure: the hash tracks pydantic equality exactly.

    `nodes` is a tuple, so a reordered spec is `!=` the original. Sorting the nodes before hashing
    would hand two unequal values one identity, which is the laundering direction of the same bug.
    """
    swapped = quarterly_report.model_copy(
        update={"nodes": (quarterly_report.nodes[1], quarterly_report.nodes[0], *quarterly_report.nodes[2:])}
    )
    assert swapped != quarterly_report
    assert spec_identity(swapped) != spec_identity(quarterly_report)


def test_the_identity_of_a_fixed_spec_is_pinned_to_a_literal() -> None:
    """What actually pins canonicalisation. Nothing else in this file would notice it moving.

    A pydantic release that serialises differently, or a field added to the grammar without a default,
    shifts every hash and orphans every evidence record written before it. It is the same class of bug, one
    layer out. This fails loudly when that happens, and the fix is a decision (migrate the evidence log,
    or accept the re-key), never a silent re-baseline of this constant. The last such decision moved the
    identity to leave defaults out, with the evidence log migrated forward in the same change.

    The spec is built here rather than taken from a fixture: a shared fixture is a second thing
    that can move the digest, and then the pin no longer says which of the two did.
    """
    pinned = Workflow(
        name="pinned",
        version=1,
        description="One parameter, one step.",
        parameters={"topic": ParameterSpec(description="What to look at", json_schema={"type": "string"})},
        nodes=(RunNode(id="look", work="look", inputs={"topic": FromParameter(parameter="topic")}),),
        output=FromNode(node="look"),
    )
    assert spec_identity(pinned) == _PINNED


def test_an_identity_is_a_whole_sha256(quarterly_report: Workflow) -> None:
    """Untruncated, unlike the fingerprint's stamps: a collision here transfers trust.

    A short digest is fine for drift, where a collision costs a missed staleness warning. It is
    not fine for a key that decides whose runs vouch for what.
    """
    identity = spec_identity(quarterly_report)
    assert len(identity) == 64
    assert set(identity) <= set("0123456789abcdef")


def test_a_gate_and_a_run_node_of_the_same_id_are_different_identities() -> None:
    """The grammar's discriminator is part of the content: swapping a person gate for a step is a new artifact."""
    gated = Workflow(
        name="approval",
        version=1,
        description="One step behind one gate.",
        nodes=(RunNode(id="draft", work="draft"), PersonGate(id="check", reviews=FromNode(node="draft"), rejected_to="draft")),
        output=FromNode(node="check"),
    )
    ungated = gated.model_copy(update={"nodes": (gated.nodes[0], RunNode(id="check", work="check", inputs={"x": FromNode(node="draft")}))})
    assert spec_identity(ungated) != spec_identity(gated)


def test_a_wire_naming_a_turn_is_a_different_identity_than_the_same_wire_without_one() -> None:
    """`FromNode.turn` is dropped from the dump only when unset -- naming one must still move the hash.

    Pins the other side of the omit-when-`None` fix: a wire that DOES name a turn is not the
    ordinary wire the fix protects, and must keep a distinct identity from one that names none.
    """
    unturned = Workflow(
        name="turned",
        version=1,
        description="One map, one node wired from its first turn.",
        nodes=(
            MapNode(id="sources", turns=("digest", "relate"), over=FromParameter(parameter="items"), each="item"),
            RunNode(id="report", work="report", inputs={"x": FromNode(node="sources")}),
        ),
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array"})},
        output=FromNode(node="report"),
    )
    turned = unturned.model_copy(
        update={
            "nodes": (
                unturned.nodes[0],
                RunNode(id="report", work="report", inputs={"x": FromNode(node="sources", turn="digest")}),
            )
        }
    )
    assert turned != unturned
    assert spec_identity(turned) != spec_identity(unturned)


_PINNED_GATED = "757992718162c4cc3c4164bbea5a0cfd6af9d5144ea2f37a1cb834db0901164c"
"""The digest of the gated spec below. Every gate carries a ceiling and none of them says so."""


def _gated_pin() -> Workflow:
    """One draft behind all three gate kinds, none of them naming a ceiling."""
    return Workflow(
        name="gated-pin",
        version=1,
        description="One draft behind a judge, a person and this run's own orchestrator.",
        nodes=(
            RunNode(id="draft", work="draft"),
            JudgeGate(id="judged", work="judge", reviews=FromNode(node="draft"), rejected_to="draft"),
            PersonGate(id="approved", reviews=FromNode(node="judged"), rejected_to="draft"),
            OrchestratorGate(
                id="orchestrated",
                charge="Check the draft against every constraint the request named, and say which one fails.",
                reviews=FromNode(node="approved"),
                rejected_to="draft",
            ),
        ),
        output=FromNode(node="orchestrated"),
    )


def test_a_gate_at_the_default_ceiling_keeps_the_identity_every_saved_workflow_already_has() -> None:
    """A gate that names no ceiling dumps exactly as it always did, so its evidence stays its own.

    The digest is pinned rather than compared against a rebuild: a rebuild moves with the code,
    and what must not move is the key every gated run filed its evidence under.
    """
    pinned = _gated_pin()

    gates = [node for node in pinned.nodes if isinstance(node, JudgeGate | PersonGate | OrchestratorGate)]
    assert [gate.max_attempts for gate in gates] == [3, 3, 3]
    assert "max_attempts" not in json.dumps(pinned.model_dump(mode="json"))
    assert spec_identity(pinned) == _PINNED_GATED


def test_a_named_ceiling_is_part_of_the_workflow_and_moves_its_identity() -> None:
    """A ceiling somebody chose is a fact about the workflow, dumped and hashed like any other."""
    pinned = _gated_pin()
    tightened = pinned.model_copy(update={"nodes": (pinned.nodes[0], *(node.model_copy(update={"max_attempts": 1}) for node in pinned.nodes[1:]))})

    assert json.loads(tightened.model_dump_json())["nodes"][1]["max_attempts"] == 1
    assert spec_identity(tightened) != _PINNED_GATED


def test_a_ceiling_written_out_at_its_default_is_the_workflow_that_never_named_one() -> None:
    """Saying `max_attempts: 3` and saying nothing are the same workflow, so they key the same."""
    document = json.loads(_gated_pin().model_dump_json())
    for node in document["nodes"][1:]:
        node["max_attempts"] = 3

    assert spec_identity(Workflow.model_validate(document)) == _PINNED_GATED


def test_a_field_added_to_the_format_with_a_default_keeps_every_identity(quarterly_report: Workflow) -> None:
    """A code change that adds a defaulted field leaves every saved workflow's identity, and so its trust, where it was."""
    with_a_new_field = create_model("WorkflowWithANewField", __base__=Workflow, review_note=(str, "none yet"))
    revived = with_a_new_field.model_validate(quarterly_report.model_dump(mode="json"))

    assert revived.model_dump(mode="json")["review_note"] == "none yet"
    assert spec_identity(revived) == spec_identity(quarterly_report)


def test_a_field_added_with_a_default_still_moves_the_identity_once_somebody_sets_it(quarterly_report: Workflow) -> None:
    with_a_new_field = create_model("WorkflowWithANewField", __base__=Workflow, review_note=(str, "none yet"))
    revived = with_a_new_field.model_validate({**quarterly_report.model_dump(mode="json"), "review_note": "checked by the owner"})

    assert spec_identity(revived) != spec_identity(quarterly_report)


def test_a_changed_default_moves_the_identity_of_a_stored_document_that_wrote_the_old_default(quarterly_report: Workflow) -> None:
    """A library keeps the full dump, so once the default changes the stored old default reads as a value somebody chose."""
    before = create_model("WorkflowBefore", __base__=Workflow, review_note=(str, "none yet"))
    after = create_model("WorkflowAfter", __base__=Workflow, review_note=(str, "pending"))
    stored = before.model_validate(quarterly_report.model_dump(mode="json")).model_dump(mode="json")

    assert stored["review_note"] == "none yet"
    assert spec_identity(before.model_validate(stored)) == spec_identity(quarterly_report)
    assert spec_identity(after.model_validate(stored)) != spec_identity(quarterly_report)


def test_a_changed_default_keeps_the_identity_of_a_document_that_left_the_field_out(quarterly_report: Workflow) -> None:
    """The identity stays, and the workflow runs under the new default."""
    after = create_model("WorkflowAfter", __base__=Workflow, review_note=(str, "pending"))
    revived = after.model_validate(quarterly_report.model_dump(mode="json"))

    assert revived.model_dump(mode="json")["review_note"] == "pending"
    assert spec_identity(revived) == spec_identity(quarterly_report)


def test_the_stored_dump_writes_every_default_in_but_four() -> None:
    """What a library keeps: every defaulted field written in, except the four the serializers leave out at their defaults."""
    workflow = Workflow(
        name="stored-defaults",
        version=1,
        description="A map, a step and a gate, every default left to the format.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array"})},
        nodes=(
            MapNode(id="each", turns=("digest",), over=FromParameter(parameter="items"), each="item"),
            RunNode(id="report", work="report", inputs={"digests": FromNode(node="each")}),
            PersonGate(id="approve", reviews=FromNode(node="report"), rejected_to="report"),
        ),
        output=FromNode(node="approve"),
    )

    stored = workflow.model_dump(mode="json")

    assert stored["inline"] == []
    assert stored["parameters"]["items"]["required"] is True
    assert {key: stored["nodes"][0][key] for key in ("inputs", "max_parallel", "on_error", "abort_after_repeats", "tier")} == {
        "inputs": {}, "max_parallel": None, "on_error": "fail", "abort_after_repeats": 3, "tier": None,
    }
    assert "turn" not in stored["nodes"][1]["inputs"]["digests"]
    assert "max_attempts" not in stored["nodes"][2]
    assert "result" not in stored


def _sha256(document: object) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_an_identity_hashes_only_what_differs_from_a_default_at_every_depth() -> None:
    """Nested models and lists drop their defaults too, and every node keeps its `kind`, which is what it is."""
    workflow = Workflow(
        name="defaults-everywhere",
        version=1,
        description="A map over a parameter, a step with no inputs, and a declared result.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array"})},
        nodes=(
            MapNode(id="each", turns=("digest",), over=FromParameter(parameter="items"), each="item"),
            RunNode(id="report", work="report"),
            PersonGate(id="approve", reviews=FromNode(node="report"), rejected_to="report"),
        ),
        output=FromNode(node="approve"),
        result=WorkflowResultSpec(summary="The report.", primary_work="report"),
    )
    written = {
        "name": "defaults-everywhere",
        "version": 1,
        "description": "A map over a parameter, a step with no inputs, and a declared result.",
        "parameters": {"items": {"description": "items", "json_schema": {"type": "array"}}},
        "nodes": [
            {"kind": "map", "id": "each", "turns": ["digest"], "over": {"parameter": "items"}, "each": "item"},
            {"kind": "run", "id": "report", "work": "report"},
            {"kind": "gate", "id": "approve", "reviews": {"node": "report"}, "rejected_to": "report"},
        ],
        "output": {"node": "approve"},
        "result": {"summary": "The report.", "primary_work": "report"},
    }

    assert _canonical_document(workflow) == written
    assert spec_identity(workflow) == _sha256(written)


def test_the_canonical_document_reads_back_as_the_same_workflow() -> None:
    """Every union is still picked from what the document holds, so leaving defaults out never merges two workflows."""
    workflow = Workflow(
        name="every-kind",
        version=2,
        description="Every node kind and every inline step kind, each with what it may leave out left out.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array"})},
        inline=(
            Briefing(
                name="digest",
                instructions="Digest one item into one line that names its claim and its source, nothing else.",
                output=Shape(json_schema={"type": "string"}),
            ),
            Program(name="count", source="len(lines)", inputs={"lines": Shape(json_schema={"type": "array"})}, output=Shape(json_schema={"type": "integer"})),
            AgentStep(name="weigh", agent={"model": "test", "instructions": "Weigh the count."}, inputs={"total": SameAs(like="count")}, output=SameAs(like="digest")),
            AgentStep(name="scout", agent="scout", output=Shape(json_schema={"type": "string"})),
        ),
        nodes=(
            MapNode(id="each", turns=("digest",), over=FromParameter(parameter="items"), each="item"),
            RunNode(id="counted", work="count", inputs={"lines": FromNode(node="each")}),
            JudgeGate(id="judged", work="weigh", reviews=FromNode(node="counted"), rejected_to="counted"),
            PersonGate(id="approved", reviews=FromNode(node="judged"), rejected_to="counted", max_attempts=2),
            OrchestratorGate(
                id="orchestrated",
                charge="Check the count against the items the run was given, and say which item went missing.",
                reviews=FromNode(node="approved"),
                rejected_to="counted",
            ),
            RunNode(id="scouted", work="scout"),
        ),
        output=FromNode(node="orchestrated"),
        result=WorkflowResultSpec(summary="The checked count.", primary_work="counted"),
    )

    assert Workflow.model_validate(_canonical_document(workflow)) == workflow


def test_a_nested_field_written_at_its_default_is_the_workflow_that_left_it_out() -> None:
    left_out = Workflow(
        name="nested-defaults",
        version=1,
        description="One map.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array"})},
        nodes=(MapNode(id="each", turns=("digest",), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="each"),
    )
    written_out = left_out.model_copy(
        update={
            "parameters": {"items": ParameterSpec(description="items", json_schema={"type": "array"}, required=True)},
            "inline": (),
            "nodes": (
                MapNode(
                    id="each", turns=("digest",), over=FromParameter(parameter="items"), each="item",
                    inputs={}, max_parallel=None, on_error="fail", abort_after_repeats=3, tier=None,
                ),
            ),
        }
    )

    assert spec_identity(written_out) == spec_identity(left_out)


def test_a_nested_value_moved_off_its_default_moves_the_identity() -> None:
    base = Workflow(
        name="nested-values",
        version=1,
        description="One map.",
        parameters={"items": ParameterSpec(description="items", json_schema={"type": "array"})},
        nodes=(MapNode(id="each", turns=("digest",), over=FromParameter(parameter="items"), each="item"),),
        output=FromNode(node="each"),
    )
    node = base.nodes[0]
    variants = {
        "baseline": base,
        "no ceiling": base.model_copy(update={"nodes": (node.model_copy(update={"abort_after_repeats": None}),)}),
        "skip": base.model_copy(update={"nodes": (node.model_copy(update={"on_error": "skip"}),)}),
        "optional": base.model_copy(
            update={"parameters": {"items": ParameterSpec(description="items", json_schema={"type": "array"}, required=False)}}
        ),
    }
    hashes = {name: spec_identity(spec) for name, spec in variants.items()}
    assert len(set(hashes.values())) == len(hashes), f"two variants share an identity: {hashes}"
