"""Adaptation by typed diff, never rewrite. Trust survives through diffs and dies through rewrites.

An adapted artifact is the incumbent plus a list of edit operations, each one a small
closed thing the grammar can check. Two properties follow, and they are the whole reason
this exists rather than "just write a new spec":

* **Node-granular trust survives.** A diff says exactly which nodes moved, so every node
  it did not touch is still the node that was attested -- `carried_trust` names them.
* **Lineage is kept, never overwritten.** The child records its parent and the ops that
  produced it; the incumbent stays exactly as it was. Supersession is a link, not a
  deletion, because the record of what used to be true is the point.

Edit ops are valid by construction: an op naming a node that is not there is refused
before anything is applied, so a half-applied adaptation cannot exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    AdaptationRefusedError,
    Briefing,
    FromNode,
    FromParameter,
    Lineage,
    ParameterSpec,
    Program,
    RunEvidence,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowState,
    adapt,
    build_graph,
    carried_trust,
    moved_nodes,
    save,
    validate,
)
from core.ai.workflows.domain.vocabulary import SameAs
from core.ai.workflows.infrastructure.folder import FolderLibrary
from tests.unit.workflows.conftest import ApproveAll, CallLog

if TYPE_CHECKING:
    from pathlib import Path


def _stop_before_publishing() -> tuple[dict[str, object], ...]:
    """Drop the publish step and answer with what the gate released instead.

    A real adaptation shape: same evidence gathering, same human decision, one fewer
    side effect -- and it needs `set_output`, because removing the terminal node without
    saying what answers now would leave the artifact pointing at a node that is gone.
    """
    return ({"op": "remove_node", "id": "publish"}, {"op": "set_output", "to": {"node": "approve"}})


def test_an_adaptation_produces_a_child_that_records_its_parent(quarterly_report: Workflow) -> None:
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    assert child.description == "Now with commentary."
    assert child.version == quarterly_report.version + 1, "a child is the next version, not a silent overwrite"
    assert lineage.parent_name == quarterly_report.name
    assert lineage.parent_version == quarterly_report.version
    assert lineage.ops == ({"op": "set_description", "to": "Now with commentary."},)


def test_the_incumbent_is_never_mutated(quarterly_report: Workflow) -> None:
    before = quarterly_report.model_dump()
    adapt(quarterly_report, ops=({"op": "remove_node", "id": "publish"},))
    assert quarterly_report.model_dump() == before, "supersession keeps the incumbent forever"


def test_an_op_naming_a_node_that_is_not_there_is_refused(quarterly_report: Workflow) -> None:
    with pytest.raises(AdaptationRefusedError, match="ghost"):
        adapt(quarterly_report, ops=({"op": "remove_node", "id": "ghost"},))


def test_an_unknown_op_is_refused_rather_than_ignored(quarterly_report: Workflow) -> None:
    with pytest.raises(AdaptationRefusedError, match="rename_everything"):
        adapt(quarterly_report, ops=({"op": "rename_everything"},))


def test_a_refused_adaptation_applies_nothing(quarterly_report: Workflow) -> None:
    """The second op is bad, so the first must not land either."""
    with pytest.raises(AdaptationRefusedError):
        adapt(quarterly_report, ops=({"op": "set_description", "to": "half"}, {"op": "remove_node", "id": "ghost"}))
    assert quarterly_report.description != "half"


def test_an_adapted_spec_still_faces_the_validator(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Valid-by-construction is about the ops, not the result.

    Removing a producer leaves a dangling wire, and the electrician is still the one who
    says so.
    """
    child, _ = adapt(quarterly_report, ops=({"op": "remove_node", "id": "fetch_costs"},))
    report = validate(child, registry)
    assert not report.ok
    assert any("fetch_costs" in issue.detail for issue in report.issues)


