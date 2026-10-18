"""Workflow validation: the mechanical checks a workflow passes before anything runs.

The checks are that ids resolve, steps exist, types line up, the wire graph is acyclic and each gate
rewinds only into its own history. A parameter wire is checked only for existence and for a scalar
type mismatch. The full type check of a parameter value happens when the workflow is invoked.
Type assignability is nominal (`issubclass`). It catches a wire between unrelated classes and says
nothing about the shape inside a class.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, assert_never, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from core.ai.workflows.domain.vocabulary import (
    VERDICTS,
    AgentStep,
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    MapNode,
    Node,
    OrchestratorGate,
    PersonGate,
    Program,
    RequiredGateCheck,
    ResultArtifactSpec,
    RunNode,
    Wire,
    Workflow,
)

if TYPE_CHECKING:
    from core.ai.workflows.registry import StepImplementation, StepRegistry


class IssueType(StrEnum):
    """What type of problem one wiring issue reports."""

    DUPLICATE_NODE_ID = "duplicate_node_id"
    UNKNOWN_NODE_REF = "unknown_node_ref"
    UNKNOWN_PARAMETER_REF = "unknown_parameter_ref"
    UNKNOWN_WORK = "unknown_work"
    INPUT_MISMATCH = "input_mismatch"
    TYPE_MISMATCH = "type_mismatch"
    WIRE_CYCLE = "wire_cycle"
    BAD_REJECTED_TO = "bad_rejected_to"
    DEAD_END_NODE = "dead_end_node"
    BAD_TASK_DEFINITION = "bad_task_definition"
    BAD_VERDICT = "bad_verdict"
    BLANK_CHARGE = "blank_charge"
    NO_ORCHESTRATOR = "no_orchestrator"
    BAD_MAX_PARALLEL = "bad_max_parallel"
    BAD_ABORT_AFTER_REPEATS = "bad_abort_after_repeats"
    DUPLICATE_TURN_NAME = "duplicate_turn_name"
    BAD_TURN_KIND = "bad_turn_kind"
    BAD_TURN_WIRE = "bad_turn_wire"
    JUDGE_ACTS = "judge_acts"
    PARTIAL_REWIND = "partial_rewind"
    BAD_RESULT = "bad_result"


class WiringIssue(BaseModel):
    """One problem validation found: its type, where it anchors and what is wrong."""

    model_config = ConfigDict(frozen=True)

    kind: IssueType
    where: str
    """Node id (or ``output``) the issue anchors to."""
    detail: str


class ValidationReport(BaseModel):
    """What validation found about one workflow: every wiring issue, or none."""

    model_config = ConfigDict(frozen=True)

    issues: tuple[WiringIssue, ...]

    @property
    def ok(self) -> bool:
        """Whether validation found no issue."""
        return not self.issues


@dataclass(frozen=True)
class Prepared:
    """The registry a workflow was validated against, and the validation report.

    Run the workflow against this `registry`, never against a second build of its inline steps.
    A second build makes new classes for the same declared shapes, and nominal typing refuses them.
    """

    registry: StepRegistry
    """The registry, plus the workflow's inline steps."""
    report: ValidationReport


def prepare(workflow: Workflow, registry: StepRegistry) -> Prepared:
    """Build the workflow's inline steps, then validate the whole wiring against the result.

    Args:
        workflow: The workflow to validate.
        registry: The steps this run can execute. An inline step it cannot build is reported
            as an issue by name, with the reason. It is never raised or skipped.

    Returns:
        The registry with the inline steps added, and the validation report.
    """
    built, problems = registry.including(workflow)
    issues = [WiringIssue(kind=IssueType.BAD_TASK_DEFINITION, where=where, detail=detail) for where, detail in problems]
    issues.extend(_wiring_issues(workflow, built))
    return Prepared(registry=built, report=ValidationReport(issues=tuple(issues)))


def validate(workflow: Workflow, registry: StepRegistry) -> ValidationReport:
    """Run every mechanical check on a workflow and report every issue, never only the first.

    Args:
        workflow: The workflow to validate.
        registry: The steps this run can execute.

    Returns:
        The validation report.
    """
    return prepare(workflow, registry).report


def _wiring_issues(workflow: Workflow, registry: StepRegistry) -> list[WiringIssue]:
    issues: list[WiringIssue] = []
    nodes = _collect_nodes(workflow, issues)
    # Inline steps that failed to build, which `_unknown_step` reports as a build failure.
    unbuilt = frozenset(task.name for task in workflow.inline) - frozenset(registry.names)
    inline: Mapping[str, Briefing] = {task.name: task for task in workflow.inline if isinstance(task, Briefing)}

    for node in nodes.values():
        _check_wires(node, workflow, nodes, issues)
        if isinstance(node, RunNode):
            _check_run_node(node, registry, issues, unbuilt)
        elif isinstance(node, MapNode):
            _check_map_node(node, registry, workflow, issues, unbuilt)
        elif isinstance(node, JudgeGate):
            _check_judge_node(node, registry, inline, issues, unbuilt)
        elif isinstance(node, OrchestratorGate):
            _check_orchestrator_node(node, registry, issues)
    _check_wire(workflow.output, where="output", workflow=workflow, nodes=nodes, issues=issues)
    _check_dead_ends(workflow, nodes, issues)

    cycle = _find_cycle(nodes)
    if cycle is not None:
        issues.append(WiringIssue(kind=IssueType.WIRE_CYCLE, where=cycle[0], detail=f"wire cycle: {' -> '.join(cycle)}"))
    else:
        _check_on_the_dag(workflow, registry, nodes, issues)
    _check_result_declaration(workflow, registry if cycle is None else None, nodes, issues)

    return issues


