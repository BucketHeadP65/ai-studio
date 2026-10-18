"""Inline steps built into registered steps, and a builder that writes a workflow in code.

A workflow may carry three types of inline step: a briefing (`Briefing`) a model performs, a
program (`Program`) run in a sandbox, or an agent step (`AgentStep`) declared as an `AgentSpec`.
This module builds each into an ordinary step registered through `StepRegistry.register`, so
nothing downstream can tell which type it was.

`WorkflowBuilder` writes a `Workflow` through the five node verbs instead of hand-written YAML. It
runs nothing. The workflow it returns is validated and run like any other.
"""

from __future__ import annotations

import ast
import inspect
import re

from functools import partial
from typing import TYPE_CHECKING, Any, Literal, assert_never, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model
from pydantic_ai import AgentSpec
from pydantic_core import SchemaError

from core.ai.workflows.domain.task import AgentTask
from core.ai.workflows.domain.validator import judge_verdict_problem, wires_of
from core.ai.workflows.domain.vocabulary import (
    AgentStep,
    Briefing,
    FromNode,
    JudgeGate,
    MapNode,
    Node,
    OrchestratorGate,
    ParameterSpec,
    PersonGate,
    Program,
    RunNode,
    SameAs,
    Shape,
    Slot,
    Tier,
    Wire,
    Workflow,
)
from core.ai.workflows.exceptions import RegistrationError, TaskShapeError, WiringRefusedError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from pydantic.json_schema import JsonSchemaValue

    from core.ai.workflows.domain.vocabulary import InlineStep
    from core.ai.workflows.internal_contracts import AgentResolver, AgentRunner, CodeRunner
    from core.ai.workflows.registry import StepRegistry

Problem = tuple[str, str]
"""Where the trouble is, and what it is. The validator turns each one into a `WiringIssue`."""

_SCALARS: Mapping[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}
_UNSUPPORTED = ("$ref", "anyOf", "oneOf", "allOf", "not", "additionalProperties", "patternProperties")

_NUMERIC = frozenset({"integer", "number"})
_CONSTRAINTS: Mapping[str, tuple[str, frozenset[str]]] = {
    "minimum": ("ge", _NUMERIC),
    "maximum": ("le", _NUMERIC),
    "exclusiveMinimum": ("gt", _NUMERIC),
    "exclusiveMaximum": ("lt", _NUMERIC),
    "minLength": ("min_length", frozenset({"string"})),
    "maxLength": ("max_length", frozenset({"string"})),
    "pattern": ("pattern", frozenset({"string"})),
    "minItems": ("min_length", frozenset({"array"})),
    "maxItems": ("max_length", frozenset({"array"})),
}
"""JSON Schema keywords carried onto the built field: what pydantic calls each, and which
declared types it may be written on.

A keyword on any other type is refused before the run. pydantic would accept it when the model is
created, such as `Field(pattern=...)` on an `int`, and fail only on the first value.
"""


