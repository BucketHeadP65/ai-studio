"""Turning workflows and step contracts into what a person reads: signatures, graphs, prose.

Every function here is pure. It takes a workflow, a registry of steps and tool names, and returns
text or a JSON-ready dict.
A workflow carries no types: a node names its step, and the registry holds the step's signature.
The types drawn here come from the same introspection the validator checks against, so they match
what runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    JudgeGate,
    MapNode,
    Node,
    OrchestratorGate,
    PersonGate,
    Prediction,
    PredictionOutcome,
    Program,
    RunNode,
    StepRegistry,
    TaskContract,
    Wire,
    Workflow,
    contract_of,
)
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from core.ai.workflows.contracts import InlineStep, ParameterSpec
    from pydantic.json_schema import JsonSchemaValue


def render_for_review(value: object, *, limit: int | None = 4_000) -> str:
    """Show the actual value, not a summary somebody wrote about it.

    A caller writing the value out in full passes `limit=None` and never keeps a second renderer.

    Args:
        value: The value to show. A pydantic model is shown as indented JSON.
        limit: The most characters to keep. `None` keeps the whole text.

    Returns:
        The text, cut to `limit` characters.
    """
    text = value.model_dump_json(indent=2) if isinstance(value, BaseModel) else str(value)
    return text if limit is None else text[:limit]


def render_prediction(outcome: PredictionOutcome) -> str:
    """One graded claim as a person reads it: the verdict, the claim, then what the evidence log showed.

    A claim about a version nobody has run reads `UNMEASURABLE`, never `MISSED`.

    Args:
        outcome: The graded claim.

    Returns:
        The line.
    """
    verdict = "UNMEASURABLE" if outcome.holds is None else ("HOLDS" if outcome.holds else "MISSED")
    return f"{verdict}, {_claimed(outcome.prediction)}: {outcome.observed}"


def _claimed(prediction: Prediction) -> str:
    """The claim itself as a sentence, read off the closed vocabulary rather than out of prose."""
    if prediction.claim == "produces":
        return f"{prediction.subject} produces {prediction.shape or 'a value'}"
    if prediction.claim == "accepts":
        return f"a run is given {prediction.subject}"
    if prediction.claim == "approves":
        return f"{prediction.subject} releases its value"
    return "a run of this version completes"


def prediction_payload(outcomes: Sequence[PredictionOutcome]) -> list[dict[str, Any]]:
    """Graded claims as the browser draws them: the claim as data, plus the sentence to show.

    The sentence under `reads` is the one `render_prediction` gives.

    Args:
        outcomes: The graded claims.

    Returns:
        One dict per claim, in the order given.
    """
    return [
        {
            "claim": outcome.prediction.claim,
            "subject": outcome.prediction.subject,
            "shape": outcome.prediction.shape,
            "holds": outcome.holds,
            "observed": outcome.observed,
            "reads": render_prediction(outcome),
        }
        for outcome in outcomes
    ]


def _wires_of(node: Node) -> list[Wire]:
    """Every wire into a node, whatever type of node it is."""
    if node.kind == "run":
        return list(node.inputs.values())
    if node.kind == "map":
        return [node.over, *node.inputs.values()]
    return [node.reviews]


def parallel_groups(workflow: Workflow) -> str:
    """Describe which nodes have no dependency on each other, that is, what will fork.

    Args:
        workflow: The workflow.

    Returns:
        The ids of the nodes no other node feeds, joined by commas, or `nothing (fully sequential)`
        when there is only one.
    """
    producers = {node.id: {wire.node for wire in _wires_of(node) if isinstance(wire, FromNode)} for node in workflow.nodes}
    roots = [node_id for node_id, upstream in producers.items() if not upstream]
    return ", ".join(roots) if len(roots) > 1 else "nothing (fully sequential)"


def type_name(schema: JsonSchemaValue) -> str:
    """A short readable name for a JSON Schema: the model's title, or its primitive type.

    A `$ref` is named by its last segment, which is the referenced model's title.

    Args:
        schema: The JSON Schema.

    Returns:
        The name, `list[...]` for an array, and `object` when the schema states no type.
    """
    title = schema.get("title")
    if isinstance(title, str) and title:
        return title
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference:
        return reference.rsplit("/", 1)[-1]
    kind = schema.get("type")
    if kind == "array":
        items = schema.get("items")
        inner = type_name(items) if isinstance(items, dict) else "any"
        return f"list[{inner}]"
    return str(kind or "object")


def signature_of(contract: TaskContract) -> str:
    """One step as a person reads it: `name(arg: Type, ...) → Output`.

    Args:
        contract: The step's contract.

    Returns:
        The signature line.
    """
    args = ", ".join(f"{arg}: {type_name(schema)}" for arg, schema in contract.inputs.items())
    return f"{contract.name}({args}) → {type_name(contract.output)}"


def graph_payload(workflow: Workflow, registry: StepRegistry, tools_by_step: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """The workflow as the UI draws it, with the types joined back in.

    Every wire is resolved to the type flowing along it and every node to the step's signature,
    plus the JSON Schemas behind that signature (`input_schemas`, `output_schema`) and, for a
    program the workflow carries, its `source`.
    Pass the registry `prepare` returned, not the bare one. Only that registry holds the workflow's
    inline steps, and a node running one is flagged `brought`.

    Args:
        workflow: The workflow.
        registry: The registry of steps `prepare` returned for this workflow.
        tools_by_step: The tool names each registered step's agent holds, by step name.

    Returns:
        The workflow's `name`, `version`, `nodes` and `edges`, ready for JSON.
    """
    output_key = workflow.output.node if isinstance(workflow.output, FromNode) else None
    output_parameter = workflow.output.parameter if isinstance(workflow.output, FromParameter) else None
    brought = {task.name: task for task in workflow.inline}
    nodes: list[dict[str, Any]] = [
        _param_entry(name, parameter, is_output=name == output_parameter) for name, parameter in workflow.parameters.items()
    ]
    edges: list[dict[str, str]] = []

    for node in workflow.nodes:
        if isinstance(node, RunNode):
            drawn, wires = _run_entry(workflow, registry, node, task=brought.get(node.work), tools_by_step=tools_by_step)
        elif isinstance(node, MapNode):
            drawn, wires = _map_entry(workflow, registry, node, brought=brought, tools_by_step=tools_by_step)
        else:
            drawn, wires = _gate_entry(workflow, registry, node, tools_by_step=tools_by_step)
        drawn["is_output"] = node.id == output_key
        nodes.append(drawn)
        edges.extend(wires)

    return {"name": workflow.name, "version": workflow.version, "nodes": nodes, "edges": edges}


def _param_entry(name: str, parameter: ParameterSpec, *, is_output: bool = False) -> dict[str, Any]:
    """A declared parameter as a drawable node, `is_output` marking the one a run hands back as its result."""
    return {
        "key": f"param:{name}",
        "id": name,
        "kind": "param",
        "signature": f"{type_name(parameter.json_schema)}{'' if parameter.required else ' (optional)'}",
        "required": parameter.required,
        "detail": parameter.description,
        "tools": [],
        "is_output": is_output,
        "unknown": False,
        "brought": False,
        "briefing": "",
        "work": "",
        "input_schemas": {},
        "output_schema": dict(parameter.json_schema),
        "source": "",
    }


def _wire_key(wire: FromNode | FromParameter) -> str:
    """The drawable id a wire starts from: a node's own, or a parameter's prefixed one."""
    return wire.node if isinstance(wire, FromNode) else f"param:{wire.parameter}"


def map_turn_of(source: MapNode, wire: FromNode) -> str:
    """Which of a map's turns a wire from it names: the one `wire.turn` says, or else its last turn.

    The indexer and the validator resolve a wire the same way.

    Args:
        source: The map node the wire starts from.
        wire: The wire.

    Returns:
        The name of the step that turn runs.
    """
    return wire.turn if wire.turn is not None else source.turns[-1]


def _flowing_type(workflow: Workflow, registry: StepRegistry, wire: FromNode | FromParameter) -> str:
    """What travels on this wire: a step's output type, a gate's pass-through, or a parameter."""
    if isinstance(wire, FromParameter):
        parameter = workflow.parameters.get(wire.parameter)
        return type_name(parameter.json_schema) if parameter else "?"
    source = next((n for n in workflow.nodes if n.id == wire.node), None)
    if source is None:
        return "?"
    # Only `isinstance` narrows the union here. A test of `kind` would not.
    if isinstance(source, PersonGate | JudgeGate | OrchestratorGate):
        return _flowing_type(workflow, registry, source.reviews)
    work = map_turn_of(source, wire) if isinstance(source, MapNode) else source.work
    implementation = registry.get(work)
    if implementation is None:
        return "?"
    produced = type_name(contract_of(implementation).output)
    # A map's value is a list of what the named turn produces.
    return f"list[{produced}]" if source.kind == "map" else produced


def _list_of(item: JsonSchemaValue) -> JsonSchemaValue:
    """A JSON Schema for a list of `item`, with the item's own `$defs` hoisted to the root so its references resolve."""
    inner = dict(item)
    defs = inner.pop("$defs", None)
    schema: JsonSchemaValue = {"type": "array", "items": inner}
    if isinstance(defs, dict):
        schema["$defs"] = defs
    return schema


