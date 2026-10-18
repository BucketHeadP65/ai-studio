"""The custom agents on disk: pydantic-ai's own `AgentSpec` documents, written as YAML, each landing disabled until activated.

A document composes registered behavior only: model, instructions, settings and capability names with
their config. It is validated by `AgentSpec.model_validate` plus every capability name resolving
against the capability registry. `FileCustomAgents.resolve_active` is what a workflow step's agent
reference resolves through.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

import yaml

from core.files import atomic_write
from pydantic import BaseModel, ValidationError
from pydantic_ai import AgentSpec
from pydantic_ai.agent.spec import CapabilitySpec, get_capability_registry
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability, WrapperCapability
from pydantic_ai_harness.capability_creation import CapabilityValidationError, load_capability_instance
from pydantic_ai_harness.skills import Skills

from harness.features.selfauthoring.domain import CUSTOM_ITEM_DIRECTORIES, AgentActivated, CustomAgent
from harness.features.selfauthoring.exceptions import AgentValidationError
from harness.features.selfauthoring.infrastructure.activation_log import NoActivations, record_overwrite
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.skills_plugin import registry_skill_names, skill_refusal, unknown_skill_names

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from pydantic import JsonValue

    from harness.features.selfauthoring.internal_contracts import ActivationLog

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_NO_CUSTOM_NAMES: Mapping[str, str] = MappingProxyType({})
"""The empty default for `validate_agent_document`'s `active_custom_capability_names`, shared and frozen."""


def _parsed_mapping(spec_yaml: str) -> Mapping[str, JsonValue]:
    """`spec_yaml` as a plain mapping, or `AgentValidationError` when it is not YAML or not a mapping."""
    try:
        parsed = yaml.safe_load(spec_yaml)
    except yaml.YAMLError as exc:
        msg = f"invalid YAML: {exc}"
        raise AgentValidationError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"an agent document must parse to a mapping, got {type(parsed).__name__}"
        raise AgentValidationError(msg)
    return parsed


def _trimmed(error: ValidationError) -> str:
    """`error`'s findings as one compact line per problem, with no schema-error URL and no echoed input."""
    lines = []
    for problem in error.errors(include_url=False, include_context=False, include_input=False):
        loc = ".".join(str(part) for part in problem["loc"]) or "(document)"
        lines.append(f"{loc}: {problem['msg']}")
    return "; ".join(lines)


def _active_custom_capability_names(capabilities_directory: Path) -> dict[str, str]:
    """Each active custom capability's serialization name mapped to the name it was written under, skipping any that fail to load."""
    names: dict[str, str] = {}
    for record in FileCustomCapabilities.read_only(capabilities_directory).list_all():
        if record.status != "active":
            continue
        try:
            instance = load_capability_instance(capabilities_directory / record.module_file)
        except CapabilityValidationError:
            continue
        serialization_name = type(instance).get_serialization_name()
        if serialization_name is not None:
            names[serialization_name] = record.name
    return names


def _document_skill_names(mapping: Mapping[str, JsonValue]) -> list[str]:
    """The `skills` field's names, kept out of `AgentSpec` itself, or `AgentValidationError` when it is not a list of strings."""
    raw = mapping.get("skills")
    if raw is None:
        return []
    if not isinstance(raw, list):
        msg = "skills must be a list of skill names"
        raise AgentValidationError(msg)
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            msg = "skills must be a list of skill names"
            raise AgentValidationError(msg)
        names.append(item)
    return names