def _pascal(text: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", text)
    return "".join(part[:1].upper() + part[1:] for part in parts if part) or "Shape"


def _annotation(schema: object, *, name: str, where: str) -> Any:
    """One field's Python annotation built from its JSON Schema, or a refusal naming what to fix."""
    if not isinstance(schema, dict):
        raise TaskShapeError([(where, "expected a JSON Schema object here")])
    for unsupported in _UNSUPPORTED:
        if unsupported in schema:
            raise TaskShapeError(
                [
                    (
                        where,
                        (
                            f"`{unsupported}` is not supported. Write one concrete shape inline, or use "
                            f'`{{"like": "<step name>"}}` to reuse a step\'s own type'
                        ),
                    )
                ]
            )
    if "enum" in schema:
        values = schema["enum"]
        if not (isinstance(values, list) and values and all(isinstance(value, str) for value in values)):
            raise TaskShapeError([(where, "`enum` is supported only as a non-empty list of strings")])
        return Literal[tuple(values)]  # type: ignore[misc]  # a runtime-built Literal is still a Literal
    declared = schema.get("type")
    if isinstance(declared, str) and declared in _SCALARS:
        return _SCALARS[declared]
    if declared == "array":
        items = schema.get("items")
        if items is None:
            raise TaskShapeError([(where, "an array must say what its `items` are")])
        return list[_annotation(items, name=f"{name}Item", where=f"{where}[]")]  # type: ignore[misc]
    if declared == "object":
        return build_type(schema, name=name, where=where)
    raise _not_a_shape(schema, where=where)


def _not_a_shape(schema: JsonSchemaValue, *, where: str) -> TaskShapeError:
    """The refusal for a shape whose `type` is missing or unsupported, naming what the shape has instead."""
    declared = schema.get("type")
    if declared is not None:
        return TaskShapeError([(where, f"unsupported type {declared!r}. Use string, integer, number, boolean, array, or object")])
    present = ", ".join(schema) or "nothing"
    beside = ". `type: object` goes beside `properties`, with `required` and `title`, never inside it" if "properties" in schema else ""
    return TaskShapeError([(where, f"no `type` here (this shape has: {present}). Say string, integer, number, boolean, array, or object{beside}")])


def _limits_of(field_schema: object, *, where: str) -> dict[str, Any]:
    """The constraints this field declared, refusing any that cannot hold on its type."""
    if not isinstance(field_schema, dict):
        return {}
    declared = "string" if "enum" in field_schema else field_schema.get("type")
    limits: dict[str, Any] = {}
    for keyword, (argument, applies_to) in _CONSTRAINTS.items():
        if keyword not in field_schema:
            continue
        if declared not in applies_to:
            meaningful = " or ".join(sorted(applies_to))
            raise TaskShapeError(
                [(where, f"`{keyword}` cannot apply to a field of type {declared!r}. It is only meaningful on {meaningful}")]
            )
        limits[argument] = field_schema[keyword]
    return limits


def build_type(schema: JsonSchemaValue, *, name: str, where: str) -> type[BaseModel]:
    """A pydantic model built from a declared JSON Schema object, or a refusal that says what to fix.

    The model refuses a value carrying an undeclared field. A field outside `required` defaults
    to `None`. Constraints in `_CONSTRAINTS` are read at the field level only, so a bound inside
    an array's `items` or on a bare scalar slot is not applied.

    Args:
        schema: The shape, a JSON Schema object with at least one entry in `properties`.
        name: The model's name when the shape has no `title`, and the prefix of its nested models' names.
        where: Where the shape sits in the workflow, named in every refusal.

    Returns:
        The model class, its docstring taken from the shape's `description`.

    Raises:
        TaskShapeError: When the shape is not an object, declares no field, or cannot be built as a type.
    """
    if schema.get("type") != "object":
        raise TaskShapeError([(where, 'a step\'s shape must be a JSON Schema object: {"type": "object", "properties": {...}}')])
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise TaskShapeError([(where, "an object shape needs at least one entry in `properties`")])
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()

    fields: dict[str, Any] = {}
    for field_name, field_schema in properties.items():
        if not (isinstance(field_name, str) and field_name.isidentifier()):
            raise TaskShapeError([(where, f"{field_name!r} cannot be a field name. Use a plain identifier")])
        annotation = _annotation(field_schema, name=f"{name}{_pascal(field_name)}", where=f"{where}.{field_name}")
        description = field_schema.get("description") if isinstance(field_schema, dict) else None
        limits = _limits_of(field_schema, where=f"{where}.{field_name}")
        if field_name in required_names:
            fields[field_name] = (annotation, Field(description=description, **limits))
        else:
            fields[field_name] = (annotation | None, Field(default=None, description=description, **limits))

    title = schema.get("title")
    model_name = _pascal(title if isinstance(title, str) and title else name)
    try:
        model = create_model(model_name, __config__=ConfigDict(extra="forbid"), **fields)  # type: ignore[call-overload]
    except (TypeError, ValueError, SchemaError) as error:
        # Reached by a constraint pydantic cannot apply to the type it was written on, e.g. a
        # `pattern` on an integer.
        raise TaskShapeError([(where, f"this shape cannot be built as a type: {error}")]) from error
    description = schema.get("description")
    model.__doc__ = description if isinstance(description, str) and description else f"A shape declared by {name!r}."
    return model


_SANDBOX_MODULES = frozenset({"asyncio", "datetime", "json", "math", "os", "pathlib", "re", "sys", "typing", "unicodedata"})
"""The standard library modules an inline program may import, the sandbox's own list.

No third-party module may be imported. An import outside this list is refused before the run.
"""


def _imported(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    """Each import in one statement: the name as written, and its top-level module, empty when relative."""
    if isinstance(node, ast.Import):
        return [(alias.name, alias.name.split(".")[0]) for alias in node.names]
    written = "." * node.level + (node.module or "")
    return [(written, "" if node.level else written.split(".")[0])]


def _program_docstring(source: str, *, where: str) -> str | None:
    """Check an inline program before it runs, and return its module docstring or None."""
    try:
        module = ast.parse(source)
    except SyntaxError as error:
        detail = f"this source does not parse as Python: {error.msg} (line {error.lineno})"
        raise TaskShapeError([(where, detail)]) from error

    problems: list[Problem] = []
    importable = ", ".join(sorted(_SANDBOX_MODULES))
    for node in ast.walk(module):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            problems.append((where, "`import *` is not supported in the sandbox. Import the names this program uses"))
            continue
        problems.extend(
            (where, f"the sandbox has no {written!r}. A brought program may import only: {importable}")
            for written, root in _imported(node)
            if root not in _SANDBOX_MODULES
        )

    if not module.body:
        problems.append((where, "this program is empty, so it produces no value for the step to return"))
    elif not isinstance(module.body[-1], ast.Expr):
        problems.append(
            (
                where,
                (
                    "the last line has to be an expression, because its value is what this step returns: "
                    "a program ending in an assignment runs correctly and hands back nothing"
                ),
            )
        )
    elif (
        isinstance(final := module.body[-1].value, ast.Name)
        and isinstance(final.ctx, ast.Load)
        and any(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == final.id for node in module.body)
    ):
        # A bare name is an expression too, so it passes the check above. When it names a
        # function this program defines, the step returns the function object, not its result.
        problems.append(
            (
                where,
                (
                    f"the last line is {final.id!r}, the name of a function this program defines. The step would return "
                    f"the function object, not its result. Do the work at module level and end with the result expression "
                    f"itself (or at least call it: `{final.id}()`)"
                ),
            )
        )
    if problems:
        raise TaskShapeError(problems)
    return ast.get_docstring(module)


def _code_step(
    step: Program, execute: CodeRunner, *, inputs: Mapping[str, type], output: type, description: str
) -> Callable[..., Awaitable[object]]:
    """An inline program as an ordinary async step, with real annotations the registry reads."""
    types = dict(inputs)

    async def run(**kwargs: object) -> object:
        return await execute(step, output, **kwargs)

    run.__name__ = step.name
    run.__qualname__ = step.name
    run.__doc__ = description
    run.__annotations__ = {**types, "return": output}
    run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation) for name, annotation in types.items()],
        return_annotation=output,
    )
    return run


