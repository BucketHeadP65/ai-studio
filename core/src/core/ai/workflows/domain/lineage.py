"""Adapting a workflow by a typed diff of edit operations, never by a rewrite.

The operations are checked as a set before any is applied, so an adaptation applies whole or not at
all. The child records its parent and the operations that produced it, and the incumbent stays byte
for byte as it was. Evidence about a node the diff did not touch carries over to the child. A child
also says why it exists (`evidence`, prose) and what it claims (`predictions`, structural claims a
later run falsifies). `grade` answers the claims from the evidence log, stores no verdict and asks no
model.
"""

from __future__ import annotations

import json

from collections.abc import Container, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from core.ai.workflows.domain.evidence import RunEvidence
from core.ai.workflows.domain.vocabulary import InlineStep, Node, ParameterSpec, SameAs, Wire, Workflow
from core.ai.workflows.exceptions import AdaptationRefusedError

if TYPE_CHECKING:
    from core.ai.workflows.domain.evidence import Approval, Grade

EditOp = Mapping[str, Any]
"""One edit, as data: `{"op": "remove_node", "id": "fetch_costs"}`.

`adapt` refuses an operation outside its closed set."""

_NODE: TypeAdapter[Node] = TypeAdapter(Node)
_WIRE: TypeAdapter[Wire] = TypeAdapter(Wire)
_STEP: TypeAdapter[InlineStep] = TypeAdapter(InlineStep)
_PARAMETER: TypeAdapter[ParameterSpec] = TypeAdapter(ParameterSpec)


class Prediction(BaseModel):
    """A claim about the child that a later run can falsify, drawn from a closed set so it can be graded.

    Each claim is answered from what a `RunEvidence` records:
    `completes` (a run of this version finishes), `produces` (the node named by `subject` yields
    a value, of the type named by `shape` when given), `accepts` (a run is given the parameter
    named by `subject`), `approves` (the gate named by `subject` releases its value).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: Literal["completes", "produces", "accepts", "approves"]
    subject: str = ""
    """The node id, parameter name or gate id the claim is about. Empty only for `completes`."""
    shape: str = ""
    """The type name a `produces` claim demands. Empty accepts whatever type the node yields."""

    @model_validator(mode="after")
    def _answerable(self) -> Prediction:
        """Refuse a claim the evidence log cannot answer, naming what is wrong with it."""
        if self.claim != "completes" and not self.subject:
            msg = f"a {self.claim!r} claim needs a subject. The node id, parameter or gate it is about"
            raise ValueError(msg)
        if self.shape and self.claim != "produces":
            msg = f"a {self.claim!r} claim has no shape to check. Only a 'produces' claim names a type"
            raise ValueError(msg)
        return self


class Lineage(BaseModel):
    """Where a child came from: its parent, the ops that made it, what they touched, and what it claims."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parent_name: str
    parent_version: int
    ops: tuple[EditOp, ...]
    """The edit operations that made the child. Required, so a record that comes back without them
    fails to validate rather than reading as an empty edit."""
    touched: tuple[str, ...] = ()
    """Node ids the diff changed, removed, or added. Every other node carries its evidence over.

    Empty is a valid state, for example after `set_description`, which moves no node."""
    evidence: str = ""
    """Why this version exists, in prose, for the person who later asks.

    It is read and never graded. `predictions` is what gets graded."""
    predictions: tuple[Prediction, ...] = ()
    """What this adaptation claims a later run of the child will show. Empty is a valid state."""


class PredictionOutcome(BaseModel):
    """One claim answered against the evidence log. Derived at read time, never stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prediction: Prediction
    holds: bool | None
    """`True` confirmed, `False` falsified, `None` unmeasurable because no run has shown either yet."""
    observed: str
    """What the evidence log showed, in words a person reads."""


class PredictionRecord(BaseModel):
    """What a body of graded claims adds up to: the trust signal on whoever made them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hits: int = 0
    misses: int = 0
    unmeasurable: int = 0
    """Claims no run has tested, counted apart from misses. An untested claim is not a wrong one."""

    @property
    def measured(self) -> int:
        """Claims the evidence log could answer either way, the only ones `hit_rate` is taken over."""
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float | None:
        """The share of measured claims that held, or `None` when nothing has been measured.

        `0.0` means every measured claim missed.
        """
        return self.hits / self.measured if self.measured else None