def validate_agent_document(
    spec_yaml: str,
    *,
    custom_capability_types: Sequence[type[AbstractCapability[object]]] = (),
    active_custom_capability_names: Mapping[str, str] = _NO_CUSTOM_NAMES,
    known_skill_names: Collection[str] | None = None,
) -> AgentSpec:
    """Parse and validate `spec_yaml` as pydantic-ai's own `AgentSpec`, its capability and skill names included.

    `Skills` always joins the registry the document is checked against, so a document naming it
    directly validates the same way the `skills` shorthand does.

    Args:
        spec_yaml: The document, as YAML.
        custom_capability_types: The promoted custom capabilities, which join the capability registry.
            Empty until one is promoted.
        active_custom_capability_names: Each active custom capability's serialization name, mapped to
            the name it was written under. An unknown capability that matches one here gets a refusal
            naming the exact `harness promote capability <name>` to run.
        known_skill_names: The names `skills` is checked against. The registry alone when `None`.

    Returns:
        The validated document.

    Raises:
        AgentValidationError: The text is not valid YAML or not a mapping, its `skills` entry is not a
            list of names or names one absent from `known_skill_names`, it fails
            `AgentSpec.model_validate`, or it declares a capability name the registry does not know.
            That refusal lists every name the registry does know. It also refuses a capability that supplies a workspace,
            whether the document names it directly or inside a wrapper.
    """
    mapping = _parsed_mapping(spec_yaml)
    skill_names = _document_skill_names(mapping)
    known_skills = known_skill_names if known_skill_names is not None else registry_skill_names()
    if unknown := unknown_skill_names(skill_names, known_skills):
        raise AgentValidationError(skill_refusal(unknown, known_skills))
    document = {key: value for key, value in mapping.items() if key != "skills"}
    try:
        agent_spec = AgentSpec.model_validate(document)
    except ValidationError as exc:
        raise AgentValidationError(_trimmed(exc)) from exc

    registry = get_capability_registry((*custom_capability_types, Skills))
    unknown = sorted({capability.name for capability in agent_spec.capabilities if capability.name not in registry})
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        known = ", ".join(sorted(registry)) or "none"
        msg = f"unknown capabilities: {names} (known capabilities: {known})"
        pointers = [
            f"{declared!r} was written as {active_custom_capability_names[declared]!r} but not promoted. "
            f"Run `harness promote capability {active_custom_capability_names[declared]}`"
            for declared in unknown
            if declared in active_custom_capability_names
        ]
        if pointers:
            msg += "; " + "; ".join(pointers)
        raise AgentValidationError(msg)
    if supplying := sorted({name for capability in agent_spec.capabilities for name in _workspace_suppliers(capability, registry)}):
        names = ", ".join(repr(name) for name in supplying)
        msg = f"capabilities that supply a workspace are refused: {names}. The harness decides where an agent works, so remove them"
        raise AgentValidationError(msg)
    return agent_spec


def _workspace_suppliers(capability: CapabilitySpec, registry: Mapping[str, type[AbstractCapability[object]]]) -> list[str]:
    """The names of the capabilities that supply a workspace: `capability` itself, or for a wrapper, those it holds."""
    capability_type = registry[capability.name]
    pass_through = next((base for base in (WrapperCapability, CombinedCapability) if issubclass(capability_type, base)), None)
    if capability_type.get_workspace is not (pass_through or AbstractCapability).get_workspace:
        return [capability.name]
    if pass_through is None:
        return []
    held = _held_capabilities((*capability.args, *capability.kwargs.values()), registry)
    return [name for nested in held for name in _workspace_suppliers(nested, registry)]


def _held_capabilities(values: Iterable[JsonValue], registry: Mapping[str, type[AbstractCapability[object]]]) -> Iterator[CapabilitySpec]:
    """Every capability spec among `values` whose name `registry` knows, looking inside lists and mappings."""
    for value in values:
        try:
            spec = CapabilitySpec.model_validate(value)
        except ValidationError:
            spec = None
        if spec is not None and spec.name in registry:
            yield spec
        elif isinstance(value, dict):
            yield from _held_capabilities(value.values(), registry)
        elif isinstance(value, list):
            yield from _held_capabilities(value, registry)


class _Manifest(BaseModel):
    agents: list[CustomAgent] = []