_TEMPLATE = re.compile(r"\{\{.+?\}\}")
"""A Handlebars-style span, refused in an agent step's instructions.

pydantic-ai turns an `instructions` string carrying one into a `TemplateStr` rendered against
`deps`, and no deps schema is derived for an agent step, so the span would reach the model
unrendered. Match it against the raw document, never the validated `AgentSpec`. Validation turns
a matching string into a `TemplateStr` this pattern can no longer see."""

_HOST_DERIVED = ("output_schema", "deps_schema")
"""`AgentSpec` keys the host derives from an agent step's own `inputs` and `output`, so a workflow
may not declare them."""

_HOST_RETAINED = ("model_settings", "retries", "tool_timeout", "end_strategy", "metadata")
"""`AgentSpec` keys that configure the host's run rather than describe the agent step, so a
workflow may not declare them.

`Agent.from_spec` merges the document's value under the host's, so an unset host value would let
the document's value stand. `model_settings` can carry `extra_headers` and `extra_body`, which ride
the HTTP request the host's credentials sign."""


def _is_templated(instructions: object) -> bool:
    """Whether any string in a raw `instructions` value, one string or a list of them, carries a `{{...}}` span."""
    candidates = instructions if isinstance(instructions, list) else [instructions]
    return any(isinstance(candidate, str) and _TEMPLATE.search(candidate) for candidate in candidates)