def _check_on_the_dag(workflow: Workflow, registry: StepRegistry, nodes: dict[str, Node], issues: list[WiringIssue]) -> None:
    """Run the checks that walk the wire graph, which are sound only once it is known to be acyclic."""
    reach = _reach(nodes)
    _check_rewinds_into_joins(nodes, reach, issues)
    for node in nodes.values():
        if isinstance(node, RunNode):
            _check_types(node, registry, nodes, workflow, issues)
        elif isinstance(node, MapNode):
            _check_map_types(node, registry, nodes, workflow, issues)
        elif isinstance(node, JudgeGate):
            _check_judge_types(node, registry, nodes, issues)
            _check_rejected_to(node, nodes, issues)
        else:
            _check_rejected_to(node, nodes, issues)


def _check_result_declaration(
    workflow: Workflow, registry: StepRegistry | None, nodes: dict[str, Node], issues: list[WiringIssue]
) -> None:
    """Check that each declared result selection resolves against this graph's real outputs."""
    result = workflow.result
    if result is None:
        return
    primary = nodes.get(result.primary_work)
    if primary is None:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where="result.primary_work",
                detail=f"primary work {result.primary_work!r} names no node in this workflow",
            )
        )
    elif not isinstance(primary, RunNode | MapNode):
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where="result.primary_work",
                detail=f"primary work {result.primary_work!r} must name a run or map, not {primary.kind!r}",
            )
        )
    elif result.empty_primary == "noop" and not isinstance(primary, MapNode):
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where="result.primary_work",
                detail=f"a noop empty primary must name a map, not {primary.kind!r}",
            )
        )
    for index, gate in enumerate(result.required_gates):
        _check_required_gate(gate, index, nodes, issues)
    artifact_ids: set[str] = set()
    for index, artifact in enumerate(result.artifacts):
        if artifact.id in artifact_ids:
            issues.append(
                WiringIssue(
                    kind=IssueType.BAD_RESULT,
                    where=f"result.artifacts[{index}].id",
                    detail=f"artifact id {artifact.id!r} appears more than once. Choose a unique id",
                )
            )
        artifact_ids.add(artifact.id)
        issues.extend(_check_result_artifact(artifact, index, workflow, registry, nodes))


def _check_required_gate(
    check: RequiredGateCheck, index: int, nodes: dict[str, Node], issues: list[WiringIssue]
) -> None:
    """Check one required gate's node and declared decision source."""
    node = nodes.get(check.gate_id)
    source: Literal["judge", "person", "orchestrator"] | None
    if isinstance(node, JudgeGate):
        source = "judge"
    elif isinstance(node, PersonGate):
        source = "person"
    elif isinstance(node, OrchestratorGate):
        source = "orchestrator"
    else:
        source = None
    where = f"result.required_gates[{index}].gate_id"
    if source is None:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where=where,
                detail=f"required gate {check.gate_id!r} must name a judge, person, or orchestrator gate",
            )
        )
    elif source not in check.accepted_sources:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where=where,
                detail=(
                    f"required gate {check.gate_id!r} is a {source} gate, but this result accepts: "
                    f"{', '.join(check.accepted_sources) or 'no gate sources'}"
                ),
            )
        )


def _check_result_artifact(
    artifact: ResultArtifactSpec,
    index: int,
    workflow: Workflow,
    registry: StepRegistry | None,
    nodes: dict[str, Node],
) -> list[WiringIssue]:
    """Check an artifact's source wire and optional selected field."""
    issues: list[WiringIssue] = []
    where = f"result.artifacts[{index}].source"
    _check_wire(artifact.source, where=where, workflow=workflow, nodes=nodes, issues=issues)
    for issue_index, issue in enumerate(issues):
        issues[issue_index] = issue.model_copy(update={"detail": f"artifact source wire: {issue.detail}"})
    if artifact.field is None or registry is None:
        return issues
    output_type = static_type(artifact.source, nodes, registry)
    fields = getattr(output_type, "model_fields", None)
    field_where = f"result.artifacts[{index}].field"
    if fields is None:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where=field_where,
                detail=f"artifact field {artifact.field!r} requires an object source with named fields",
            )
        )
        return issues
    selected = fields.get(artifact.field)
    if selected is None:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where=field_where,
                detail=f"artifact source has no field {artifact.field!r}. It has: {', '.join(fields) or 'none'}",
            )
        )
    elif artifact.media_type in ("text/markdown", "text/plain") and selected.annotation is not str:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_RESULT,
                where=field_where,
                detail=(
                    f"artifact field {artifact.field!r} is {_type_name(selected.annotation)}, but "
                    f"{artifact.media_type!r} requires a string field"
                ),
            )
        )
    return issues