@dataclass
class FileCustomAgents:
    """The custom agents on disk, each landing disabled until the owner activates it.

    Each custom agent is one `<name>.yaml` file under `directory`, written verbatim, so the stored
    document is exactly what was written. A sibling `manifest.json` indexes them. Pending is the only
    landing state, even for a document that validates cleanly. Only `activate` and `deactivate`, driven
    by the owner, flip an entry, and each act is recorded in the activation log.
    """

    directory: Path
    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded. Custom agents that are only read are built
    over `NoActivations`."""

    custom_capability_types: Sequence[type[AbstractCapability[object]]] = ()
    """Promoted custom capabilities, threaded into every validation and resolution here. Empty until one is promoted."""
    known_skill_names: Collection[str] | None = None
    """The name space `skills` is checked against. `None` falls back to the registry alone."""

    @classmethod
    def read_only(cls, directory: Path) -> FileCustomAgents:
        """Custom agents over `directory` that record nowhere, for a caller that only reads.

        Args:
            directory: The folder holding the agent documents and the manifest.

        Returns:
            The custom agents, built over `NoActivations`.
        """
        return cls(directory, activations=NoActivations())

    @property
    def _manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    def _load_manifest(self) -> _Manifest:
        path = self._manifest_path
        if not path.exists():
            return _Manifest()
        try:
            return _Manifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError):
            return _Manifest()

    def _save_manifest(self, manifest: _Manifest) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(self._manifest_path, manifest.model_dump_json(indent=2))

    def _upsert(self, record: CustomAgent) -> None:
        manifest = self._load_manifest()
        for index, existing in enumerate(manifest.agents):
            if existing.name == record.name:
                manifest.agents[index] = record
                break
        else:
            manifest.agents.append(record)
        self._save_manifest(manifest)

    def _existing(self, name: str) -> CustomAgent | None:
        for record in self.list_all():
            if record.name == name:
                return record
        return None

    def write(self, name: str, spec_yaml: str) -> CustomAgent:
        """Write `spec_yaml` to `<name>.yaml`, validate it, and upsert its manifest entry disabled.

        A document that validates cleanly lands disabled exactly like one that failed validation. The
        owner activates it with `harness activate agent <name>`. Overwriting an active agent is itself
        a deactivation, recorded from `agent`, the only surface that writes.

        Args:
            name: The name to write the document under.
            spec_yaml: The complete `AgentSpec` document, as YAML.

        Returns:
            The manifest entry as written, with the validation error when there was one.

        Raises:
            ValueError: `name` is not lowercase letters, digits and underscores starting with a letter.
                Nothing is written.
        """
        if not _NAME_RE.match(name):
            msg = f"invalid agent name {name!r}: use lowercase letters, digits, and underscores, starting with a letter"
            raise ValueError(msg)
        previous = self._existing(name)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{name}.yaml"
        path.write_text(spec_yaml, encoding="utf-8")
        try:
            agent_spec = validate_agent_document(
                spec_yaml,
                custom_capability_types=self.custom_capability_types,
                active_custom_capability_names=_active_custom_capability_names(self.directory.parent / CUSTOM_ITEM_DIRECTORIES["capability"]),
                known_skill_names=self.known_skill_names,
            )
            record = CustomAgent(name=name, document_file=path.name, model=agent_spec.model)
        except AgentValidationError as exc:
            record = CustomAgent(name=name, document_file=path.name, model=None, last_error=str(exc))
        self._upsert(record)
        record_overwrite(previous, name, event_type=AgentActivated, activations=self.activations, revoke=None)
        return record

    def activate(self, name: str) -> bool:
        """Flip `name` active and record the act from `cli`.

        Args:
            name: The agent's name.

        Returns:
            Whether an agent had that name.

        Raises:
            ValueError: The agent's last write failed validation. Activating a broken document is
                refused, with the stored error in the message.
        """
        record = self._existing(name)
        if record is not None and record.last_error is not None:
            msg = f"cannot activate agent {name!r}: it failed validation: {record.last_error}"
            raise ValueError(msg)
        return self._set_status(name, "active", action="activate")

    def delete(self, name: str) -> bool:
        """Remove `name`'s manifest entry and its document, and record the act from `cli`.

        An active agent is refused, so tidying the library never changes the worker catalog.

        Args:
            name: The agent's name.

        Returns:
            Whether an agent had that name.

        Raises:
            ValueError: `name` is still active. It has to be disabled first.
        """
        record = self._existing(name)
        if record is None:
            return False
        if record.status == "active":
            msg = f"cannot delete agent {name!r} while it is active: disable it first"
            raise ValueError(msg)
        manifest = self._load_manifest()
        manifest.agents = [entry for entry in manifest.agents if entry.name != name]
        self._save_manifest(manifest)
        (self.directory / record.document_file).unlink(missing_ok=True)
        self.activations.record(AgentActivated(name=name, action="delete", via="cli"))
        return True

    def deactivate(self, name: str) -> bool:
        """Flip `name` disabled and record the act from `cli`.

        Args:
            name: The agent's name.

        Returns:
            Whether an agent had that name.
        """
        return self._set_status(name, "disabled", action="deactivate")

    def _set_status(self, name: str, status: Literal["active", "disabled"], *, action: Literal["activate", "deactivate"]) -> bool:
        manifest = self._load_manifest()
        found = False
        for record in manifest.agents:
            if record.name == name:
                record.status = status
                found = True
        if found:
            self._save_manifest(manifest)
            self.activations.record(AgentActivated(name=name, action=action, via="cli"))
        return found

    def list_all(self) -> list[CustomAgent]:
        """Every manifest entry, in insertion order.

        Returns:
            The entries.
        """
        return self._load_manifest().agents

    def resolve_active(self, name: str) -> Mapping[str, JsonValue]:
        """The document of the active agent named `name`, which is what a workflow step's agent reference resolves to.

        It reads from disk on every call, so an activation takes effect the next time anything resolves
        the name, with no restart and no cache to go stale.

        Args:
            name: The agent's name.

        Returns:
            The document, as a plain mapping.

        Raises:
            LookupError: No custom agent has this name, it exists but is not active (the message names
                `harness activate agent <name>`, the fix), or its stored document cannot be read.
        """
        for record in self.list_all():
            if record.name != name:
                continue
            if record.status != "active":
                msg = f"agent {name!r} is pending. Run `harness activate agent {name}` to activate it"
                raise LookupError(msg)
            try:
                return _parsed_mapping((self.directory / record.document_file).read_text(encoding="utf-8"))
            except (OSError, AgentValidationError) as exc:
                msg = f"agent {name!r} is active but its stored document could not be read: {exc}"
                raise LookupError(msg) from exc
        msg = f"No custom agent named {name!r}."
        raise LookupError(msg)