def _worker_document(step: AgentStep, *, grants: Sequence[str], where: str, resolve_agent: AgentResolver | None) -> str:
    """Check an agent step's document, resolving a stored agent by name first, and return its description."""
    if isinstance(step.agent, str):
        if resolve_agent is None:
            msg = f"this run cannot resolve a stored agent by name. {step.agent!r} was referenced, but no agent registry is wired here"
            raise TaskShapeError([(where, msg)])
        try:
            document = resolve_agent(step.agent)
        except LookupError as error:
            raise TaskShapeError([(where, str(error))]) from error
    else:
        document = step.agent

    problems: list[Problem] = [
        (where, f"{key!r} is derived by the host from the step's wiring, so the workflow may not declare it")
        for key in _HOST_DERIVED
        if key in document
    ]
    problems.extend(
        (where, f"{key!r} configures the host's run, and the host retains it, so the workflow may not declare it")
        for key in _HOST_RETAINED
        if key in document
    )
    if _is_templated(document.get("instructions")):
        problems.append((where, "templated instructions need a deps schema the host does not derive yet"))

    try:
        agent_spec = AgentSpec.model_validate(dict(document))
    except ValidationError as error:
        problems.append((where, f"this document does not validate as pydantic-ai's AgentSpec: {error}"))
        raise TaskShapeError(problems) from error

    problems.extend(
        (where, f"capability {capability.name!r} is not granted by this host. Capabilities are granted by name, like tools")
        for capability in agent_spec.capabilities
        if capability.name not in grants
    )
    if problems:
        raise TaskShapeError(problems)

    description = agent_spec.description
    if isinstance(description, str) and description:
        return description
    return f"Runs the {step.name!r} agent this workflow brought."


def _worker_step(
    step: AgentStep, execute: AgentRunner, *, inputs: Mapping[str, type], output: type, description: str
) -> Callable[..., Awaitable[object]]:
    """An agent step as an ordinary async step, with real annotations the registry reads."""
    types = dict(inputs)

    async def run(**kwargs: object) -> object:
        return await execute(step, output, **kwargs)

    run.__name__ = step.name
    run.__qualname__ = step.name
    run.__doc__ = description
    run.__annotations__ = {**types, "return": output}
    run.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation) for name, annotation in types.items()],
        return_annotation=output,
    )
    return run


def including(registry: StepRegistry, workflow: Workflow) -> tuple[StepRegistry, tuple[Problem, ...]]:
    """The registry plus the workflow's inline steps, built on the registry's own terms. Reached as `StepRegistry.including`.

    It returns problems rather than raising them. An inline step that fails to build is left out
    of the returned registry, so the validator also reports every node that names it.

    Args:
        registry: The registry to extend. What it can run and the tools it grants decide what builds.
        workflow: The workflow whose `inline` steps are built.

    Returns:
        The extended registry, and one `(where, what)` problem for each reason a step did not build.
    """
    if not workflow.inline:
        return registry, ()

    extended = registry.copy()
    pending, problems = _admitted(registry, workflow)

    # Types are nominal, so a `like` reference must yield the same class the referenced step
    # produces. Build in dependency order and reuse what came out.
    known: dict[str, type] = {name: impl.output_type for name in registry.names if (impl := registry.get(name)) is not None}
    while pending:
        progressed = False
        for name, task in list(pending.items()):
            slots = (*task.inputs.values(), task.output)
            references = [slot.like for slot in slots if isinstance(slot, SameAs)]
            if any(reference in pending for reference in references):
                continue  # a sibling inline step has to be built first
            if missing := sorted({reference for reference in references if reference not in known}):
                problems.append((f"inline[{name}]", f"`like` names no step that exists here: {', '.join(missing)}"))
            elif (output := _built(extended, task, known, problems)) is not None:
                known[name] = output
            del pending[name]
            progressed = True
        if not progressed:
            detail = "its `like` references form a cycle. A step cannot depend on its own type"
            problems.extend((f"inline[{name}]", detail) for name in sorted(pending))
            break
    return extended, tuple(problems)