def grade(lineage: Lineage, events: Iterable[RunEvidence | Approval | Grade], *, version: int) -> tuple[PredictionOutcome, ...]:
    """Answer an adaptation's claims from the runs of the version it made them about.

    The answer is folded from the evidence log on every call and never stored, and no model is asked.
    Only a completed run of this version answers a claim, except `completes`, which holds once any run of
    this version completes and is falsified when runs exist and none completed. A version nothing has run
    at all is unmeasurable, not wrong.

    Args:
        lineage: The adaptation whose claims are being answered.
        events: Everything the evidence log holds for this workflow. An approval or a `Grade` is not a
            run and grades nothing here.
        version: The child's version. A run that never recorded which version it exercised is
            excluded.

    Returns:
        One outcome per prediction, in the order they were claimed.
    """
    runs = [event for event in events if isinstance(event, RunEvidence) and event.workflow_version == version]
    completed = tuple(run for run in runs if run.outcome == "completed")
    return tuple(_graded(prediction, completed=completed, ran=len(runs)) for prediction in lineage.predictions)


def prediction_record(outcomes: Iterable[PredictionOutcome]) -> PredictionRecord:
    """Fold graded claims into the record their adapter is scored by. It never does the scoring.

    The outcomes may come from as many adaptations as the caller has, and the record scores whoever
    made the claims.

    Args:
        outcomes: Graded claims, from one adaptation or many.

    Returns:
        The count of hits, misses and unmeasurable claims.
    """
    verdicts = tuple(outcome.holds for outcome in outcomes)
    return PredictionRecord(
        hits=sum(holds is True for holds in verdicts),
        misses=sum(holds is False for holds in verdicts),
        unmeasurable=sum(holds is None for holds in verdicts),
    )


def _graded(prediction: Prediction, *, completed: Sequence[RunEvidence], ran: int) -> PredictionOutcome:
    """One claim against this version's runs, as a verdict plus the sentence behind it."""
    if prediction.claim == "completes":
        holds, observed = _completion(completed=len(completed), ran=ran)
    elif not completed:
        holds, observed = None, "no completed run of this version yet"
    elif prediction.claim == "produces":
        # A completed run with no recorded shapes is the observer's silence, not the node's.
        witnesses = tuple(run for run in completed if run.produced)
        holds, observed = _production(prediction, witnesses) if witnesses else (None, "no completed run recorded what it produced")
    elif prediction.claim == "accepts":
        holds, observed = _acceptance(prediction, completed)
    else:
        holds, observed = _approval(prediction, completed)
    return PredictionOutcome(prediction=prediction, holds=holds, observed=observed)


def _completion(*, completed: int, ran: int) -> tuple[bool | None, str]:
    """Did this version finish a run? Failures are the evidence against it, and no run at all is silence."""
    if completed:
        return True, f"{completed} completed run(s) of this version"
    if ran:
        return False, f"{ran} run(s) of this version, none of which completed"
    return None, "no run of this version yet"


def _production(prediction: Prediction, completed: Sequence[RunEvidence]) -> tuple[bool, str]:
    """Did the node yield a value, of the demanded type where one was demanded?"""
    seen = sorted({run.produced[prediction.subject] for run in completed if prediction.subject in run.produced})
    if not seen:
        every = sorted({node for run in completed for node in run.produced})
        return False, f"{len(completed)} completed run(s) produced {_listed(every)}, never {prediction.subject!r}"
    if not prediction.shape or prediction.shape in seen:
        return True, f"{prediction.subject!r} produced {_listed(seen)}"
    return False, f"{prediction.subject!r} produced {_listed(seen)}, not {prediction.shape!r}"


def _acceptance(prediction: Prediction, completed: Sequence[RunEvidence]) -> tuple[bool, str]:
    """Was a run given this parameter? A parameter nothing ever passes is a parameter in name only."""
    given = sum(1 for run in completed if prediction.subject in run.params)
    if given:
        return True, f"{given} completed run(s) were given {prediction.subject!r}"
    every = sorted({name for run in completed for name in run.params})
    return False, f"{len(completed)} completed run(s) were given {_listed(every)}, never {prediction.subject!r}"