async def test_a_coherent_adaptation_runs(quarterly_report: Workflow, registry: StepRegistry, call_log: CallLog) -> None:
    child, _ = adapt(quarterly_report, ops=_stop_before_publishing())
    assert validate(child, registry).ok, validate(child, registry).issues
    await build_graph(child, registry, approval=ApproveAll()).run(state=WorkflowState(params={"quarter": "Q2"}))
    assert "publish" not in call_log.calls, "the removed side effect must not run"
    assert "draft_report" in call_log.calls, "everything the diff left alone still runs"


def test_trust_carries_for_every_node_the_diff_did_not_touch(quarterly_report: Workflow) -> None:
    born = RunEvidence(
        workflow_id="wf.report",
        outcome="completed",
        produced={"fetch_sales": "SalesData", "fetch_costs": "CostData", "reconcile": "Ledger", "publish": "ReportUrl"},
    )
    child, lineage = adapt(quarterly_report, ops=_stop_before_publishing())
    carried = carried_trust(born, lineage)
    assert "fetch_sales" in carried, "an untouched node is still the node that was attested"
    assert "fetch_costs" in carried
    assert "reconcile" in carried
    assert "publish" not in carried, "a removed node carries nothing"
    assert child.name == quarterly_report.name, "an adaptation is a new version of a thing, not a new thing"


def test_lineage_round_trips_as_json(quarterly_report: Workflow) -> None:
    _, lineage = adapt(quarterly_report, ops=_stop_before_publishing())
    assert Lineage.model_validate_json(lineage.model_dump_json()) == lineage