def unsatisfied_parameters(workflow: Workflow, supplied: Iterable[str]) -> tuple[str, ...]:
    """The required parameters the caller has not supplied.

    `validate` does not check this.

    Args:
        workflow: The workflow whose parameters are read.
        supplied: The names of the parameters the caller supplies.

    Returns:
        The missing names, in the order the workflow declares them.
    """
    have = set(supplied)
    return tuple(name for name, param in workflow.parameters.items() if param.required and name not in have)


def static_type(wire: Wire, nodes: dict[str, Node], registry: StepRegistry) -> type | None:
    """The type known before the run that flows on a wire, or None where only the run knows it.

    A parameter wire always gives None. A gate passes its reviewed value through, so it gives
    its source's type.

    Args:
        wire: The wire to read.
        nodes: The workflow's nodes by id.
        registry: The steps the run can execute.

    Returns:
        The type, or None.
    """
    match wire:
        case FromParameter():
            return None
        case FromNode(node=node_id):
            source = nodes.get(node_id)
            if isinstance(source, RunNode):
                impl = registry.get(source.work)
                return impl.output_type if impl is not None else None
            if isinstance(source, MapNode):
                # The last turn's output is what the map collects, unless the wire names an
                # earlier turn's own list explicitly.
                turn = wire.turn if wire.turn is not None else source.turns[-1]
                impl = registry.get(turn)
                return list[impl.output_type] if impl is not None else None  # type: ignore[misc,name-defined]
            if isinstance(source, PersonGate | JudgeGate | OrchestratorGate):
                return static_type(source.reviews, nodes, registry)
            return None
        case _ as unreachable:
            assert_never(unreachable)


def _collect_nodes(workflow: Workflow, issues: list[WiringIssue]) -> dict[str, Node]:
    nodes: dict[str, Node] = {}
    for node in workflow.nodes:
        if node.id in nodes:
            issues.append(
                WiringIssue(kind=IssueType.DUPLICATE_NODE_ID, where=node.id, detail=f"node id {node.id!r} appears more than once")
            )
            continue
        nodes[node.id] = node
    return nodes


def wires_of(node: Node) -> Sequence[tuple[str, Wire]]:
    """Every wire one node reads, each with the slot it fills.

    Args:
        node: The node to read.

    Returns:
        Pairs of the slot, such as `inputs[name]` or `reviews`, and its wire.
    """
    match node:
        case RunNode():
            return [(f"inputs[{name}]", wire) for name, wire in node.inputs.items()]
        case JudgeGate():
            return [("reviews", node.reviews), *((f"inputs[{name}]", wire) for name, wire in node.inputs.items())]
        case PersonGate() | OrchestratorGate():
            return [("reviews", node.reviews)]
        case MapNode():
            return [
                ("over", node.over),
                *((f"inputs[{name}]", wire) for name, wire in node.inputs.items()),
            ]
        case _ as unreachable:
            assert_never(unreachable)


def _check_wires(node: Node, workflow: Workflow, nodes: dict[str, Node], issues: list[WiringIssue]) -> None:
    for slot, wire in wires_of(node):
        _check_wire(wire, where=f"{node.id}.{slot}", workflow=workflow, nodes=nodes, issues=issues)


def _check_wire(wire: Wire, *, where: str, workflow: Workflow, nodes: dict[str, Node], issues: list[WiringIssue]) -> None:
    match wire:
        case FromNode(node=node_id, turn=turn):
            if node_id not in nodes:
                issues.append(WiringIssue(kind=IssueType.UNKNOWN_NODE_REF, where=where, detail=f"no node named {node_id!r}"))
            elif turn is not None:
                source = nodes[node_id]
                if not isinstance(source, MapNode):
                    issues.append(
                        WiringIssue(
                            kind=IssueType.BAD_TURN_WIRE,
                            where=where,
                            detail=f"{node_id!r} is not a map, so it has no turn named {turn!r} to wire from",
                        )
                    )
                elif len(source.turns) == 1:
                    # A single-turn map records no `<node id>.<step name>` value, so a turn wire on
                    # one would raise UnrecordedTurnError when the run reaches it.
                    issues.append(
                        WiringIssue(
                            kind=IssueType.BAD_TURN_WIRE,
                            where=where,
                            detail=f"map {node_id!r} runs one turn, wire it by node alone",
                        )
                    )
                elif turn not in source.turns:
                    issues.append(
                        WiringIssue(
                            kind=IssueType.BAD_TURN_WIRE,
                            where=where,
                            detail=f"map {node_id!r} has no turn named {turn!r}. It runs: {', '.join(source.turns)}",
                        )
                    )
        case FromParameter(parameter=parameter) if parameter not in workflow.parameters:
            issues.append(WiringIssue(kind=IssueType.UNKNOWN_PARAMETER_REF, where=where, detail=f"no parameter named {parameter!r}"))
        case _:
            pass