def _unrunnable(registry: StepRegistry, step: InlineStep) -> str | None:
    """Why this run cannot build this type of inline step at all, or None when it can."""
    match step:
        case Program() if not registry.runs_programs:
            return "this run cannot execute a program a workflow brought. It has no sandbox, and brought source is never run outside one"
        case Briefing() if not registry.performs_briefings:
            return "this run cannot perform a briefing. It can only run workflows that name registered steps"
        case AgentStep() if not registry.runs_agents:
            return (
                "this run cannot run a worker a workflow brought. It has no agent runner, and a brought agent is never run outside one"
            )
        case _:
            return None


def _admitted(registry: StepRegistry, workflow: Workflow) -> tuple[dict[str, InlineStep], list[Problem]]:
    """The inline steps eligible to build: runnable here, uniquely named, shadowing nothing, asking only for offered tools."""
    problems: list[Problem] = []
    pending: dict[str, InlineStep] = {}
    for task in workflow.inline:
        where = f"inline[{task.name}]"
        if (unrunnable := _unrunnable(registry, task)) is not None:
            problems.append((where, unrunnable))
        elif registry.get(task.name) is not None:
            problems.append(
                (where, f"{task.name!r} is already a step this app registered. A workflow may add a step, never replace one")
            )
        elif task.name in pending:
            problems.append((where, f"two inline steps are both named {task.name!r}"))
        elif unknown := [tool for tool in task.tools if tool not in registry.grants]:
            offered = ", ".join(registry.grants) or "none"
            problems.append((where, f"asks for tools this run does not offer: {', '.join(unknown)} (offered: {offered})"))
        else:
            pending[task.name] = task
    return pending, problems


def _built(extended: StepRegistry, task: InlineStep, known: Mapping[str, type], problems: list[Problem]) -> type | None:
    """Build and register one inline step and return its output type, or record the failure and return None."""
    name = task.name
    try:
        inputs = {
            slot_name: _resolve(slot, known, name=f"{name}_{slot_name}", where=f"inline[{name}].inputs[{slot_name}]")
            for slot_name, slot in task.inputs.items()
        }
        output = _resolve(task.output, known, name=name, where=f"inline[{name}].output")
        match task:
            case Briefing() if (perform := extended.perform) is not None:
                extended.register(
                    AgentTask(name=name, instructions=task.instructions, runner=partial(perform, task, output), tool_names=task.tools).step(
                        inputs=inputs, output=output
                    )
                )
            case Program() if (execute := extended.run_code) is not None:
                documented = _program_docstring(task.source, where=f"inline[{name}].source")
                description = documented or f"Runs the {name!r} program this workflow brought."
                extended.register(
                    _code_step(task, execute, inputs=inputs, output=output, description=description), replay_safe=not task.tools
                )
            case AgentStep() if (execute := extended.run_agent) is not None:
                description = _worker_document(
                    task, grants=extended.capability_grants, where=f"inline[{name}].agent", resolve_agent=extended.resolve_agent
                )
                extended.register(_worker_step(task, execute, inputs=inputs, output=output, description=description))
            case _:  # pragma: no cover (`_admitted` withholds any step whose runner this run lacks)
                return None
    except TaskShapeError as error:
        problems.extend(error.problems)
        return None
    except (RegistrationError, ValueError) as error:
        problems.append((f"inline[{name}]", str(error)))
        return None
    return output


def _resolve(slot: Slot, known: Mapping[str, type], *, name: str, where: str) -> Any:
    """One slot's Python type, which may be a plain scalar as well as an object."""
    match slot:
        case SameAs(like=like, many=many, element=element):
            produced = known[like]  # existence was checked before we got here
            if element:
                if get_origin(produced) is not list:
                    kind = getattr(produced, "__name__", str(produced))
                    raise TaskShapeError([(where, f"`element: true` needs {like!r} to produce a list, and it produces {kind}")])
                (produced,) = get_args(produced)
            return list[produced] if many else produced  # type: ignore[valid-type]
        case Shape(json_schema=schema):
            return _annotation(schema, name=name, where=where)
        case _ as unreachable:
            assert_never(unreachable)