async def test_a_child_is_kept_beside_its_parent_with_the_diff_that_made_it(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """Supersession is a link, not a deletion: both versions stay, and the child says why."""
    store = FolderLibrary(tmp_path / "workflows")
    parent_id = await save(quarterly_report, store, registry, title="Quarterly report", summary="s", applicability="a", saved_by="test")
    child, lineage = adapt(quarterly_report, ops=_stop_before_publishing())
    child_id = await save(
        child, store, registry, title="Quarterly report", summary="s", applicability="a", saved_by="test", lineage=lineage
    )

    assert child_id != parent_id, "a child is its own artifact, not an overwrite"
    assert await store.fetch_workflow(parent_id) == quarterly_report, "the incumbent stays exactly as it was"
    kept = await store.fetch_lineage(child_id)
    assert kept is not None
    assert kept.parent_version == quarterly_report.version
    assert kept.touched == ("publish",)
    assert await store.fetch_lineage(parent_id) is None, "an artifact nobody adapted has no parent"
    assert await store.locate(lineage.parent_name, lineage.parent_version) == parent_id, "and the link has an address"
    assert await store.locate(quarterly_report.name, 99) is None, "a version nobody saved has none"


def test_a_replaced_node_is_a_touched_node(quarterly_report: Workflow, registry: StepRegistry) -> None:
    same_shape = RunNode(id="publish", work="publish", inputs={"report": FromNode(node="approve")})
    child, lineage = adapt(quarterly_report, ops=({"op": "replace_node", "id": "publish", "with": same_shape.model_dump(mode="json")},))
    assert lineage.touched == ("publish",), "replacing a node touches it even when the shape is identical"
    assert validate(child, registry).ok


# --- `touched` is derived, because a draft can be edited after it was adapted -------------------


def test_a_node_the_ops_did_not_name_but_that_moved_anyway_counts_as_touched(quarterly_report: Workflow) -> None:
    """`touched` decides which nodes keep their parent's evidence, so understating it launders trust.

    Adapting produces a child and a diff in one act, and `touched` is right at that moment. But the
    child then becomes a *draft*, and the point of a draft is that it gets edited -- in conversation,
    by the agent, before anybody saves. Every node the ops never named is still claimed as carried.

    So the ops are what the author asked for and this is what actually moved: derived by comparing
    the two specs, and unioned in, so it can only ever widen. Widening can only withdraw trust,
    which is the safe direction for a mistake in it to go.
    """
    child, lineage = adapt(quarterly_report, ops=({"op": "set_description", "to": "Now with commentary."},))
    assert lineage.touched == (), "the diff itself moved no node"

    swapped = RunNode(id="publish", work="reconcile", inputs={"sales": FromNode(node="approve")})
    edited = child.model_copy(update={"nodes": tuple(swapped if n.id == "publish" else n for n in child.nodes)})
    assert moved_nodes(quarterly_report, edited) == ("publish",)


def test_rewriting_a_brought_step_moves_every_node_that_runs_it(quarterly_report: Workflow) -> None:
    """A node is its wiring *and* its briefing, and only one of those lives on the node.

    An artifact that brings its own steps can be changed entirely without touching a `nodes:` entry:
    rewrite `inline[x].instructions` and the node called `x` does something else. Comparing nodes
    alone would report nothing moved and carry the parent's evidence onto a step whose whole
    instruction set is new.
    """
    parent = quarterly_report.model_copy(
        update={
            "inline": (
                Briefing(
                    name="summarise",
                    instructions="Summarise the ledger in one paragraph, and say what you left out.",
                    inputs={"ledger": Shape(json_schema={"type": "string"})},
                    output=Shape(json_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
                ),
            ),
            "nodes": (*quarterly_report.nodes, RunNode(id="summarise", work="summarise", inputs={"ledger": FromNode(node="reconcile")})),
        }
    )
    reworded = parent.model_copy(
        update={"inline": (parent.inline[0].model_copy(update={"instructions": "Summarise the ledger and lead with what is missing."}),)}
    )
    assert moved_nodes(parent, reworded) == ("summarise",)


def test_two_identical_specs_moved_nothing(quarterly_report: Workflow) -> None:
    assert moved_nodes(quarterly_report, quarterly_report) == ()


# --- remove_inline_step: demanded live, by an agent that offered what nothing could honor ----------------


def _with_dead_task(spec: Workflow) -> Workflow:
    """A spec dragging a task definition no node runs -- what an adaptation leaves behind."""
    return spec.model_copy(
        update={
            "inline": (
                Briefing(
                    name="write_brief",
                    instructions="Write the brief from the analysis, leading with the disagreements found.",
                    inputs={"analysis": Shape(json_schema={"type": "string"})},
                    output=Shape(json_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
                ),
            )
        }
    )


def test_a_task_no_node_runs_can_be_shed_by_the_diff(quarterly_report: Workflow) -> None:
    """The gap a live conversation found: adapt away a step's node and its definition stays forever.

    An agent adapted an artifact to stop before its final step, noticed the artifact still carried
    that step's `tasks` entry, and offered to delete it -- an offer nothing could honor, because
    the ops covered nodes, description and output, and never `tasks`. A dead briefing is not
    harmless: it is authored text that keeps shipping, keeps being built at every validation, and
    reads as capability the artifact no longer has.
    """
    child, lineage = adapt(_with_dead_task(quarterly_report), ops=({"op": "remove_inline_step", "name": "write_brief"},))
    assert child.inline == ()
    assert lineage.ops == ({"op": "remove_inline_step", "name": "write_brief"},)
    assert lineage.touched == (), "no node moved; the diff shed a definition, not a step in the graph"


def test_removing_a_task_a_node_still_runs_is_caught_by_the_electrician(quarterly_report: Workflow, registry: StepRegistry) -> None:
    """Same ruling as removing a producer node: the edit applies, the validator refuses the result."""
    spec = _with_dead_task(quarterly_report).model_copy(
        update={
            "nodes": (*quarterly_report.nodes, RunNode(id="write_brief", work="write_brief", inputs={"analysis": FromNode(node="publish")}))
        }
    )
    child, _ = adapt(spec, ops=({"op": "remove_inline_step", "name": "write_brief"},))
    report = validate(child, registry)
    assert not report.ok
    assert any("write_brief" in issue.detail for issue in report.issues)


def test_removing_a_task_that_is_not_there_refuses_the_whole_set(quarterly_report: Workflow) -> None:
    with pytest.raises(AdaptationRefusedError, match="ghost_task"):
        adapt(quarterly_report, ops=({"op": "remove_inline_step", "name": "ghost_task"},))


# --- replace_inline_step: the same conversation, one turn later ------------------------------------------

_BRIEF = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
_SHARPER = {"type": "object", "properties": {"text": {"type": "string"}, "risk": {"type": "string"}}, "required": ["text", "risk"]}
_FILED = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

_BRIEFING = Briefing(
    name="write_brief",
    instructions="Write the brief from the analysis, leading with the disagreements the sources have.",
    inputs={"analysis": Shape(json_schema={"type": "string"})},
    output=Shape(json_schema=_BRIEF),
)
"""The step as prose: a model call, paid on every run and answered a little differently each time."""

_PROGRAM = Program(
    name="write_brief",
    source="{'text': analysis.strip()}",
    inputs={"analysis": Shape(json_schema={"type": "string"})},
    output=Shape(json_schema=_BRIEF),
)
"""The same step as arithmetic, declaring the same result -- what the studio agent was asked for."""

_RETYPED = _PROGRAM.model_copy(update={"source": "{'text': analysis.strip(), 'risk': 'unknown'}", "output": Shape(json_schema=_SHARPER)})
"""The same swap, except the replacement answers with something else -- the case that moves types."""


async def _never_performed(task: Briefing, output_type: type, /, **inputs: object) -> object:  # pragma: no cover - never reached
    del output_type, inputs
    raise AssertionError(task.name)


async def _never_executed(step: Program, output_type: type, /, **inputs: object) -> object:  # pragma: no cover - never reached
    del output_type, inputs
    raise AssertionError(step.name)


def _kit() -> StepRegistry:
    """A run that can both perform a briefing and execute a program, so either kind of step builds.

    These graphs are validated and never run: the question here is whether a swapped-in step still
    passes the electrician, not what it computes.
    """
    return StepRegistry(perform=_never_performed, run_code=_never_executed)


def _brief_writer(step: Briefing | Program = _BRIEFING) -> Workflow:
    """One brought step and the node that runs it -- the artifact the studio agent was adapting."""
    return Workflow(
        name="write-the-brief",
        version=1,
        description="Turn an analysis into a brief.",
        parameters={"analysis": ParameterSpec(description="the analysis to brief from", json_schema={"type": "string"})},
        inline=(step,),
        nodes=(RunNode(id="brief", work="write_brief", inputs={"analysis": FromParameter(parameter="analysis")}),),
        output=FromNode(node="brief"),
    )


def _brief_then_polish(step: Briefing | Program = _BRIEFING) -> Workflow:
    """The same artifact, with two steps downstream that name the brief's type instead of redeclaring it.

    `polish_brief` consumes *and* produces whatever `write_brief` produces, and `file_brief`
    consumes what `polish_brief` produces. So the borrowing is transitive: move the brief's type
    and the polished brief's type moves with it, and the filing step's input moves after that --
    none of it visible in any of their own definitions, which is the whole difficulty.
    """
    base = _brief_writer(step)
    polish = Briefing(
        name="polish_brief",
        instructions="Tighten the brief without dropping a disagreement, and keep every number exactly as given.",
        inputs={"draft": SameAs(like="write_brief")},
        output=SameAs(like="write_brief"),
    )
    file_it = Briefing(
        name="file_brief",
        instructions="File the polished brief where the quarter's readers look for it, and answer with its address.",
        inputs={"brief": SameAs(like="polish_brief")},
        output=Shape(json_schema=_FILED),
    )
    return base.model_copy(
        update={
            "inline": (step, polish, file_it),
            "nodes": (
                *base.nodes,
                RunNode(id="polish", work="polish_brief", inputs={"draft": FromNode(node="brief")}),
                RunNode(id="file", work="file_brief", inputs={"brief": FromNode(node="polish")}),
            ),
            "output": FromNode(node="file"),
        }
    )


def _swap(step: Briefing | Program) -> dict[str, object]:
    """The op the field event needed: a task's name, and the whole new definition to put under it."""
    return {"op": "replace_inline_step", "name": step.name, "with": step.model_dump(mode="json")}


def test_a_briefing_becomes_a_program_without_the_artifact_losing_its_lineage() -> None:
    """The gap the next turn of that same conversation found: a step could not change its implementation.

    The agent was asked to turn a brought briefing into a program and refused correctly -- the ops
    could remove a task and never add or replace one, so the only route was `remove_inline_step` followed
    by a re-save. That breaks every `{like: write_brief}` reference, puts `unknown_work` under the
    node, and stores an orphan: the lineage that made adapting worth doing is spent to change one
    step's implementation.
    """
    child, lineage = adapt(_brief_writer(), ops=(_swap(_PROGRAM),))
    assert child.inline == (_PROGRAM,), "the step is now a program, under the same name"
    assert lineage.ops == (_swap(_PROGRAM),)
    assert lineage.touched == ("brief",), "the node's wiring did not move, but what it runs is new"
    assert validate(child, _kit()).ok, validate(child, _kit()).issues


def test_the_incumbent_keeps_its_own_step_after_a_replacement() -> None:
    parent = _brief_writer()
    before = parent.model_dump()
    adapt(parent, ops=(_swap(_PROGRAM),))
    assert parent.model_dump() == before, "supersession keeps the incumbent forever, steps included"


def test_a_step_borrowing_the_replaced_type_carries_when_the_replacement_answers_the_same_way() -> None:
    """The line that makes trust node-granular rather than merely cautious.

    `polish_brief` says `{like: write_brief}`, so its contract is defined by the replaced step. If
    the replacement declares the same result, nothing about the polishing step changed and its
    parent's evidence still describes it. Touching it anyway would withdraw trust from a node
    nothing happened to, which is a real cost: it is exactly the evidence the next tier is folded
    from.
    """
    child, lineage = adapt(_brief_then_polish(), ops=(_swap(_PROGRAM),))
    assert lineage.touched == ("brief",), "an identical result moves no borrowed type"
    assert validate(child, _kit()).ok, validate(child, _kit()).issues


def test_a_step_borrowing_the_replaced_type_moves_with_it_when_the_replacement_answers_differently() -> None:
    """And the other direction, which is the one that would launder trust if it were missed.

    A `{like: write_brief}` slot names the replaced step's type *by name*, so a replacement that
    produces something else changes the borrower's contract without a byte of the borrower's own
    definition moving. `moved_nodes` compares each node against its own definition and sees
    nothing; the validator is satisfied precisely *because* the reference still resolves. The
    only place that can notice is the op that made the swap, so it notices here -- and it follows
    the chain, because a step whose own output is borrowed passes the move along.
    """
    child, lineage = adapt(_brief_then_polish(), ops=(_swap(_RETYPED),))
    assert lineage.touched == ("brief", "polish", "file"), "the type moved under all three, one of them at two removes"
    assert validate(child, _kit()).ok, "the graph still wires up, which is exactly why nothing else would catch this"
    assert moved_nodes(_brief_then_polish(), child) == ("brief",), "and comparing definitions alone sees only the step that changed"


def test_replacing_a_task_that_is_not_there_refuses_the_whole_set() -> None:
    with pytest.raises(AdaptationRefusedError, match="ghost_task"):
        adapt(_brief_writer(), ops=({"op": "replace_inline_step", "name": "ghost_task", "with": _PROGRAM.model_dump(mode="json")},))


def test_a_replacement_under_a_different_name_is_refused_rather_than_quietly_renaming() -> None:
    """A rename is a removal and an addition. Letting it in here would leave every `{like:}` dangling."""
    renamed = _PROGRAM.model_copy(update={"name": "compose_brief"})
    with pytest.raises(AdaptationRefusedError, match="compose_brief"):
        adapt(_brief_writer(), ops=({"op": "replace_inline_step", "name": "write_brief", "with": renamed.model_dump(mode="json")},))


def test_a_replacement_that_is_not_a_step_in_this_grammar_is_refused() -> None:
    """Same door as `replace_node`: the payload is parsed by the grammar, never trusted as given."""
    with pytest.raises(AdaptationRefusedError, match="inline step"):
        adapt(_brief_writer(), ops=({"op": "replace_inline_step", "name": "write_brief", "with": {"name": "write_brief"}},))


def test_the_definition_is_accepted_under_the_ops_own_noun() -> None:
    """`with` is the documented key, but a caller reaching for `task`, `node` or `parameter` has said what they mean.

    Live, an agent put the step under `task:` and the node under `node:` and burned two of its
    retries on nothing but the key's name -- an error for a form the door could have accepted.
    """
    swapped, _ = adapt(_brief_writer(), ops=({"op": "replace_inline_step", "name": "write_brief", "task": _PROGRAM.model_dump(mode="json")},))
    assert swapped.inline == (_PROGRAM,)

    rewired, _ = adapt(
        _brief_writer(),
        ops=(
            {
                "op": "replace_node",
                "id": "brief",
                "node": {"kind": "run", "id": "brief", "work": "write_brief", "inputs": {"analysis": {"parameter": "analysis"}}},
            },
        ),
    )
    assert rewired.version == 2

    widened, _ = adapt(_brief_writer(), ops=({"op": "add_parameter", "name": "focus", "parameter": _FOCUS},))
    assert "focus" in widened.parameters


def test_a_definition_under_two_keys_is_two_answers_to_one_question() -> None:
    with pytest.raises(AdaptationRefusedError, match="both 'with' and 'task'"):
        adapt(
            _brief_writer(),
            ops=(
                {
                    "op": "replace_inline_step",
                    "name": "write_brief",
                    "with": _PROGRAM.model_dump(mode="json"),
                    "task": _BRIEFING.model_dump(mode="json"),
                },
            ),
        )


def test_a_missing_definition_is_refused_with_an_example_to_copy() -> None:
    """The refusal shows a valid payload, not just the rule -- an agent mid-correction copies, it does not deduce."""
    with pytest.raises(AdaptationRefusedError, match=r"needs a step definition under 'with', e\.g\."):
        adapt(_brief_writer(), ops=({"op": "replace_inline_step", "name": "write_brief"},))
    with pytest.raises(AdaptationRefusedError, match=r"needs the node definition under 'with', e\.g\."):
        adapt(_brief_writer(), ops=({"op": "replace_node", "id": "brief"},))


def test_a_replacement_the_validator_cannot_build_is_still_the_electrician_to_refuse() -> None:
    """Valid-by-construction is about the edit. A program on a run with no sandbox is a finding, not a refusal."""
    child, _ = adapt(_brief_writer(), ops=(_swap(_PROGRAM),))
    report = validate(child, StepRegistry(perform=_never_performed))
    assert not report.ok
    assert any("write_brief" in issue.detail for issue in report.issues)


# --- add_inline_step: the third task op, demanded live by a redesign that had to hijack `finish` ----------


def _summarizer() -> Briefing:
    """A step the artifact never declared, consuming the brief's own shape."""
    return Briefing(
        name="summarize_brief",
        instructions="Reduce the brief to its three strongest disagreements, in the brief's own words.",
        inputs={"brief": SameAs(like="write_brief")},
        output=SameAs(like="write_brief"),
    )


def test_a_new_task_joins_the_artifact_and_the_node_added_to_run_it_builds() -> None:
    """Before this op, a redesign needing a new step had to rename an existing task or re-save and lose the lineage."""
    summarizer = _summarizer()
    ops = (
        {"op": "add_inline_step", "with": summarizer.model_dump(mode="json")},
        {"op": "add_node", "node": {"kind": "run", "id": "summary", "work": "summarize_brief", "inputs": {"brief": {"node": "brief"}}}},
        {"op": "set_output", "to": {"node": "summary"}},
    )

    child, lineage = adapt(_brief_writer(), ops=ops)

    assert [task.name for task in child.inline] == ["write_brief", "summarize_brief"]
    assert child.version == 2
    assert lineage.ops == ops
    assert validate(child, _kit()).ok, "the electrician builds the added step like any brought one"


def test_adding_a_step_under_a_declared_name_is_refused_as_replace_inline_steps_business() -> None:
    with pytest.raises(AdaptationRefusedError, match=r"already declares a task 'write_brief'.*replace_inline_step"):
        adapt(_brief_writer(), ops=({"op": "add_inline_step", "with": _PROGRAM.model_dump(mode="json")},))


def test_adding_something_that_is_not_a_step_refuses_the_whole_set() -> None:
    with pytest.raises(AdaptationRefusedError, match="not an inline step"):
        adapt(_brief_writer(), ops=({"op": "add_inline_step", "with": {"name": "summarize_brief"}},))
    with pytest.raises(AdaptationRefusedError, match="needs a step definition under 'with'"):
        adapt(_brief_writer(), ops=({"op": "add_inline_step"},))


# --- add_parameter: a workflow input the artifact never took, so a hand fire can be scoped --------


_FOCUS = {"description": "One theme to keep the brief on; empty means the whole analysis.", "json_schema": {"type": "string"}, "required": False}


def test_a_new_parameter_joins_the_artifact_and_the_node_wired_to_it_builds() -> None:
    """Before this op, giving a saved workflow a scoping input meant a re-save that lost the lineage."""
    focused = _BRIEFING.model_copy(update={"inputs": {**_BRIEFING.inputs, "focus": Shape(json_schema={"type": "string"})}})
    ops = (
        {"op": "add_parameter", "name": "focus", "with": _FOCUS},
        _swap(focused),
        {
            "op": "replace_node",
            "id": "brief",
            "with": {"kind": "run", "id": "brief", "work": "write_brief", "inputs": {"analysis": {"parameter": "analysis"}, "focus": {"parameter": "focus"}}},
        },
    )

    child, lineage = adapt(_brief_writer(), ops=ops)

    assert list(child.parameters) == ["analysis", "focus"]
    assert child.parameters["focus"].required is False
    assert child.version == 2
    assert lineage.ops == ops
    assert validate(child, _kit()).ok, "the electrician wires the added parameter like any declared one"


def test_a_parameter_alone_touches_no_node() -> None:
    child, lineage = adapt(_brief_writer(), ops=({"op": "add_parameter", "name": "focus", "with": _FOCUS},))

    assert lineage.touched == ()
    assert child.nodes == _brief_writer().nodes


def test_adding_a_parameter_under_a_declared_name_is_refused() -> None:
    with pytest.raises(AdaptationRefusedError, match=r"already takes a parameter 'analysis'"):
        adapt(_brief_writer(), ops=({"op": "add_parameter", "name": "analysis", "with": _FOCUS},))


def test_adding_something_that_is_not_a_parameter_refuses_the_whole_set() -> None:
    with pytest.raises(AdaptationRefusedError, match="not a parameter in this grammar"):
        adapt(_brief_writer(), ops=({"op": "add_parameter", "name": "focus", "with": {"json_schema": {"type": "string"}}},))
    with pytest.raises(AdaptationRefusedError, match="needs a parameter definition under 'with'"):
        adapt(_brief_writer(), ops=({"op": "add_parameter", "name": "focus"},))
    with pytest.raises(AdaptationRefusedError, match="needs a 'name'"):
        adapt(_brief_writer(), ops=({"op": "add_parameter", "with": _FOCUS},))