def _unknown_step(work: str, node_id: str, registry: StepRegistry, unbuilt: frozenset[str] = frozenset()) -> WiringIssue:
    """The unknown-step issue, naming what the run can execute, or saying an inline step failed to build."""
    if work in unbuilt:
        return WiringIssue(
            kind=IssueType.UNKNOWN_WORK,
            where=node_id,
            detail=(
                f"work {work!r} is declared by this workflow but failed to build. See the "
                f"bad_task_definition finding for inline[{work}]. Nothing needs registering, fix the definition"
            ),
        )
    available = ", ".join(registry.names) or "nothing (this run registered no work at all)"
    return WiringIssue(
        kind=IssueType.UNKNOWN_WORK, where=node_id, detail=f"work {work!r} is not registered. This run can execute: {available}"
    )


def _check_run_node(node: RunNode, registry: StepRegistry, issues: list[WiringIssue], unbuilt: frozenset[str] = frozenset()) -> None:
    impl = registry.get(node.work)
    if impl is None:
        issues.append(_unknown_step(node.work, node.id, registry, unbuilt))
        return
    missing = sorted(impl.required - node.inputs.keys())
    extra = sorted(node.inputs.keys() - impl.input_types.keys())
    if missing:
        issues.append(
            WiringIssue(
                kind=IssueType.INPUT_MISMATCH, where=node.id, detail=f"work {node.work!r} requires unwired inputs: {', '.join(missing)}"
            )
        )
    if extra:
        issues.append(
            WiringIssue(kind=IssueType.INPUT_MISMATCH, where=node.id, detail=f"work {node.work!r} has no such inputs: {', '.join(extra)}")
        )


# The JSON Schema scalar types each Python type accepts. A boolean fits an int slot, since
# bool subclasses int, and an integer fits a float slot.
_SCALAR_SCHEMA_FOR: dict[type, frozenset[str]] = {
    str: frozenset({"string"}),
    int: frozenset({"integer", "boolean"}),
    float: frozenset({"number", "integer"}),
    bool: frozenset({"boolean"}),
}
_SCALAR_SCHEMA_TYPES = frozenset({"string", "integer", "number", "boolean"})


def _check_parameter_scalar(wire: Wire, expected: type, *, work: str, where: str, workflow: Workflow) -> WiringIssue | None:
    """Report a parameter wire only when both sides are scalar types that cannot match."""
    if not isinstance(wire, FromParameter):
        return None
    param = workflow.parameters.get(wire.parameter)
    if param is None:  # unknown parameter: already reported by _check_wire, nothing to compare against
        return None
    declared = param.json_schema.get("type")
    accepted = _SCALAR_SCHEMA_FOR.get(expected)
    if not isinstance(declared, str) or declared not in _SCALAR_SCHEMA_TYPES or accepted is None or declared in accepted:
        return None
    return WiringIssue(
        kind=IssueType.TYPE_MISMATCH,
        where=where,
        detail=(
            f"parameter {wire.parameter!r} is declared {{'type': {declared!r}}} but work {work!r} expects "
            f"{_type_name(expected)} here. Wire a parameter of the right type, or fix this parameter's json_schema"
        ),
    )


def _check_types(node: RunNode, registry: StepRegistry, nodes: dict[str, Node], workflow: Workflow, issues: list[WiringIssue]) -> None:
    impl = registry.get(node.work)
    if impl is None:
        return
    for name, wire in node.inputs.items():
        expected = impl.input_types.get(name)
        if expected is None:
            continue
        if (scalar := _check_parameter_scalar(wire, expected, work=node.work, where=f"{node.id}.inputs[{name}]", workflow=workflow)) is not None:
            issues.append(scalar)
        actual = static_type(wire, nodes, registry)
        if actual is not None and not _assignable(actual, expected):
            issues.append(
                WiringIssue(
                    kind=IssueType.TYPE_MISMATCH,
                    where=f"{node.id}.inputs[{name}]",
                    detail=f"wire carries {_type_name(actual)}, work {node.work!r} expects {_type_name(expected)}",
                )
            )


def _turn_kind_name(turn: str, inline: Mapping[str, Program | Briefing | AgentStep]) -> str:
    """How a non-briefing turn is described in a refusal, by what it actually is."""
    brought = inline.get(turn)
    if isinstance(brought, AgentStep):
        return "a worker step"
    if isinstance(brought, Program):
        return "a program"
    return "a step this app registered"


def _uncovered_params(impl: StepImplementation, node: MapNode) -> frozenset[str]:
    """This turn's required parameters that neither `each` nor the map's shared `inputs` name."""
    return impl.required - node.inputs.keys() - {node.each}


