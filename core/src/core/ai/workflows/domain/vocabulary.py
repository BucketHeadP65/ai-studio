"""The closed set of models a workflow is written in: its nodes, wires, parameters, result and inline steps.

A workflow is typed data, validated as a whole and then built into a pydantic_graph graph.
Computation lives in steps, never in the workflow. Types come from the registry of steps,
except the shapes an inline step declares. Every model forbids extra keys, so a misspelled
or invented key fails validation. There are five node types, `run`, `map`, `gate`, `judge`
and `orchestrator-gate`, and none for retrying, streaming or a Python `BaseNode`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    PositiveInt,
    SerializerFunctionWrapHandler,
    StringConstraints,
    Tag,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue

from core.ai.workflows.domain.parameter_values import check_parameter_schema


def _discriminator_is_required(schema: JsonSchemaValue) -> None:
    """Mark the discriminator `kind` required in the JSON Schema, as parsing JSON or YAML demands it."""
    required = schema.setdefault("required", [])
    if isinstance(required, list) and "kind" not in required:
        required.append("kind")


Tier = Literal["orchestrator", "worker", "helper"]
"""Which of the host's three named model tiers performs a step."""

VERDICTS: tuple[str, ...] = ("approve", "reject", "cannot_judge")
Verdict = Literal["approve", "reject", "cannot_judge"]
"""What a judge answers: release the value, send it back with feedback, or hand it to a person."""


