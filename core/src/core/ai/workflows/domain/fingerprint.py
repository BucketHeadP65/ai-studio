"""The environment a workflow was saved against, stamped so a later fetch can report what moved.

Every stamp is read off the registry at save time and read again at fetch time. Nothing is declared by hand.

Four things are stamped: the introspected contract of every registered step the workflow names, the agent
document each string `agent` reference of an inline agent step resolves to, the name of each tool the workflow's
inline steps ask for that the registry grants, and whatever stamps the host adds. An inline step and an inline
agent document travel inside the workflow and are not stamped. A tool the registry grants and no inline step asks
for is not stamped, so granting one more tool moves no stamp.

`spec_identity` hashes the workflow itself rather than its environment, and evidence is filed under that hash.
`step_content_address` hashes one reusable step the same way, and `canonical_step_yaml` writes the document it hashes
as YAML.
"""

from __future__ import annotations

import hashlib
import json

from typing import TYPE_CHECKING

import yaml

from pydantic import BaseModel, ConfigDict, TypeAdapter

from core.ai.workflows.domain.vocabulary import AgentStep, referenced_steps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

    from core.ai.workflows.domain.vocabulary import InlineStep, Workflow
    from core.ai.workflows.registry import StepImplementation, StepRegistry


class WorkflowFingerprint(BaseModel):
    """The stamps, serializable so any library can keep them beside the workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    steps: dict[str, str] = {}
    """A hash of the introspected contract of every registered step the workflow names, by step name.

    An inline step is absent. Empty is a valid stamp for a workflow whose steps are all inline, and it
    is a different claim from a missing stamp."""
    agents: dict[str, str] = {}
    """A hash of the document each string `agent` reference resolved to, by the name of the inline
    `AgentStep` that holds the reference.

    An `AgentStep` whose `agent` is an inline document is absent. A string reference names a document in
    a library that can change, and re-authoring or re-activating that name shows up as drift."""
    grants: tuple[str, ...] = ()
    """The name of each tool the workflow's inline steps ask for that the registry grants, sorted, each once.

    Empty is a workflow whose steps ask for no tool. A stamp kept without this key holds it empty."""
    environment: dict[str, str] = {}
    """Stamps the host adds, such as capability versions. They are compared as opaque strings: equal is
    fresh, and a changed or missing stamp is drift."""


def fingerprint(workflow: Workflow, registry: StepRegistry, *, environment: Mapping[str, str] | None = None) -> WorkflowFingerprint:
    """Stamp the environment this workflow would run against, right now.

    A string agent reference the registry cannot resolve now (deactivated, renamed, never authored) is
    left out of `agents` rather than raised, exactly like a step that is no longer registered. A tool an
    inline step asks for that the registry does not grant now is left out of `grants` the same way.

    Args:
        workflow: The workflow to stamp.
        registry: The registry of steps its steps and agent references are read from.
        environment: The host's own stamps, kept as given. `None` adds none.

    Returns:
        The fingerprint.

    Raises:
        LookupError: An inline `AgentStep` names its agent by string and `registry` has no
            `resolve_agent` at all.
    """
    inline_names = {task.name for task in workflow.inline}
    named = sorted(set(referenced_steps(workflow)) - inline_names)
    steps = {name: _step_hash(impl) for name in named if (impl := registry.get(name)) is not None}
    string_refs = {task.name: task.agent for task in workflow.inline if isinstance(task, AgentStep) and isinstance(task.agent, str)}
    agents = {task_name: hashed for task_name, ref in string_refs.items() if (hashed := _agent_hash(ref, registry)) is not None}
    grants = tuple(sorted({tool for task in workflow.inline for tool in task.tools} & set(registry.grants)))
    return WorkflowFingerprint(steps=steps, agents=agents, grants=grants, environment=dict(environment or {}))


def _stale(recorded: Mapping[str, str], current: Mapping[str, str], *, missing: str, changed: str) -> list[str]:
    """Every name in `recorded` that `current` lacks or hashes differently, worded with `missing` or `changed`."""
    findings: list[str] = []
    for name, stamp in recorded.items():
        if name not in current:
            findings.append(missing.format(name=name))
        elif current[name] != stamp:
            findings.append(changed.format(name=name))
    return findings


def drift(recorded: WorkflowFingerprint, current: WorkflowFingerprint) -> tuple[str, ...]:
    """Everything that moved between the environment a workflow was saved in and this one.

    Only what `recorded` holds is compared. A step, tool or stamp that only `current` has is not drift.

    Args:
        recorded: The fingerprint saved with the workflow.
        current: The fingerprint of the environment now.

    Returns:
        One sentence per change, empty when nothing moved.
    """
    findings: list[str] = [
        *_stale(
            recorded.steps, current.steps, missing="step {name!r} is no longer registered here",
            changed="step {name!r} changed its contract since this was saved",
        ),
        *_stale(
            recorded.agents, current.agents, missing="agent {name!r} is no longer resolvable here",
            changed="agent {name!r} changed its stored document since this was saved",
        ),
    ]
    findings.extend(
        f"tool {tool!r} that this workflow's steps ask for is no longer granted here" for tool in recorded.grants if tool not in current.grants
    )
    for key, value in recorded.environment.items():
        if key not in current.environment:
            findings.append(f"environment stamp {key!r} was {value!r} at save time. The environment now does not report it")
        elif current.environment[key] != value:
            findings.append(f"environment stamp {key!r} changed: {value!r} -> {current.environment[key]!r}")
    return tuple(findings)


def spec_identity(workflow: Workflow) -> str:
    """This workflow's content address: the SHA-256 of its canonical JSON.

    It stays the same when the workflow moves between libraries or gets a new id, and it changes when
    any value changes, `version` included. Two workflows share an identity exactly when they are equal
    as values, including the order of `nodes`, and then they share their evidence. The JSON is dumped
    with `mode="json"`, keys sorted and every field at its default left out at every depth, so a field
    written at its default, a field left out and a field added to the format later with a default all hash
    the same. Each node keeps its `kind`. A changed default moves the identity of a workflow whose document
    wrote the field at its old default or at the value that becomes the new default. A document that left the
    field out, or wrote any other value, keeps its identity. A value that is not JSON raises rather than being
    hashed as text.

    Args:
        workflow: The workflow to address.

    Returns:
        The digest as lowercase hex.
    """
    return hashlib.sha256(json.dumps(_canonical_document(workflow), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _canonical_document(workflow: Workflow) -> dict[str, JsonValue]:
    """The workflow as JSON data with every default left out at every depth and each node's `kind` kept."""
    document: dict[str, JsonValue] = workflow.model_dump(mode="json", exclude_defaults=True)
    document["nodes"] = [{**node.model_dump(mode="json", exclude_defaults=True), "kind": node.kind} for node in workflow.nodes]
    return document