def turn_feed(impl: StepImplementation, node: MapNode, *, first: bool) -> str | None:
    """The parameter of a map turn that receives the previous turn's output, or None.

    It is the one required parameter that neither `each` nor the map's `inputs` covers.
    Validation refuses a first turn with an uncovered parameter and any turn with two, so a
    validated workflow always gets one answer.

    Args:
        impl: The turn's step.
        node: The map node.
        first: Whether this is the map's first turn, which has no previous output.

    Returns:
        The parameter name, or None for the first turn or when no single parameter is uncovered.
    """
    if first:
        return None
    uncovered = _uncovered_params(impl, node)
    return next(iter(uncovered)) if len(uncovered) == 1 else None


def _check_map_node(
    node: MapNode, registry: StepRegistry, workflow: Workflow, issues: list[WiringIssue], unbuilt: frozenset[str] = frozenset()
) -> None:
    """Check a map's turns: each resolves, a multi-turn map runs briefings only, and each turn's inputs line up."""
    multi = len(node.turns) > 1
    inline = {task.name: task for task in workflow.inline}
    seen: set[str] = set()
    impls: list[StepImplementation | None] = []
    for turn in node.turns:
        if turn in seen:
            issues.append(
                WiringIssue(
                    kind=IssueType.DUPLICATE_TURN_NAME, where=node.id, detail=f"turn {turn!r} appears more than once in this map's turns"
                )
            )
            impls.append(None)
            continue
        seen.add(turn)
        impl = registry.get(turn)
        if impl is None:
            issues.append(_unknown_step(turn, node.id, registry, unbuilt))
        elif multi and not isinstance(inline.get(turn), Briefing):
            issues.append(
                WiringIssue(
                    kind=IssueType.BAD_TURN_KIND,
                    where=f"{node.id}.turns",
                    detail=f"a multi turn map runs briefings only, turn {turn!r} is {_turn_kind_name(turn, inline)}",
                )
            )
        impls.append(impl)

    if node.each in node.inputs:
        issues.append(
            WiringIssue(
                kind=IssueType.INPUT_MISMATCH,
                where=f"{node.id}.inputs[{node.each}]",
                detail=f"{node.each!r} is the mapped parameter. It comes from `over`, one item at a time, so it cannot also be wired",
            )
        )

    if node.max_parallel is not None and node.max_parallel < 1:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_MAX_PARALLEL,
                where=f"{node.id}.max_parallel",
                detail=f"max_parallel must be at least 1, or left unset for unbounded fan-out. Got {node.max_parallel}",
            )
        )
    if node.abort_after_repeats is not None and node.abort_after_repeats < 1:
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_ABORT_AFTER_REPEATS,
                where=f"{node.id}.abort_after_repeats",
                detail=f"abort_after_repeats must be at least 1, or left unset to disable the ceiling. Got {node.abort_after_repeats}",
            )
        )

    first_impl = impls[0] if impls else None
    if first_impl is not None and node.each not in first_impl.input_types:
        issues.append(
            WiringIssue(
                kind=IssueType.INPUT_MISMATCH,
                where=f"{node.id}.each",
                detail=(
                    f"the first turn, work {node.turns[0]!r}, has no parameter {node.each!r}. "
                    f"It takes {', '.join(first_impl.input_types) or 'nothing'}"
                ),
            )
        )

    named = {name for impl in impls if impl is not None for name in impl.input_types}
    extra = sorted(name for name in node.inputs if name not in named)
    if extra:
        issues.append(
            WiringIssue(
                kind=IssueType.INPUT_MISMATCH,
                where=node.id,
                detail=f"no turn in this map takes an input named: {', '.join(extra)}",
            )
        )

    for index, (turn, impl) in enumerate(zip(node.turns, impls, strict=True)):
        if impl is None:
            continue
        uncovered = sorted(_uncovered_params(impl, node))
        if index == 0:
            if uncovered:
                issues.append(
                    WiringIssue(
                        kind=IssueType.INPUT_MISMATCH,
                        where=f"{node.id}.turns[{turn}]",
                        detail=f"the first turn, work {turn!r}, requires unwired inputs: {', '.join(uncovered)}",
                    )
                )
        elif len(uncovered) > 1:
            issues.append(
                WiringIssue(
                    kind=IssueType.INPUT_MISMATCH,
                    where=f"{node.id}.turns[{turn}]",
                    detail=(
                        f"work {turn!r} requires unwired inputs: {', '.join(uncovered)}. A later turn may leave at "
                        f"most one uncovered, to receive the previous turn's output"
                    ),
                )
            )