def _approval(prediction: Prediction, completed: Sequence[RunEvidence]) -> tuple[bool, str]:
    """Did the gate release its value? Read off run state by the observer, never self-reported."""
    released = sum(1 for run in completed if prediction.subject in run.gates_approved)
    if released:
        return True, f"{prediction.subject!r} released its value in {released} completed run(s)"
    return False, f"no completed run of this version released {prediction.subject!r}"


def _listed(names: Sequence[str]) -> str:
    """Names as a person reads them, and the honest word for none of them."""
    return ", ".join(names) or "nothing"


def adapt(
    workflow: Workflow, *, ops: Sequence[EditOp], evidence: str = "", predictions: Sequence[Prediction] = ()
) -> tuple[Workflow, Lineage]:
    """Apply a typed diff to a workflow, returning the child and its lineage.

    The incumbent is not touched. The child is the next version of the same name.

    Args:
        workflow: The incumbent, which is read and never modified.
        ops: The edits to apply, as data.
        evidence: Why this version exists, in prose. Read by people, graded by nothing.
        predictions: What the child is claimed to show once it runs. Graded later against the evidence log.

    Returns:
        The child and the lineage that records how it was made.

    Raises:
        AdaptationRefusedError: If any op is unknown or names something that is not there. The
            whole set is checked first, so nothing is applied unless everything can be.
    """
    if not ops:
        msg = "an adaptation with no operations is not an adaptation"
        raise AdaptationRefusedError(msg)
    nodes = list(workflow.nodes)
    inline = list(workflow.inline)
    parameters = dict(workflow.parameters)
    description = workflow.description
    output = workflow.output
    touched: list[str] = []
    for index, op in enumerate(ops):
        where = f"op {index} ({op.get('op', '?')})"
        match op.get("op"):
            case "remove_node":
                node_id = _named(op, "id", where)
                if not any(node.id == node_id for node in nodes):
                    msg = f"{where}: no node {node_id!r} to remove"
                    raise AdaptationRefusedError(msg)
                nodes = [node for node in nodes if node.id != node_id]
                touched.append(node_id)
            case "replace_node":
                node_id = _named(op, "id", where)
                position = next((i for i, node in enumerate(nodes) if node.id == node_id), None)
                if position is None:
                    msg = f"{where}: no node {node_id!r} to replace"
                    raise AdaptationRefusedError(msg)
                nodes[position] = _node(_definition(op, where, "with", "node"), where)
                touched.append(node_id)
            case "add_node":
                replacement = _node(_definition(op, where, "node", "with"), where)
                if any(node.id == replacement.id for node in nodes):
                    msg = f"{where}: a node {replacement.id!r} is already here"
                    raise AdaptationRefusedError(msg)
                nodes.append(replacement)
                touched.append(replacement.id)
            case "rewire":
                node_id = _named(op, "id", where)
                position = next((i for i, node in enumerate(nodes) if node.id == node_id), None)
                if position is None:
                    msg = f"{where}: no node {node_id!r} to rewire"
                    raise AdaptationRefusedError(msg)
                nodes[position] = _rewired(nodes[position], op, where)
                touched.append(node_id)
            case "set_output":
                # An adaptation that removes the terminal node names what the workflow answers with here.
                output = _WIRE.validate_python(op.get("to")) if op.get("to") is not None else None
                if output is None:
                    msg = f"{where}: needs a 'to' wire"
                    raise AdaptationRefusedError(msg)
            case "remove_inline_step":
                # `touched` stays as it is because no node moved. The validator refuses the removal
                # of a definition a node still runs.
                task_name = _named(op, "name", where)
                if not any(task.name == task_name for task in inline):
                    msg = f"{where}: this workflow declares no task {task_name!r}"
                    raise AdaptationRefusedError(msg)
                inline = [task for task in inline if task.name != task_name]
            case "replace_inline_step":
                # The replacement keeps the step's name, so every `{like: <task>}` reference still
                # resolves. `_replaced` refuses a rename.
                task_name = _named(op, "name", where)
                position = next((i for i, task in enumerate(inline) if task.name == task_name), None)
                if position is None:
                    msg = f"{where}: this workflow declares no task {task_name!r} to replace"
                    raise AdaptationRefusedError(msg)
                definition = _replaced(_definition(op, where, "with", "task"), named=task_name, where=where)
                touched.extend(_reimplemented(nodes, inline, before=inline[position], after=definition))
                inline[position] = definition
            case "add_inline_step":
                # A step no node ran before. `touched` stays as it is: nothing runs the step until an
                # `add_node` or `replace_node` in this same set does, and those touch their own node.
                inline.append(_added(_definition(op, where, "with", "task"), taken=inline, where=where))
            case "add_parameter":
                # A workflow input the workflow never took. `touched` stays as it is: a parameter
                # changes nothing until a node wires it, and that node's own op in this same set
                # touches it.
                parameter_name = _named(op, "name", where)
                if parameter_name in parameters:
                    msg = f"{where}: this workflow already takes a parameter {parameter_name!r}"
                    raise AdaptationRefusedError(msg)
                parameters[parameter_name] = _parameter(_definition(op, where, "with", "parameter"), where)
            case "set_description":
                description = str(_named(op, "to", where))
            case unknown:
                msg = f"{where}: unknown operation {unknown!r}"
                raise AdaptationRefusedError(msg)
    child = workflow.model_copy(
        update={
            "nodes": tuple(nodes),
            "inline": tuple(inline),
            "parameters": parameters,
            "description": description,
            "output": output,
            "version": workflow.version + 1,
        },
        deep=True,
    )
    lineage = Lineage(
        parent_name=workflow.name,
        parent_version=workflow.version,
        ops=tuple(ops),
        touched=tuple(dict.fromkeys(touched)),
        evidence=evidence,
        predictions=tuple(predictions),
    )
    return child, lineage