class Budget(BaseModel):
    """What one performance of a step may spend, stated to the model before it starts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: Annotated[int, Field(ge=1)]
    """Model requests, the count the run refuses past."""
    tool_calls: Annotated[int, Field(ge=1)] | None = None
    """Tool calls across the performance. None leaves them uncounted."""


NodeId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=100)]
"""Lower-snake node identifier, unique within one workflow."""

StepName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=100)]
"""The name of the step a node runs."""


def _branch(value: object, by_key: Mapping[str, str]) -> str | None:
    """The tag of the union member whose key alone is present in a payload, or None when none or several are."""
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        return None
    present = [tag for key, tag in by_key.items() if key in value]
    return present[0] if len(present) == 1 else None


class FromNode(BaseModel):
    """A wire from another node's output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: NodeId
    turn: StepName | None = None
    """For a wire from a multi-turn map: which turn's collected outputs travel, instead of the
    last turn's. Refused on any source that is not a map declaring this step in its `turns`."""

    @model_serializer(mode="wrap")
    def _omit_turn_when_unnamed(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Drop `turn` from the dump when it is None, which keeps a saved workflow's `spec_identity` unchanged."""
        data = handler(self)
        if self.turn is None:
            data.pop("turn", None)
        return data


class FromParameter(BaseModel):
    """A wire from one of the workflow's typed input parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parameter: str


def _wire_kind(value: object) -> str | None:
    """Which branch of `Wire` this is, read off the key that names the source."""
    return _branch(value, {"node": "from_node", "parameter": "from_parameter"})


Wire = Annotated[
    Annotated[FromNode, Tag("from_node")] | Annotated[FromParameter, Tag("from_parameter")],
    Discriminator(
        _wire_kind,
        custom_error_type="wire_not_recognised",
        custom_error_message="a wire is {'node': <node id>} or {'parameter': <parameter name>}",
    ),
]
"""Where a value comes from: another node's output or a workflow parameter.

Wires are the only data flow a workflow has. The branch is read from which key is present,
`node` or `parameter`, so a malformed wire gets one error message."""


class ParameterSpec(BaseModel):
    """One workflow input, typed by a JSON Schema.

    `json_schema` is what pydantic emits for the type, such as `TypeAdapter(FiscalQuarter).json_schema()`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: Annotated[str, StringConstraints(min_length=1)]
    json_schema: JsonSchemaValue
    required: bool = True

    @model_validator(mode="after")
    def _its_schema_can_admit_a_value(self) -> ParameterSpec:
        """Refuse a schema that is not a JSON Schema or that reaches outside itself."""
        check_parameter_schema(self.json_schema)
        return self


class RunNode(BaseModel):
    """Run one registered step.

    The step may be an agent or a plain function, and the workflow does not say which.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_extra=_discriminator_is_required)

    kind: Literal["run"] = "run"
    id: NodeId
    work: StepName
    inputs: Mapping[str, Wire] = Field(default_factory=dict)
    """One wire per step parameter name. Validation checks each wire's type against the
    registered step's signature."""
    tier: Tier | None = None
    """Which model tier performs this. None means the step's own tier, else the host's default."""


MaxGateAttempts = Annotated[int, Field(ge=1)]
"""How many times one gate may be reached in a run, its first review included."""

_DEFAULT_GATE_ATTEMPTS = 3


class _Gate(BaseModel):
    """What every gate carries whoever decides it: how many reaches it gets before it gives up."""

    max_attempts: MaxGateAttempts = _DEFAULT_GATE_ATTEMPTS
    """Total reaches, the first review included. A rejection below the ceiling reruns
    `rejected_to` with the refused value and the repair. The rejection that spends the ceiling
    ends the run instead, carrying that same evidence out with it."""

    @model_serializer(mode="wrap")
    def _omit_the_default_ceiling(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Drop `max_attempts` from the dump when it is the default."""
        data = handler(self)
        if self.max_attempts == _DEFAULT_GATE_ATTEMPTS:
            data.pop("max_attempts", None)
        return data


class PersonGate(_Gate):
    """A gate a person decides.

    Approval is recorded, never inferred. A rejection routes back to `rejected_to`, the only
    cycle a workflow may have.

    A node that must not run unapproved wires from `FromNode(node=<gate id>)`, never from
    the gate's source directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_extra=_discriminator_is_required)

    kind: Literal["gate"] = "gate"
    id: NodeId
    reviews: Wire
    """What the approver reviews, and what passes through on approval."""
    rejected_to: NodeId


class MapNode(BaseModel):
    """Run one worker once per item of a collection, and collect the results.

    The per-item work is the sequence of registered steps in `turns`. The node's output is the
    last turn's `list[<step output>]`, one entry per item, and a node wires from it as from a
    `run` node.

    The steps' other inputs go in `inputs` and are resolved once, shared by every item. Only
    `each` varies. `max_parallel` bounds how many items run at once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_extra=_discriminator_is_required)

    kind: Literal["map"] = "map"
    id: NodeId
    turns: Annotated[tuple[StepName, ...], Field(min_length=1)]
    """The ordered steps this map performs per item, one worker taking one turn per entry.

    With one name, the map runs that step once per item. Two or more become turns of one
    conversation: the same worker holds the item across the sequence, each turn opened by that
    step's instructions as a user message, and the map collects the last turn's outputs. Every
    turn's own outputs stay in the run's values under `<node id>.<step name>`, and a wire can
    name one with `FromNode.turn`."""
    over: Wire
    """A wire producing the collection: a list, tuple, set or sequence of items assignable to the
    parameter `each` names. Validation checks the item type, so mapping a `list[RunEvidence]`
    into a step wanting a `str` is refused before the run."""
    each: str
    """Which of the step's parameters receives one item."""
    inputs: Mapping[str, Wire] = Field(default_factory=dict)
    """Wires for the steps' other parameters, resolved once and shared by every item."""
    max_parallel: int | None = None
    """How many items may run at once. `None` means no bound. Validation refuses anything below 1."""
    on_error: Literal["fail", "skip"] = "fail"
    """What one item's failure does to the map's collected list, for every turn of the item alike.

    `fail` (default): the first item that raises fails the run, same as an unmapped node.
    `skip`: that item is left out of the collected list and the run carries on with the rest."""
    abort_after_repeats: int | None = 3
    """How many consecutive items may fail with the same error class before the run aborts.

    The run aborts whatever `on_error` says. Failures are counted in completion order, and any
    item that succeeds resets the count. `None` disables the ceiling."""
    tier: Tier | None = None
    """Which model tier performs this. None means the step's own tier, else the host's default."""


class JudgeGate(_Gate):
    """A gate a registered step decides, never a person.

    The step may be an agent, a schema check or arithmetic. On approval the gate passes its
    reviewed value through. A rejection routes back to `rejected_to`, re-running that node and
    everything after it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_extra=_discriminator_is_required)

    kind: Literal["judge"] = "judge"
    id: NodeId
    work: StepName
    """The deciding step. It takes the reviewed value as its one required input and returns
    either a `bool` or a model whose `approved_when` field carries the verdict."""
    inputs: Mapping[str, Wire] = Field(default_factory=dict)
    """Wires beside the reviewed value, the parameter a search needs and the like. The one
    required input these leave uncovered receives the reviewed value."""
    reviews: Wire
    """What is judged, and what passes through on approval."""
    approved_when: str | None = None
    """The field on the step's output that carries the verdict, or `None` when the step returns a
    bare `bool`.

    The field holds a `bool`, or the closed set `approve`, `reject`, `cannot_judge`.
    `cannot_judge` hands the value to a person.

    It is a field name, not an expression. Validation checks that the field exists and holds a
    verdict type, so a misspelled field is refused before the run."""
    rejected_to: NodeId


class OrchestratorGate(_Gate):
    """A gate the host's orchestrator decides: the session agent the run already has, reading the gate's `charge`.

    Its decision is machine evidence, never a person's approval. This gate is how a workflow
    runs unattended. The workflow writes the brief and the host supplies the decider. A run with
    no orchestrator refuses the workflow before it starts. On approval the gate passes its
    reviewed value through, and a rejection routes back to `rejected_to` with the decider's
    feedback, re-running that node and everything after it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", json_schema_extra=_discriminator_is_required)

    kind: Literal["orchestrator-gate"] = "orchestrator-gate"
    id: NodeId
    charge: Annotated[str, StringConstraints(min_length=40)]
    """The review brief, in full: what to look for and what would fail. It is the whole
    instruction the decider works from. A charge under 40 characters is refused when the
    workflow is parsed."""
    reviews: Wire
    """What is reviewed, and what passes through on approval."""
    rejected_to: NodeId


Node = Annotated[RunNode | PersonGate | MapNode | JudgeGate | OrchestratorGate, Field(discriminator="kind")]


class RequiredGateCheck(BaseModel):
    """One gate a caller requires recorded acceptance from before using this result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: NodeId
    accepted_sources: tuple[Literal["judge", "person", "orchestrator"], ...]


class ResultArtifactSpec(BaseModel):
    """One selectable value from a workflow's completed graph."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NodeId
    label: Annotated[str, StringConstraints(min_length=1)]
    source: Wire
    field: str | None = None
    media_type: Literal["text/markdown", "application/json", "text/plain"]

    @model_serializer(mode="wrap")
    def _omit_field_when_unselected(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Keep whole-value artifact declarations identical when they select no field."""
        data = handler(self)
        if self.field is None:
            data.pop("field", None)
        return data


class WorkflowResultSpec(BaseModel):
    """The declared result a caller may select after a workflow completes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: Annotated[str, StringConstraints(min_length=1)]
    primary_work: NodeId
    empty_primary: Literal["noop", "unknown"] = "unknown"
    required_gates: tuple[RequiredGateCheck, ...] = ()
    artifacts: tuple[ResultArtifactSpec, ...] = ()


def referenced_steps(workflow: Workflow) -> tuple[str, ...]:
    """Every step the workflow's nodes run, in order of first appearance.

    Args:
        workflow: The workflow to read.

    Returns:
        The step names, each once.
    """
    seen: set[str] = set()
    work: list[str] = []
    for node in workflow.nodes:
        names: tuple[str, ...]
        if isinstance(node, RunNode | JudgeGate):
            names = (node.work,)
        elif isinstance(node, MapNode):
            names = node.turns
        else:
            names = ()
        for name in names:
            if name not in seen:
                seen.add(name)
                work.append(name)
    return tuple(work)


class SameAs(BaseModel):
    """A slot that carries the type another step produces, the step `like` names.

    Type assignability is nominal (`issubclass`), so an inline step names the type it consumes
    this way. Re-describing the same shape builds a different class, which validation refuses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    like: StepName
    """A step name: a step this run registered, or another inline step of this workflow."""
    element: bool = False
    """Carry one element of the list `like` produces, the inverse of `many`. A map's per-item
    step names its item's type this way. Refused when `like` does not produce a list."""
    many: bool = False
    """Carry a list of what `like` produces rather than one of them.

    A step wired after a `map` names the map's collected list this way."""

    @model_validator(mode="after")
    def _one_hat(self) -> SameAs:
        if self.element and self.many:
            msg = f"element and many together name the list {self.like!r} already produces. Use {{like: {self.like!r}}} alone"
            raise ValueError(msg)
        return self


class Shape(BaseModel):
    """A new type declared as JSON Schema, which only an inline step may declare.

    A shape may be an object with `properties`, or a scalar such as `{"type": "string"}`. A slot
    fed from a workflow parameter is usually a scalar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    json_schema: JsonSchemaValue


def _slot_kind(value: object) -> str | None:
    """Which branch of `Slot` this is: a shape of its own, or a reference to another step's."""
    return _branch(value, {"json_schema": "shape", "like": "same_as"})


Slot = Annotated[
    Annotated[Shape, Tag("shape")] | Annotated[SameAs, Tag("same_as")],
    Discriminator(
        _slot_kind,
        custom_error_type="slot_not_recognised",
        custom_error_message="a slot is {'json_schema': <schema>} to declare a shape or {'like': <step name>} to reuse one",
    ),
]
"""One typed slot of an inline step: a shape of its own, or another step's type.

The branch is read from which key is present, `json_schema` or `like`, so a malformed slot
gets one error message."""


class Briefing(BaseModel):
    """An inline step written as instructions, typed slots and a typed result.

    It is data, prose and JSON Schema with no code, so any run that can perform a briefing can
    run it. `tools` are checked against what the run offers, and a briefing may not take the
    name of a registered step.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StepName
    instructions: Annotated[str, StringConstraints(min_length=40)]
    """The briefing, in full. Instructions under 40 characters are refused when the workflow is
    parsed."""
    inputs: Mapping[str, Slot] = Field(default_factory=dict)
    output: Slot
    tools: tuple[str, ...] = ()
    """Tools this step may use while it works. Empty means it reasons over its inputs only."""
    tier: Tier | None = None
    """Which model tier performs this. None means the step's own tier, else the host's default."""
    budget: Budget | None = None
    """What this step may spend. None runs on the host's default."""
    skills: tuple[str, ...] = ()
    """Skills this step holds while it works, by name. The host resolves them."""


class Program(BaseModel):
    """An inline step written as a program: source, typed slots and a typed result.

    Use it for deterministic glue such as dedupe, fuse, filter, truncate or reshape, which then
    costs no model call. `tools` are checked against what the run offers, and a program may not
    take the name of a registered step. A workflow may invent a computation, never a database,
    a credential or a tool. A program runs in a sandbox, never `exec`, and a registry with no
    runner refuses it with a reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StepName
    source: Annotated[str, StringConstraints(min_length=1)]
    """The program, as Python. Its last line is an expression, whose value is the step's result.
    An assignment as the last line returns nothing. Source that does not parse is reported beside
    the wiring issues."""
    inputs: Mapping[str, Slot] = Field(default_factory=dict)
    """One slot per input name. Each input is bound as a plain global before the program runs,
    so the program reads `dense`, not a field of an argument object."""
    output: Slot
    tools: tuple[str, ...] = ()
    """Tools this program may call, as functions, from inside the sandbox. Empty is the ordinary
    case and the valuable one: a program that reaches nothing is pure computation."""


class AgentStep(BaseModel):
    """An inline step written as a whole declared agent, pydantic-ai's AgentSpec kept verbatim.

    `inputs`, `output` and `tools` are the workflow's wiring, and the agent document is validated
    with pydantic-ai's own model. `agent` is the document inline, or a string naming an agent
    registered elsewhere. A host with no agent registry refuses a string, and a host with no
    agent runner refuses the step. At build the host overrides the model, derives the output type
    from `output`, grants `tools` by name and refuses capabilities it has not granted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StepName
    agent: Mapping[str, JsonValue] | str
    inputs: Mapping[str, Slot] = Field(default_factory=dict)
    output: Slot
    tools: tuple[str, ...] = ()


def _inline_step_kind(value: object) -> str | None:
    """The tag of the inline step type whose key alone is present, or None when none or several are."""
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        return None
    present = [tag for key, tag in (("agent", "worker"), ("source", "code"), ("instructions", "task")) if key in value]
    return present[0] if len(present) == 1 else None


InlineStep = Annotated[
    Annotated[Program, Tag("code")] | Annotated[Briefing, Tag("task")] | Annotated[AgentStep, Tag("worker")],
    Discriminator(
        _inline_step_kind,
        custom_error_type="step_not_recognised",
        custom_error_message="an inline step carries 'instructions' (a briefing), 'source' (a program) or 'agent' "
        "(an agent step): exactly one",
    ),
]
"""A step written inside the workflow itself: a briefing, a program or an agent step.

The type is read from which key is present, `instructions`, `source` or `agent`, and exactly
one must be."""


class Workflow(BaseModel):
    """One workflow, whole.

    Execution order comes from wires, never from list position. A workflow is meaningful only
    once validated against a registry of steps.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=100)]
    version: PositiveInt
    description: Annotated[str, StringConstraints(min_length=1)]
    parameters: Mapping[str, ParameterSpec] = Field(default_factory=dict)
    inline: tuple[InlineStep, ...] = ()
    """Steps written inside this workflow, for a step the host does not register.

    Empty is the ordinary case. Prefer a registered step to a new one.

    Each is a briefing (`Briefing`), a program (`Program`) for deterministic glue, or a whole
    declared agent (`AgentStep`) for reusing one built elsewhere.
    """
    nodes: tuple[Node, ...]
    output: Wire
    """Which value is the workflow's result."""
    result: WorkflowResultSpec | None = None
    """The caller-facing declaration for selected completed work, when this workflow has one."""

    @model_serializer(mode="wrap")
    def _omit_absent_result(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """Preserve saved workflow serialisation when no result is declared."""
        data = handler(self)
        if self.result is None:
            data.pop("result", None)
        return data