def judge_verdict_problem(output_type: type, approved_when: str | None, *, work: str) -> str | None:
    """What is wrong with a step's output type as a judge's verdict, or None when it can serve.

    It reads only the output type and the verdict field, with no registry and no graph.

    Args:
        output_type: The type the judge's step returns.
        approved_when: The field that carries the verdict, or None when the step returns a bare bool.
        work: The step's name, as the problem names it.

    Returns:
        The problem, written for the workflow's author, or None.
    """
    if approved_when is None:
        if output_type is bool:
            return None
        return (
            f"work {work!r} returns {_type_name(output_type)}, not bool, so this judge must say which "
            f"field carries the verdict: set `approved_when`"
        )
    fields = getattr(output_type, "model_fields", None)
    if fields is None:
        return f"work {work!r} returns {_type_name(output_type)}, which has no fields to read a verdict from"
    field = fields.get(approved_when)
    if field is None:
        return f"{_type_name(output_type)} has no field {approved_when!r}. It has {', '.join(fields) or 'none'}"
    if field.annotation is bool or (get_origin(field.annotation) is Literal and set(get_args(field.annotation)) == set(VERDICTS)):
        return None
    return (
        f"{_type_name(output_type)}.{approved_when} is {_type_name(field.annotation)}, not bool and not the "
        f"verdict set approve, reject, cannot_judge"
    )


def _check_judge_node(
    node: JudgeGate,
    registry: StepRegistry,
    inline: Mapping[str, Briefing],
    issues: list[WiringIssue],
    unbuilt: frozenset[str] = frozenset(),
) -> None:
    """Check that a judge's step takes the reviewed value and its wired inputs, has a readable verdict and only reads."""
    impl = registry.get(node.work)
    if impl is None:
        issues.append(_unknown_step(node.work, node.id, registry, unbuilt))
        return
    uncovered = sorted(impl.required - node.inputs.keys())
    extra = sorted(node.inputs.keys() - impl.input_types.keys())
    if len(uncovered) != 1:
        issues.append(
            WiringIssue(
                kind=IssueType.INPUT_MISMATCH,
                where=node.id,
                detail=(
                    f"a judge's work takes exactly one required input (the thing it judges) but "
                    f"{node.work!r} steps {len(uncovered)} uncovered: {', '.join(uncovered) or 'none'}"
                ),
            )
        )
    if extra:
        issues.append(
            WiringIssue(kind=IssueType.INPUT_MISMATCH, where=node.id, detail=f"work {node.work!r} has no such inputs: {', '.join(extra)}")
        )
    detail = judge_verdict_problem(impl.output_type, node.approved_when, work=node.work)
    if detail is not None:
        where = node.id if node.approved_when is None else f"{node.id}.approved_when"
        issues.append(WiringIssue(kind=IssueType.BAD_VERDICT, where=where, detail=detail))
    task = inline.get(node.work)
    if task is not None and registry.judge_tools is not None:
        acting = sorted(set(task.tools) - registry.judge_tools)
        if acting:
            issues.append(
                WiringIssue(
                    kind=IssueType.JUDGE_ACTS,
                    where=node.id,
                    detail=(
                        f"judge {node.id!r} holds {', '.join(acting)}, which can act. A judge only reads, so it may "
                        f"hold: {', '.join(sorted(registry.judge_tools))}"
                    ),
                )
            )


def _check_orchestrator_node(node: OrchestratorGate, registry: StepRegistry, issues: list[WiringIssue]) -> None:
    """Check that the run has an orchestrator to ask and that the gate's charge is not blank."""
    if not registry.decides_gates:
        issues.append(
            WiringIssue(
                kind=IssueType.NO_ORCHESTRATOR,
                where=node.id,
                detail=(
                    "this run cannot decide an orchestrator gate. It has no orchestrator, and a gate that names "
                    "no person is never waved through instead"
                ),
            )
        )
    if not node.charge.strip():
        issues.append(
            WiringIssue(
                kind=IssueType.BLANK_CHARGE,
                where=f"{node.id}.charge",
                detail="the charge is blank. An orchestrator gate is the brief it hands its decider, and there is nothing here to read",
            )
        )