def moved_nodes(parent: Workflow, child: Workflow) -> tuple[str, ...]:
    """Every node id that is not the same node in both workflows.

    A node is its wiring and the definition of the inline step it runs, and an added, removed or
    changed node counts as moved. Union the answer into `Lineage.touched` at save time, because a
    draft edited after `adapt` can move more than its operations say.

    Args:
        parent: The workflow the child was adapted from.
        child: The child as it is about to be saved.

    Returns:
        The moved node ids, sorted.
    """
    before, after = _node_prints(parent), _node_prints(child)
    return tuple(sorted(node_id for node_id in before.keys() | after.keys() if before.get(node_id) != after.get(node_id)))


def _node_prints(workflow: Workflow) -> dict[str, str]:
    """Each node's identity as JSON: the node itself, plus the step definition it binds to."""
    inline = {task.name: task.model_dump(mode="json") for task in workflow.inline}
    return {
        node.id: json.dumps([node.model_dump(mode="json"), inline.get(getattr(node, "work", ""))], sort_keys=True) for node in workflow.nodes
    }


def carried_trust(born: RunEvidence, lineage: Lineage) -> tuple[str, ...]:
    """Nodes the parent's recorded run covers that this diff did not touch.

    An untouched node is the node the run recorded, so the run's evidence still describes it.

    Args:
        born: The parent's recorded run.
        lineage: The child's lineage.

    Returns:
        The node ids whose evidence carries over to the child.
    """
    return tuple(node_id for node_id in born.produced if node_id not in lineage.touched)


def _named(op: EditOp, key: str, where: str) -> str:
    value = op.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{where}: needs a {key!r}"
        raise AdaptationRefusedError(msg)
    return value


def _definition(op: EditOp, where: str, *keys: str) -> object:
    """The op's definition payload under whichever of `keys` it arrived, refused when two carry one."""
    present = [key for key in keys if op.get(key) is not None]
    if len(present) > 1:
        msg = f"{where}: carries a definition under both {present[0]!r} and {present[1]!r}. Keep one"
        raise AdaptationRefusedError(msg)
    return op.get(present[0]) if present else None