def _flowing_schema(workflow: Workflow, registry: StepRegistry, wire: FromNode | FromParameter) -> JsonSchemaValue | None:
    """The JSON Schema of what travels on this wire, or `None` when its source cannot be resolved here."""
    if isinstance(wire, FromParameter):
        parameter = workflow.parameters.get(wire.parameter)
        return dict(parameter.json_schema) if parameter else None
    source = next((n for n in workflow.nodes if n.id == wire.node), None)
    if source is None:
        return None
    if isinstance(source, PersonGate | JudgeGate | OrchestratorGate):
        return _flowing_schema(workflow, registry, source.reviews)
    if isinstance(source, MapNode):
        implementation = registry.get(map_turn_of(source, wire))
        return _list_of(dict(contract_of(implementation).output)) if implementation else None
    implementation = registry.get(source.work)
    return dict(contract_of(implementation).output) if implementation else None


def _held_tools(work: str, task: InlineStep | None, tools_by_step: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """The tools a step's box lists: an inline step's own, or those this app built a registered step's agent with."""
    return tuple(task.tools) if task is not None else tuple(tools_by_step.get(work, ()))


def _turn_contract(
    registry: StepRegistry, work: str, task: InlineStep | None, tools_by_step: Mapping[str, Sequence[str]]
) -> TaskContract | None:
    """One map turn's contract, or `None` when this run has no step by that name."""
    implementation = registry.get(work)
    return contract_of(implementation, tools=_held_tools(work, task, tools_by_step)) if implementation else None


def _run_entry(
    workflow: Workflow, registry: StepRegistry, node: RunNode, *, task: InlineStep | None, tools_by_step: Mapping[str, Sequence[str]]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """One `run` node and its input wires, with the step's signature joined back in."""
    implementation = registry.get(node.work)
    contract = contract_of(implementation, tools=_held_tools(node.work, task, tools_by_step)) if implementation else None
    drawn: dict[str, Any] = {
        "key": node.id,
        "id": node.id,
        "kind": "run",
        "signature": signature_of(contract) if contract else f"{node.work}, not something this app can do",
        "detail": contract.description if contract else f"This app has no step called {node.work!r}.",
        "tools": list(contract.tools) if contract else [],
        "unknown": contract is None,
        "brought": task is not None,
        # Only a briefing has instructions. A program's own text is under `source` below.
        "briefing": task.instructions if isinstance(task, Briefing) else "",
        # The step name, which differs from the node id when a workflow runs one step under two ids.
        "work": node.work,
        # The schemas behind the signature, so a reader can see a result's fields, not only its name.
        "input_schemas": dict(contract.inputs) if contract else {},
        "output_schema": dict(contract.output) if contract else None,
        # A program's own text. A briefing or a registered step has none.
        "source": task.source if isinstance(task, Program) else "",
    }
    edges = [
        {"from": _wire_key(wire), "to": node.id, "kind": "wire", "label": f"{arg}: {_flowing_type(workflow, registry, wire)}"}
        for arg, wire in node.inputs.items()
    ]
    return drawn, edges


def _map_entry(
    workflow: Workflow,
    registry: StepRegistry,
    node: MapNode,
    *,
    brought: Mapping[str, InlineStep],
    tools_by_step: Mapping[str, Sequence[str]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """One `map` node, its fan-out edge and its input wires, with each turn's own piece under `turns`."""
    entries = [(work, brought.get(work), _turn_contract(registry, work, brought.get(work), tools_by_step)) for work in node.turns]
    first_work, first_task, first_contract = entries[0]
    last_contract = entries[-1][2]
    # At runtime every turn holds the union of all turns' tools, so the map's own `tools` lists that union.
    union_tools = list(dict.fromkeys(tool for _, _, contract in entries if contract is not None for tool in contract.tools))
    unknown_work = next((work for work, _, contract in entries if contract is None), None)
    chain = " then ".join(node.turns)
    each_item = type_name(first_contract.output) if first_contract else "?"
    # `on_error` is stated in the `detail` sentence and has no payload key of its own.
    skip_clause = ", and an item that fails is skipped while the rest carry on" if node.on_error == "skip" else ""

    if unknown_work is not None and len(node.turns) == 1:
        signature = f"{unknown_work}, not something this app can do"
        detail = f"This app has no step called {unknown_work!r}."
    elif unknown_work is not None:
        signature = f"{chain} once per item, not something this app can do"
        detail = f"Runs {first_work!r} once per item, but this app has no step called {unknown_work!r}."
    else:
        last_item = type_name(last_contract.output) if last_contract else "?"
        chained_clause = "".join(f", then runs {work!r}" for work in node.turns[1:])
        signature = f"{chain} once per item · {_flowing_type(workflow, registry, node.over)} → list[{last_item}]"
        detail = (
            f"Runs {first_work!r} once for every item of {node.over}, feeding each one as {node.each!r}{chained_clause}, "
            f"and collects the results as they finish, not in item order{skip_clause}. An empty collection is fine: "
            "nothing runs and the list is empty."
        )

    # A single turn keeps the map's own id, like a `run` node. With two or more turns, every turn,
    # the first included, is under its own dotted id, the id the indexer records and reports it under.
    turns_payload = [
        {
            "work": work,
            "node_id": node.id if len(node.turns) == 1 else f"{node.id}.{work}",
            "briefing": task.instructions if isinstance(task, Briefing) else "",
            "tools": list(contract.tools) if contract else [],
            "output_schema": dict(contract.output) if contract else None,
            "source": task.source if isinstance(task, Program) else "",
        }
        for work, task, contract in entries
    ]
    drawn: dict[str, Any] = {
        "key": node.id,
        "id": node.id,
        "kind": "map",
        "signature": signature,
        "detail": detail,
        "tools": union_tools,
        "unknown": unknown_work is not None,
        "brought": first_task is not None,
        # Only a briefing has instructions. A program's own text is under `source` below.
        "briefing": first_task.instructions if isinstance(first_task, Briefing) else "",
        # The step name, which differs from the node id when a workflow runs one step under two ids.
        "work": first_work,
        "input_schemas": dict(first_contract.inputs) if first_contract else {},
        # The collected list is the LAST turn's output, which is what flows on downstream.
        "output_schema": _list_of(last_contract.output) if last_contract is not None and unknown_work is None else None,
        "source": first_task.source if isinstance(first_task, Program) else "",
        "turns": turns_payload,
    }
    edges = [{"from": _wire_key(node.over), "to": node.id, "kind": "map", "label": f"each {node.each}: {each_item}"}]
    edges.extend(
        {"from": _wire_key(wire), "to": node.id, "kind": "wire", "label": f"{arg}: {_flowing_type(workflow, registry, wire)}"}
        for arg, wire in node.inputs.items()
    )
    return drawn, edges


def _gate_entry(
    workflow: Workflow,
    registry: StepRegistry,
    node: PersonGate | JudgeGate | OrchestratorGate,
    *,
    tools_by_step: Mapping[str, Sequence[str]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """One gate, whoever decides it, with what it reviews and where a rejection reruns from."""
    flowing = _flowing_type(workflow, registry, node.reviews)
    work = node.work if isinstance(node, JudgeGate) else ""
    if isinstance(node, JudgeGate):
        signature = f"{work} decides · {flowing} passes through"
        detail = (
            f"A step decides, not a person, {work!r}"
            f"{f' reading its {node.approved_when!r} field' if node.approved_when else ''}. "
            f"Rejected → {node.rejected_to}, which reruns from there."
        )
        briefing = ""
    elif isinstance(node, OrchestratorGate):
        signature = f"this run's orchestrator decides · {flowing} passes through"
        detail = (
            f"The orchestrator decides, not a person, against the charge below. Rejected → {node.rejected_to}, which reruns from there."
        )
        briefing = node.charge
    else:
        signature = f"a human approves · {flowing} passes through"
        detail = f"rejected → {node.rejected_to}, which reruns from there"
        briefing = ""
    drawn: dict[str, Any] = {
        "key": node.id,
        "id": node.id,
        "kind": node.kind,
        "signature": signature,
        "detail": detail,
        "tools": list(tools_by_step.get(work, ())) if work else [],
        "unknown": isinstance(node, JudgeGate) and registry.get(work) is None,
        "brought": False,
        "briefing": briefing,
        "work": work,
        # A gate produces nothing of its own: what passes through is what it reviewed.
        "input_schemas": {},
        "output_schema": _flowing_schema(workflow, registry, node.reviews),
        "source": "",
    }
    edges = [
        {
            "from": _wire_key(node.reviews),
            "to": node.id,
            "kind": "wire",
            "label": f"reviews: {_flowing_type(workflow, registry, node.reviews)}",
        },
        {"from": node.id, "to": node.rejected_to, "kind": "rejected", "label": "rejected"},
    ]
    return drawn, edges