def _check_map_types(node: MapNode, registry: StepRegistry, nodes: dict[str, Node], workflow: Workflow, issues: list[WiringIssue]) -> None:
    """Type-check a map's shared inputs and mapped item against each turn, and each feed against the previous turn's output."""
    known: dict[str, StepImplementation] = {}
    for turn in node.turns:
        impl = registry.get(turn)
        if impl is not None:
            known[turn] = impl

    for name, wire in node.inputs.items():
        for turn, impl in known.items():
            expected = impl.input_types.get(name)
            if expected is None:
                continue
            if (scalar := _check_parameter_scalar(wire, expected, work=turn, where=f"{node.id}.inputs[{name}]", workflow=workflow)) is not None:
                issues.append(scalar)
            actual = static_type(wire, nodes, registry)
            if actual is not None and not _assignable(actual, expected):
                issues.append(
                    WiringIssue(
                        kind=IssueType.TYPE_MISMATCH,
                        where=f"{node.id}.inputs[{name}]",
                        detail=f"wire carries {_type_name(actual)}, work {turn!r} expects {_type_name(expected)}",
                    )
                )

    collection = static_type(node.over, nodes, registry)
    if collection is not None:
        item = _element_type(collection)
        if item is None:
            issues.append(
                WiringIssue(
                    kind=IssueType.TYPE_MISMATCH,
                    where=f"{node.id}.over",
                    detail=f"wire carries {_type_name(collection)}, which is not a collection this can map over",
                )
            )
        else:
            for turn, impl in known.items():
                expected_item = impl.input_types.get(node.each)
                if expected_item is not None and not _assignable(item, expected_item):
                    issues.append(
                        WiringIssue(
                            kind=IssueType.TYPE_MISMATCH,
                            where=f"{node.id}.over",
                            detail=(
                                f"wire carries {_type_name(collection)}, so each item is {_type_name(item)}, "
                                f"but {turn!r} expects {_type_name(expected_item)} for {node.each!r}"
                            ),
                        )
                    )

    previous: StepImplementation | None = None
    for index, turn in enumerate(node.turns):
        impl = known.get(turn)
        if impl is not None and index > 0 and previous is not None:
            feed = turn_feed(impl, node, first=False)
            if feed is not None:
                expected = impl.input_types[feed]
                if not _assignable(previous.output_type, expected):
                    issues.append(
                        WiringIssue(
                            kind=IssueType.TYPE_MISMATCH,
                            where=f"{node.id}.turns[{turn}]",
                            detail=(
                                f"work {node.turns[index - 1]!r} returns {_type_name(previous.output_type)}, but "
                                f"{turn!r} expects {_type_name(expected)} for {feed!r}"
                            ),
                        )
                    )
        if impl is not None:
            previous = impl


def _check_judge_types(node: JudgeGate, registry: StepRegistry, nodes: dict[str, Node], issues: list[WiringIssue]) -> None:
    impl = registry.get(node.work)
    if impl is None:
        return
    uncovered = impl.required - node.inputs.keys()
    if len(uncovered) != 1:
        return
    (parameter,) = uncovered
    expected = impl.input_types.get(parameter)
    actual = static_type(node.reviews, nodes, registry)
    if expected is not None and actual is not None and not _assignable(actual, expected):
        issues.append(
            WiringIssue(
                kind=IssueType.TYPE_MISMATCH,
                where=f"{node.id}.reviews",
                detail=f"wire carries {_type_name(actual)}, judge {node.work!r} expects {_type_name(expected)} for {parameter!r}",
            )
        )


def _element_type(collection: object) -> type | None:
    """The item type of a parameterised list, tuple, set or sequence, or None for anything else."""
    origin = get_origin(collection)
    if origin is None or not isinstance(origin, type) or not issubclass(origin, list | tuple | set | frozenset | Sequence):
        return None
    args = [arg for arg in get_args(collection) if arg is not Ellipsis]
    if len(args) != 1 or not isinstance(args[0], type):
        return None
    return args[0]


def _check_dead_ends(workflow: Workflow, nodes: dict[str, Node], issues: list[WiringIssue]) -> None:
    """Report every node that nothing reads and that is not the workflow's output."""
    consumed = {wire.node for node in nodes.values() for _, wire in wires_of(node) if isinstance(wire, FromNode)}
    if isinstance(workflow.output, FromNode):
        consumed.add(workflow.output.node)
    issues.extend(
        WiringIssue(
            kind=IssueType.DEAD_END_NODE,
            where=node_id,
            detail=(
                f"nothing reads {node_id!r} and it is not the workflow output. Wire it into a "
                f"node that uses it, make it the output, or drop it"
            ),
        )
        for node_id in nodes
        if node_id not in consumed
    )


def _check_rejected_to(gate: PersonGate | JudgeGate | OrchestratorGate, nodes: dict[str, Node], issues: list[WiringIssue]) -> None:
    if gate.rejected_to not in nodes:
        issues.append(
            WiringIssue(kind=IssueType.BAD_REJECTED_TO, where=gate.id, detail=f"rejected_to points at unknown node {gate.rejected_to!r}")
        )
        return
    if gate.rejected_to not in _ancestors(gate.id, nodes):
        issues.append(
            WiringIssue(
                kind=IssueType.BAD_REJECTED_TO,
                where=gate.id,
                detail=f"rejected_to {gate.rejected_to!r} is not upstream of the gate. A rejection must rewind the gate's own history",
            )
        )


def _producers(node: Node) -> list[str]:
    return list(dict.fromkeys(wire.node for _, wire in wires_of(node) if isinstance(wire, FromNode)))


@dataclass(frozen=True)
class _Reach:
    """What each node reaches upstream by two measures, each including the node, sound only on an acyclic graph."""

    ancestry: Mapping[str, frozenset[str]]
    """Node id -> itself and every node upstream of it by any path."""
    dominators: Mapping[str, frozenset[str]]
    """Node id -> itself and the upstream nodes EVERY path from a root to it passes through."""