def step_content_address(step: InlineStep) -> str:
    """A reusable step's content address: the SHA-256 of its canonical JSON.

    It follows the rule `spec_identity` follows. The JSON is dumped with `mode="json"`, keys sorted, no spaces
    and every field at its default left out at every depth, so a field written at its default and a field left
    out hash the same. Two copies of a step are the same step exactly when their content addresses match, and any
    other change of value moves it. A briefing, a program and an agent step never share one.

    Args:
        step: The step to address.

    Returns:
        The digest as lowercase hex.
    """
    return hashlib.sha256(json.dumps(_canonical_step(step), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def canonical_step_yaml(step: InlineStep) -> str:
    """A reusable step as canonical YAML: the document `step_content_address` hashes, keys sorted.

    Two steps with the same content address get the same text. A string holding a line break is written as a
    literal block, one line of text per line, when YAML allows one, so no tab and no line ending in a space. A line
    diff of two such steps names the lines that changed. Any other string with a line break is double-quoted, its
    line breaks escaped.

    Args:
        step: The step to write.

    Returns:
        The YAML text.
    """
    return yaml.dump(_canonical_step(step), Dumper=_CanonicalDumper, sort_keys=True, allow_unicode=True, width=1000)


def _canonical_step(step: InlineStep) -> dict[str, JsonValue]:
    """The step as JSON data with every default left out at every depth."""
    return step.model_dump(mode="json", exclude_defaults=True)


class _CanonicalDumper(yaml.SafeDumper):
    """The safe dumper, writing a string that holds a line break as a literal block."""


def _represent_text(dumper: yaml.SafeDumper, text: str) -> yaml.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", text, style="|" if "\n" in text else None)


_CanonicalDumper.add_representer(str, _represent_text)


def _agent_hash(name: str, registry: StepRegistry) -> str | None:
    """The hash of the document `name` resolves to now, or `None` when no stored agent answers to it."""
    if registry.resolve_agent is None:
        msg = f"this run cannot resolve a stored agent by name. {name!r} was referenced, but no agent registry is wired here"
        raise LookupError(msg)
    try:
        return _digest(registry.resolve_agent(name))
    except LookupError:
        return None


def _step_hash(impl: StepImplementation) -> str:
    """One step's contract, hashed: the name and JSON Schema of every input and of the output, and which inputs are required."""
    payload = {
        "inputs": {name: _type_stamp(annotation) for name, annotation in sorted(impl.input_types.items())},
        "required": sorted(impl.required),
        "output": _type_stamp(impl.output_type),
    }
    return _digest(payload)


def _type_stamp(annotation: type) -> dict[str, object]:
    """The name and JSON Schema of one annotation, with an empty schema when pydantic cannot build one."""
    try:
        schema: object = TypeAdapter(annotation).json_schema()
    except Exception:
        schema = {}
    return {"name": getattr(annotation, "__qualname__", str(annotation)), "schema": schema}


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