class WorkflowBuilder:
    """A builder that writes a `Workflow` in code: its parameters, its nodes and one output wire.

    Each call refuses the problem it introduced: a duplicate id, a wire to a node not added yet,
    an output wire to nowhere, or a judge whose step's return shape cannot serve as a verdict.
    Every other check (types, dead ends, cycles, unregistered steps) is left to `validate` on the
    workflow `build()` returns. The builder runs nothing.
    """

    def __init__(self, name: str, version: int, description: str, *, registry: StepRegistry | None = None) -> None:
        """Start a workflow, optionally checked against `registry` as nodes are added.

        Args:
            name: `Workflow.name`.
            version: `Workflow.version`.
            description: `Workflow.description`.
            registry: When given, `judge` checks the named step's return shape at call time, the
                way `validate` eventually would. Without one, that one check waits for a
                downstream `validate` call against the run's actual registry.
        """
        self._name = name
        self._version = version
        self._description = description
        self._registry = registry
        self._parameters: dict[str, ParameterSpec] = {}
        self._nodes: dict[str, Node] = {}
        self._output: Wire | None = None

    def parameter(self, name: str, spec: ParameterSpec) -> WorkflowBuilder:
        """Declare one workflow input.

        Args:
            name: The parameter's name, unique in this workflow.
            spec: What the parameter accepts.

        Returns:
            This builder, for the next call.

        Raises:
            WiringRefusedError: When `name` is already declared.
        """
        if name in self._parameters:
            raise WiringRefusedError([(name, f"parameter {name!r} is already declared")])
        self._parameters[name] = spec
        return self

    def run(self, node_id: str, *, work: str, inputs: Mapping[str, Wire] | None = None, tier: Tier | None = None) -> WorkflowBuilder:
        """Add a node that runs one step.

        Args:
            node_id: The node's id, unique in this workflow.
            work: The name of the step the node runs.
            inputs: The step's inputs by parameter name, each wired from a workflow parameter or a node.
            tier: The model tier that performs the step. None uses the step's own tier, else the host's default.

        Returns:
            This builder, for the next call.

        Raises:
            WiringRefusedError: When the id is already used, or an input is wired from a node not added yet.
        """
        return self._place(RunNode(id=node_id, work=work, inputs=dict(inputs or {}), tier=tier))

    def map(
        self,
        node_id: str,
        *,
        turns: Sequence[str],
        over: Wire,
        each: str,
        inputs: Mapping[str, Wire] | None = None,
        max_parallel: int | None = None,
        on_error: Literal["fail", "skip"] = "fail",
        abort_after_repeats: int | None = 3,
        tier: Tier | None = None,
    ) -> WorkflowBuilder:
        """Add a node that runs its steps once per item of a collection and collects the results.

        Args:
            node_id: The node's id, unique in this workflow.
            turns: The steps each item runs through in order, as turns of one conversation.
            over: The wire producing the collection.
            each: The steps' parameter that receives one item.
            inputs: The steps' other inputs, resolved once and shared by every item.
            max_parallel: How many items run at once. None leaves it unbounded.
            on_error: What one item's failure does. `fail` fails the run, `skip` leaves the item out and carries on.
            abort_after_repeats: How many consecutive items may fail the same way before the run
                aborts, whatever `on_error` says. None removes the limit.
            tier: The model tier that performs every item and every turn. None uses the step's own
                tier, else the host's default.

        Returns:
            This builder, for the next call.

        Raises:
            WiringRefusedError: When the id is already used, or a wire names a node not added yet.
        """
        return self._place(
            MapNode(
                id=node_id,
                turns=tuple(turns),
                over=over,
                each=each,
                inputs=dict(inputs or {}),
                max_parallel=max_parallel,
                on_error=on_error,
                abort_after_repeats=abort_after_repeats,
                tier=tier,
            )
        )

    def gate(self, node_id: str, *, reviews: Wire, rejected_to: str) -> WorkflowBuilder:
        """Add a gate a person decides before the run goes on.

        Args:
            node_id: The node's id, unique in this workflow.
            reviews: The wire carrying what the person reviews, which passes through on approval.
            rejected_to: The node a rejection sends the run back to.

        Returns:
            This builder, for the next call.

        Raises:
            WiringRefusedError: When the id is already used, or `reviews` names a node not added yet.
        """
        return self._place(PersonGate(id=node_id, reviews=reviews, rejected_to=rejected_to))

    def judge(self, node_id: str, *, work: str, reviews: Wire, rejected_to: str, approved_when: str | None = None) -> WorkflowBuilder:
        """Add a gate a registered step decides.

        Args:
            node_id: The node's id, unique in this workflow.
            work: The name of the step that decides.
            reviews: The wire carrying what is judged, which passes through on approval.
            rejected_to: The node a rejection sends the run back to.
            approved_when: The field of the step's output that carries the verdict, or None when
                the step returns a bare `bool`.

        Returns:
            This builder, for the next call.

        Raises:
            WiringRefusedError: When the id is already used, `reviews` names a node not added yet,
                or, when this builder holds a registry, the step's return shape cannot serve as a verdict.
        """
        node = JudgeGate(id=node_id, work=work, reviews=reviews, rejected_to=rejected_to, approved_when=approved_when)
        extra: list[Problem] = []
        impl = self._registry.get(work) if self._registry is not None else None
        if impl is not None:
            detail = judge_verdict_problem(impl.output_type, approved_when, work=work)
            if detail is not None:
                extra.append((node_id, detail))
        return self._place(node, extra)

    def orchestrator(self, node_id: str, *, charge: str, reviews: Wire, rejected_to: str) -> WorkflowBuilder:
        """Add a gate the run's own orchestrator decides.

        Args:
            node_id: The node's id, unique in this workflow.
            charge: The brief the orchestrator decides from. It has the same minimum length as a
                briefing's instructions.
            reviews: The wire carrying what is reviewed, which passes through on approval.
            rejected_to: The node a rejection sends the run back to.

        Returns:
            This builder, for the next call.

        Raises:
            pydantic.ValidationError: When `charge` is shorter than the minimum length.
            WiringRefusedError: When the id is already used, or `reviews` names a node not added yet.
        """
        return self._place(OrchestratorGate(id=node_id, charge=charge, reviews=reviews, rejected_to=rejected_to))

    def output(self, wire: Wire) -> WorkflowBuilder:
        """Set which value is the workflow's result.

        Args:
            wire: The wire carrying the result.

        Returns:
            This builder, for the next call.

        Raises:
            WiringRefusedError: When `wire` names a node not added yet.
        """
        if isinstance(wire, FromNode) and wire.node not in self._nodes:
            raise WiringRefusedError([("output", f"no node named {wire.node!r}. Add it before wiring it as the output")])
        self._output = wire
        return self

    def build(self) -> Workflow:
        """Return the `Workflow` built so far.

        Returns:
            The workflow. `validate` has not checked it yet.

        Raises:
            WiringRefusedError: When no output was wired.
        """
        if self._output is None:
            raise WiringRefusedError([("output", "no output wired. Call .output(...) before .build()")])
        return Workflow(
            name=self._name,
            version=self._version,
            description=self._description,
            parameters=dict(self._parameters),
            nodes=tuple(self._nodes.values()),
            output=self._output,
        )

    def _place(self, node: Node, extra: Sequence[Problem] = ()) -> WorkflowBuilder:
        """Add one node, refusing a duplicate id, a wire to a node not added yet, or the caller's own findings."""
        problems: list[Problem] = []
        if node.id in self._nodes:
            problems.append((node.id, f"node id {node.id!r} appears more than once"))
        problems.extend(self._unknown_node_refs(node))
        problems.extend(extra)
        if problems:
            raise WiringRefusedError(problems)
        self._nodes[node.id] = node
        return self

    def _unknown_node_refs(self, node: Node) -> list[Problem]:
        return [
            (f"{node.id}.{slot}", f"no node named {wire.node!r}. Add it earlier in the chain before wiring to it")
            for slot, wire in wires_of(node)
            if isinstance(wire, FromNode) and wire.node not in self._nodes
        ]