def _reach(nodes: dict[str, Node]) -> _Reach:
    """Walk the wire graph producers-first, accumulating both measures in one pass."""
    ancestry: dict[str, frozenset[str]] = {}
    dominators: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in ancestry or node_id in visiting:
            return
        visiting.add(node_id)
        producers = [p for p in _producers(nodes[node_id]) if p in nodes]
        for producer in producers:
            visit(producer)
        upstream = [p for p in producers if p in ancestry]
        ancestry[node_id] = frozenset({node_id}).union(*(ancestry[p] for p in upstream)) if upstream else frozenset({node_id})
        dominators[node_id] = (
            frozenset({node_id}) | frozenset.intersection(*(dominators[p] for p in upstream)) if upstream else frozenset({node_id})
        )
        visiting.discard(node_id)

    for node_id in nodes:
        visit(node_id)
    return _Reach(ancestry=ancestry, dominators=dominators)


def _waits(nodes: dict[str, Node]) -> list[tuple[str, list[str]]]:
    """Every node that waits on more than one existing producer, with those producers."""
    waits: list[tuple[str, list[str]]] = []
    for node in nodes.values():
        producers = [producer for producer in _producers(node) if producer in nodes]
        if len(producers) > 1:
            waits.append((node.id, producers))
    return waits


def _check_rewinds_into_joins(nodes: dict[str, Node], reach: _Reach, issues: list[WiringIssue]) -> None:
    """Report, on the gate, a rejection that would re-run only some of the producers a node waits on."""
    waits = _waits(nodes)
    for gate in nodes.values():
        if not isinstance(gate, PersonGate | JudgeGate | OrchestratorGate) or gate.rejected_to not in nodes:
            continue  # a target naming no node is already reported by `_check_rejected_to`
        target = gate.rejected_to
        stalled_waits: list[tuple[str, list[str]]] = []
        for node_id, producers in waits:
            if node_id not in reach.ancestry[gate.id]:
                continue  # a wait this gate does not sit behind is never re-entered by its rejection
            if not any(target in reach.ancestry[producer] for producer in producers):
                continue  # the rewind lands below the wait, so the wait is never re-entered either
            stalled = [producer for producer in producers if target not in reach.dominators[producer]]
            if stalled:
                stalled_waits.append((node_id, stalled))
        if not stalled_waits:
            continue
        clauses = ". ".join(f"{node_id!r} would still be waiting on {_names(stalled)}" for node_id, stalled in stalled_waits)
        issues.append(
            WiringIssue(
                kind=IssueType.PARTIAL_REWIND,
                where=f"{gate.id}.rejected_to",
                detail=(
                    f"a rejection at gate {gate.id!r} rewinds to {target!r}, and {clauses}. A rewind must re-run every "
                    f"branch a waiting node reads, so point rejected_to at a node that every path to those producers "
                    f"passes through."
                ),
            )
        )


def _names(ids: Sequence[str]) -> str:
    """Node ids as a refusal reads them."""
    return ", ".join(repr(node_id) for node_id in ids)


def _ancestors(node_id: str, nodes: dict[str, Node]) -> set[str]:
    seen: set[str] = set()
    stack = _producers(nodes[node_id])
    while stack:
        current = stack.pop()
        if current in seen or current not in nodes:
            continue
        seen.add(current)
        stack.extend(_producers(nodes[current]))
    return seen


def _find_cycle(nodes: dict[str, Node]) -> list[str] | None:
    """The first wire cycle as a path of node ids, or None, never counting `rejected_to`, which is not a wire."""
    white, grey = set(nodes), set()

    def visit(node_id: str, path: list[str]) -> list[str] | None:
        white.discard(node_id)
        grey.add(node_id)
        for producer in _producers(nodes[node_id]):
            if producer in grey:
                return [*path, node_id, producer]
            if producer in white:
                found = visit(producer, [*path, node_id])
                if found is not None:
                    return found
        grey.discard(node_id)
        return None

    while white:
        found = visit(next(iter(white)), [])
        if found is not None:
            return found
    return None


def _assignable(actual: object, expected: object) -> bool:
    if actual is expected or actual == expected:  # `==` because `list[X]` is rebuilt, never identical
        return True
    actual_origin, expected_origin = get_origin(actual), get_origin(expected)
    if actual_origin is not None or expected_origin is not None:
        # issubclass raises on parameterised types, so compare by origin/args instead.
        if actual_origin is not expected_origin:
            return False
        actual_args, expected_args = get_args(actual), get_args(expected)
        return len(actual_args) == len(expected_args) and all(_assignable(a, e) for a, e in zip(actual_args, expected_args, strict=True))
    if not isinstance(actual, type) or not isinstance(expected, type):
        return False
    try:
        return issubclass(actual, expected)
    except TypeError:
        return False


def _type_name(tp: object) -> str:
    """A type's name as an author reads it, with the arguments of a parameterised type."""
    origin = get_origin(tp)
    if origin is None:
        return getattr(tp, "__name__", str(tp))
    inner = ", ".join(_type_name(arg) for arg in get_args(tp)) or "..."
    return f"{_type_name(origin)}[{inner}]"