def _node(payload: object, where: str) -> Node:
    if payload is None:
        msg = (
            f"{where}: needs the node definition under 'with', e.g. "
            '{"kind": "run", "id": "merge", "work": "merge_flags", "inputs": {"candidates": {"node": "flag_ideas"}}}'
        )
        raise AdaptationRefusedError(msg)
    try:
        return _NODE.validate_python(payload)
    except ValidationError as exc:
        msg = f"{where}: that is not a node in this grammar: {exc}"
        raise AdaptationRefusedError(msg) from exc


def _parsed_step(payload: object, where: str) -> InlineStep:
    """An inline step read off an op's `with`, or the refusal for what is not one."""
    if payload is None:
        msg = (
            f"{where}: needs a step definition under 'with', e.g. "
            '{"name": "merge_flags", "instructions": "<the full briefing>", '
            '"inputs": {"candidates": {"like": "flag_ideas", "many": true}}, '
            '"output": {"json_schema": {"type": "array", "items": {"type": "string"}}}}'
        )
        raise AdaptationRefusedError(msg)
    try:
        return _STEP.validate_python(payload)
    except ValidationError as exc:
        msg = f"{where}: that is not an inline step in this grammar: {exc}"
        raise AdaptationRefusedError(msg) from exc


def _parameter(payload: object, where: str) -> ParameterSpec:
    """A workflow input read off an op's `with`, or the refusal for what is not one."""
    if payload is None:
        msg = (
            f"{where}: needs a parameter definition under 'with', e.g. "
            '{"description": "which collection to read", "json_schema": {"type": "string"}, "required": true}'
        )
        raise AdaptationRefusedError(msg)
    try:
        return _PARAMETER.validate_python(payload)
    except ValidationError as exc:
        msg = f"{where}: that is not a parameter in this grammar: {exc}"
        raise AdaptationRefusedError(msg) from exc


def _added(payload: object, *, taken: Sequence[InlineStep], where: str) -> InlineStep:
    """An inline step joining the workflow, parsed by the grammar and refused under a name already declared."""
    step = _parsed_step(payload, where)
    if any(task.name == step.name for task in taken):
        msg = f"{where}: this workflow already declares a task {step.name!r}: replace_inline_step changes it, add_inline_step cannot add a second"
        raise AdaptationRefusedError(msg)
    return step


def _replaced(payload: object, *, named: str, where: str) -> InlineStep:
    """The new definition for an inline step, parsed by the grammar and refused under any other name."""
    step = _parsed_step(payload, where)
    if step.name != named:
        msg = (
            f"{where}: the replacement is named {step.name!r}, not {named!r}. Renaming a step is a removal "
            f"and an addition, never a replacement, because every {{like: {named!r}}} slot names the old one"
        )
        raise AdaptationRefusedError(msg)
    return step


def _reimplemented(nodes: Sequence[Node], inline: Sequence[InlineStep], *, before: InlineStep, after: InlineStep) -> list[str]:
    """Every node a replaced step changed under: the nodes that run it, and every node that borrows a retyped output."""
    definitions = tuple(after if task.name == after.name else task for task in inline)
    retyped = {after.name} if before.output != after.output else set[str]()
    reimplemented = {after.name}
    while True:
        borrowers = {task.name for task in definitions if _borrows(task, retyped)}
        heirs = {task.name for task in definitions if isinstance(task.output, SameAs) and task.output.like in retyped}
        if borrowers <= reimplemented and heirs <= retyped:
            return [node.id for node in nodes if getattr(node, "work", "") in reimplemented]
        reimplemented |= borrowers
        retyped |= heirs


def _borrows(task: InlineStep, retyped: Container[str]) -> bool:
    """Does this step's contract name one of these steps' types, in any of its slots?"""
    return any(isinstance(slot, SameAs) and slot.like in retyped for slot in (*task.inputs.values(), task.output))


def _rewired(node: Node, op: EditOp, where: str) -> Node:
    """Point one of a node's named inputs at something else, refused by the same rules as an authored node."""
    field = _named(op, "input", where)
    inputs = dict(getattr(node, "inputs", {}) or {})
    if field not in inputs:
        msg = f"{where}: {node.id!r} has no input {field!r}. It takes {', '.join(inputs) or 'none'}"
        raise AdaptationRefusedError(msg)
    payload = node.model_dump(mode="json")
    payload["inputs"][field] = op.get("to")
    return _node(payload, where)
