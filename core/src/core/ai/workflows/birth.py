"""Checks that a workflow still does what the recorded run it came from did.

`check_birth` is free and structural: the workflow still validates, every node the run produced a
value for still exists, and each step still declares the type the run returned. `replay_birth`
spends a real run on the recorded inputs and compares what it produced, shape by shape.

Neither asks a model anything. Both compare shapes, never values, so two runs over a live
knowledge base may say different things and still match.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args, get_origin

from core.ai.workflows.domain.validator import prepare
from core.ai.workflows.domain.vocabulary import MapNode, RunNode
from core.ai.workflows.graph_builder import WorkflowState, build_graph

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.domain.evidence import RunEvidence
    from core.ai.workflows.domain.vocabulary import Node, Workflow
    from core.ai.workflows.internal_contracts import ApprovalHook
    from core.ai.workflows.registry import StepRegistry


def shapes_of(values: Mapping[str, object]) -> dict[str, str]:
    """A run's produced values as type names, the shapes a replay compares against.

    A list carries its element type, such as `list[str]`, when all its items share one.

    Args:
        values: The run's produced values by node id.

    Returns:
        Each node id with the type name of what it produced.
    """
    return {node_id: _shape(value) for node_id, value in values.items()}


def check_birth(workflow: Workflow, registry: StepRegistry, born: RunEvidence) -> tuple[str, ...]:
    """The free check: whether the workflow can still produce what the run it came from produced.

    Args:
        workflow: The workflow as it is now.
        registry: The steps this run can execute.
        born: The evidence of the run the workflow came from.

    Returns:
        One finding per difference, empty when nothing moved.
    """
    prepared = prepare(workflow, registry)
    if not prepared.report.ok:
        return tuple(f"the spec no longer validates here: [{i.kind}] {i.where}: {i.detail}" for i in prepared.report.issues)
    findings: list[str] = []
    by_id = {node.id: node for node in workflow.nodes}
    for node_id, shape in born.produced.items():
        known, declared = _declared_shape_of(node_id, by_id, prepared.registry)
        if not known:
            findings.append(f"node {node_id!r} produced {shape} at birth and is not in this spec any more")
        elif declared is not None and declared != shape:
            findings.append(f"node {node_id!r} produced {shape} at birth. The step behind it now declares {declared}")
    findings.extend(
        f"gate {gate!r} was approved at birth and is not in this spec any more"
        for gate in (*born.gates_approved, *born.gates_orchestrated)
        if gate not in by_id
    )
    return tuple(findings)


async def replay_birth(workflow: Workflow, registry: StepRegistry, born: RunEvidence, *, approval: ApprovalHook) -> tuple[str, ...]:
    """The expensive check: run the workflow again on the recorded inputs and compare the shapes.

    It costs a real run. Call it only when a person asked for it, never on the retrieval path.

    Args:
        workflow: The workflow as it is now.
        registry: The steps this run can execute.
        born: The evidence of the run the workflow came from, with its inputs and produced shapes.
        approval: Who decides the person gates the replay reaches.

    Returns:
        One finding per shape that differs, or the findings that stopped the replay. Empty when
        every shape matches.
    """
    if not born.params or not born.produced:
        return (f"cannot replay {born.workflow_id!r}: the recorded run has no inputs and no produced shapes",)
    if findings := check_birth(workflow, registry, born):
        return findings  # no point spending a run on something that cannot reproduce structurally
    state = WorkflowState(params=dict(born.params))
    try:
        await build_graph(workflow, registry, approval=approval).run(state=state)
    except Exception as exc:
        return (f"the replay did not finish: {type(exc).__name__}: {exc}",)
    now = shapes_of(state.values)
    return tuple(
        f"node {node_id!r} produced {shape} at birth and {now.get(node_id, 'nothing')} on replay"
        for node_id, shape in born.produced.items()
        if now.get(node_id) != shape
    )


def _shape(value: object) -> str:
    if isinstance(value, list):
        inner = {_shape(item) for item in value}
        return f"list[{inner.pop()}]" if len(inner) == 1 else "list"
    return type(value).__name__


def _declared_shape_of(node_id: str, by_id: Mapping[str, Node], registry: StepRegistry) -> tuple[bool, str | None]:
    """`(True, declared)` for a node id, or a `<map node id>.<turn>` key whose map still declares the turn, else `(False, None)`."""
    if node_id in by_id:
        return True, _declared_shape(by_id[node_id], registry)
    map_id, sep, turn = node_id.partition(".")
    map_node = by_id.get(map_id)
    if sep and isinstance(map_node, MapNode) and turn in map_node.turns:
        return True, _turn_collected_shape(turn, registry)
    return False, None


def _turn_collected_shape(work: str, registry: StepRegistry) -> str | None:
    """The declared shape of one turn's own collected list, kept under `<map node id>.<turn>`."""
    impl = registry.get(work)
    return f"list[{_annotation_shape(impl.output_type)}]" if impl is not None else None


def _declared_shape(node: Node, registry: StepRegistry) -> str | None:
    """The declared shape of what a node produces (for a map, its last turn's list), or None for a gate."""
    if isinstance(node, MapNode):
        return _turn_collected_shape(node.turns[-1], registry) if node.turns else None
    if isinstance(node, RunNode):
        impl = registry.get(node.work)
        return _annotation_shape(impl.output_type) if impl is not None else None
    return None


def _annotation_shape(annotation: object) -> str:
    """A declared type in the words `_shape` uses for a runtime value, keeping a list's element type."""
    if get_origin(annotation) is list and (args := get_args(annotation)):
        return f"list[{_annotation_shape(args[0])}]"
    return getattr(annotation, "__name__", str(annotation))
