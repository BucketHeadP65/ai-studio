"""The agent's own self-authoring tools: the creation capabilities for each custom-item type, and the profile voice.

Every custom item the agent writes lands pending. Each capability here states that in its guidance,
and the owner's `harness activate` is the only way one goes live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai_harness.capability_creation import CapabilityCreation

from harness.features.selfauthoring.infrastructure.agents import FileCustomAgents
from harness.features.selfauthoring.infrastructure.capabilities import FileCustomCapabilities
from harness.features.selfauthoring.infrastructure.profiles import FileCustomProfiles
from harness.features.selfauthoring.infrastructure.steps import FileCustomSteps
from harness.features.selfauthoring.service import selected_profile_voice

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from pydantic_ai._instructions import AgentInstructions
    from pydantic_ai.tools import RunContext
    from pydantic_ai.toolsets import AgentToolset

    from harness.features.selfauthoring.internal_contracts import ActivationLog
    from harness.settings import StateSettings

PENDING_GUIDANCE = (
    "You can author new pydantic-ai capabilities at runtime with `write_custom_capability(name, code)`. "
    "A capability is a subclass of `pydantic_ai.capabilities.AbstractCapability` that constructs "
    "with no arguments and overrides one or more lifecycle hooks (a single overridden hook is a "
    "valid capability). Every capability you author lands pending: it is validated immediately but "
    "stays disabled until the owner runs `harness activate capability <name>`. Authoring "
    "it does not make it active, on this run or any later one. Call `list_custom_capabilities` "
    "to see each one's true status, and `disable_custom_capability` to stop one you no longer "
    "want considered. Once active, the owner may also promote it (`harness promote "
    "capability <name>`) if it declares a spec-serialization name and a config dataclass on the "
    "class itself. A promoted capability can be declared by name in a custom agent document. "
    "Promotion is the owner's act. You cannot do it yourself."
)
"""The guidance `PendingCapabilityCreation` is given in place of the vendor's, which says a capability
becomes active on the next run. Here a custom capability stays disabled until the owner activates it,
and `list_custom_capabilities` shows each one's real status."""


_TOOL_NAMES = {
    "write_custom_capability": "author_capability",
    "list_custom_capabilities": "list_authored_capabilities",
    "disable_custom_capability": "disable_authored_capability",
}
"""This app's name for each of the vendor's three capability tools. The vendor keeps its own names,
and the agent only ever sees these."""


@dataclass
class PendingCapabilityCreation(CapabilityCreation[AgentDepsT]):
    """The vendor's `CapabilityCreation`, over `FileCustomCapabilities` in place of the vendor's own store.

    Pass `guidance=PENDING_GUIDANCE` or other accurate text, because the vendor default describes the
    vendor's own activation timing. The three tools are mounted under this app's names (`_TOOL_NAMES`).
    The rename rides `tool_def`, so the code-mode marker on the write tool travels with it and the tool
    stays native beside `run_code`.
    """

    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded, so the owner's acts and the agent's own
    tools land in one log."""

    @property
    def store(self) -> FileCustomCapabilities:
        """The custom capabilities in `directory`, in place of the vendor's `CapabilityStore(self.directory)`."""
        return FileCustomCapabilities(self.directory, activations=self.activations)

    def get_toolset(self) -> AgentToolset[AgentDepsT] | None:
        """The vendor's authoring toolset, its three tools under this app's names.

        Returns:
            The renamed toolset, or what the vendor answered when it built no toolset.
        """
        toolset = super().get_toolset()
        if not isinstance(toolset, AbstractToolset):
            return toolset
        return toolset.renamed(_TOOL_NAMES)


STEP_GUIDANCE = (
    "You can author new steps at runtime with `write_custom_step(name, code)`. A "
    "step is one or more plain `async def` functions with every parameter and the return type "
    "annotated (`Any` is refused): an ingredient a workflow wires in, never a workflow "
    "itself. Every step you author lands pending: it is validated immediately but stays disabled "
    "until the owner runs `harness activate step <name>`, and even then it only becomes "
    "callable the next time this process starts, not on any turn of the current one, unlike an "
    "custom capability, an activated step is not picked up mid-process. Call "
    "`list_custom_steps` to see each one's true status and registered function names."
)
"""Static guidance for `StepCreation`. It states the pending contract and how steps differ from
`PENDING_GUIDANCE`'s capabilities: steps load once, into the registry a process boots with, so an
activation takes effect on the next process start rather than the next turn."""


@dataclass
class StepCreation(AbstractCapability[AgentDepsT]):
    """Custom steps at runtime: the `write_custom_step` and `list_custom_steps` tools.

    The code is validated by registering it into a throwaway copy of the registry of steps, which runs
    the engine's own checks. Every step lands pending. `harness activate step <name>` is the owner's
    activation, loaded into the session's registry at the next environment build.
    """

    directory: Path
    """Directory holding the custom `<name>.py` step files and the `manifest.json` index."""

    guidance: str | None = None
    """Static system-prompt guidance on authoring. Cache-stable. Leave `None` for `STEP_GUIDANCE`,
    or set `''` to omit guidance entirely."""

    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded, so the owner's acts and the agent's own
    tools land in one log."""

    @property
    def store(self) -> FileCustomSteps:
        """The custom steps in `directory`, the same ones the environment build loads at boot."""
        return FileCustomSteps(self.directory, activations=self.activations)

    def get_instructions(self) -> AgentInstructions[AgentDepsT] | None:
        """Static, cache-stable guidance on the step tools.

        Returns:
            `STEP_GUIDANCE` when `guidance` is `None`, else `guidance`, or `None` when that is empty.
        """
        guidance = STEP_GUIDANCE if self.guidance is None else self.guidance
        return guidance or None

    def get_toolset(self) -> AgentToolset[AgentDepsT] | None:
        """The toolset providing `write_custom_step` and `list_custom_steps` over `store`.

        Returns:
            The toolset.
        """
        return StepCreationToolset[AgentDepsT](self.store)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        """Not spec-serializable: the capability holds live custom steps on disk.

        Returns:
            Always `None`.
        """
        return None


class StepCreationToolset(FunctionToolset[AgentDepsT]):
    """The `write_custom_step` and `list_custom_steps` tools."""

    def __init__(self, steps: FileCustomSteps) -> None:
        """Register both tools over `steps`.

        Args:
            steps: The custom steps the tools write and list.
        """
        super().__init__()
        self._steps = steps
        self.add_function(
            self.write_custom_step,
            name="write_custom_step",
            metadata={"code_arg_name": "code", "code_arg_language": "python"},
        )
        self.add_function(self.list_custom_steps, name="list_custom_steps")

    async def write_custom_step(self, name: str, code: str) -> str:
        """Author one or more custom steps from Python source.

        `code` must define at least one public `async def` function with every parameter and the
        return type annotated (`Any` is refused). The registry of steps' own front plugin validates
        it. Every public async function the module defines is registered under its own name. It
        lands pending: validated immediately, but not callable from any workflow until the owner runs
        `harness activate step <name>`.

        Args:
            name: Identifier for the step module. Lowercase letters, digits, and underscores,
                starting with a letter. Reusing a name replaces the previous module of that name.
            code: Complete Python source defining one or more public async functions.
        """
        try:
            record = self._steps.write(name, code)
        except ValueError as exc:
            raise ModelRetry(str(exc)) from exc
        if record.last_error is not None:
            return (
                f"Step {name!r} was written but failed validation: {record.last_error}\n"
                f"Fix the code and call write_custom_step again with the same name."
            )
        return (
            f"Step {name!r} ({', '.join(record.function_names)}) written and validated. It stays "
            f"disabled until the owner runs `harness activate step {name}`, and even then "
            f"only becomes callable the next time this process starts, not on any turn of the "
            f"current one."
        )

    async def list_custom_steps(self) -> str:
        """List the custom steps, with their status and registered function names."""
        records = self._steps.list_all()
        if not records:
            return "No custom steps yet."
        lines: list[str] = []
        for record in records:
            suffix = f". ERROR: {record.last_error}" if record.last_error is not None else ""
            functions = ", ".join(record.function_names) or "?"
            lines.append(f"- {record.name} [{record.status}] {functions}{suffix}")
        return "\n".join(lines)


AGENT_GUIDANCE = (
    "You can author new agents at runtime with `write_custom_agent(name, spec_yaml)`. An agent "
    "document is pydantic-ai's own `AgentSpec` (model, instructions, settings, and named "
    "capabilities, written as YAML) and composes REGISTERED behavior only. It can never carry "
    "a function body. Every capability it names must already exist in the capability registry, "
    "or authoring is refused, naming the unknown name. The document may also name `skills`, a "
    "list of skill names this agent holds. Call `list_skills` first to see what is on file, "
    "an unknown name is refused the same way an unknown capability is. Every agent you author "
    "lands pending: it is validated immediately but stays disabled until the owner runs `harness "
    "activate agent <name>`. Authoring it does not make it usable in any workflow step, on "
    "this run or any later one. Once activated, a workflow step naming it by name resolves it "
    "immediately: no restart, no waiting for the next turn. Call `list_custom_agents` to see "
    "each one's true status."
)
"""Static guidance for `AgentCreation`. It states the pending contract and the activation timing.
Resolution is fresh every time, so an activation lands sooner than a custom step's (the next
environment build) and needs none of the per-turn re-injection a custom capability's does."""


class AgentCreationToolset(FunctionToolset[AgentDepsT]):
    """The `write_custom_agent` and `list_custom_agents` tools."""

    def __init__(self, agents: FileCustomAgents) -> None:
        """Register both tools over `agents`.

        Args:
            agents: The custom agents the tools write and list.
        """
        super().__init__()
        self._agents = agents
        self.add_function(
            self.write_custom_agent,
            name="write_custom_agent",
            metadata={"code_arg_name": "spec_yaml", "code_arg_language": "yaml"},
        )
        self.add_function(self.list_custom_agents, name="list_custom_agents")

    async def write_custom_agent(self, name: str, spec_yaml: str) -> str:
        """Author an agent from a pydantic-ai `AgentSpec` document, written as YAML.

        The document composes REGISTERED behavior only (model, instructions, settings, and
        named capabilities), never a function body. Every capability it names is checked
        against the capability registry. An unknown name is refused, naming it. It lands
        pending: validated immediately, but not resolvable by any workflow step until the owner
        runs `harness activate agent <name>`.

        Args:
            name: Identifier for the agent. Lowercase letters, digits, and underscores, starting
                with a letter. Reusing a name replaces the previous document of that name.
            spec_yaml: The complete `AgentSpec` document, as YAML.
        """
        try:
            record = self._agents.write(name, spec_yaml)
        except ValueError as exc:
            raise ModelRetry(str(exc)) from exc
        if record.last_error is not None:
            return (
                f"Agent {name!r} was written but failed validation: {record.last_error}\n"
                f"Fix the document and call write_custom_agent again with the same name."
            )
        return (
            f"Agent {name!r} written and validated. It stays disabled until the owner runs "
            f"`harness activate agent {name}`. Authoring it does not make it usable in "
            f"any workflow step, on this run or any later one. Once activated, a workflow step "
            f"naming it resolves it immediately: no restart, no waiting for the next turn."
        )

    async def list_custom_agents(self) -> str:
        """List the custom agents, with their status and the model each one names."""
        records = self._agents.list_all()
        if not records:
            return "No custom agents yet."
        lines: list[str] = []
        for record in records:
            suffix = f". ERROR: {record.last_error}" if record.last_error is not None else ""
            model = record.model or "(host default model)"
            lines.append(f"- {record.name} [{record.status}] {model}{suffix}")
        return "\n".join(lines)


@dataclass
class AgentCreation(AbstractCapability[AgentDepsT]):
    """Custom agents at runtime: the `write_custom_agent` and `list_custom_agents` tools.

    The YAML is validated by the framework's own `AgentSpec.model_validate`, plus every named
    capability resolving against the capability registry. Every agent lands pending. `harness activate
    agent <name>` is the owner's activation, and a workflow step's agent reference resolves it from
    then on.
    """

    directory: Path
    """Directory holding the custom `<name>.yaml` documents and the `manifest.json` index."""

    guidance: str | None = None
    """Static system-prompt guidance on authoring. Cache-stable. Leave `None` for `AGENT_GUIDANCE`,
    or set `''` to omit guidance entirely."""

    custom_capability_types: Sequence[type[AbstractCapability[object]]] = ()
    """Promoted custom capabilities, threaded to `self.store`. Empty until one is promoted."""

    known_skill_names: Collection[str] | None = None
    """The name space `skills` is checked against, threaded to `self.store`. `None` falls back to the registry alone."""

    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded, so the owner's acts and the agent's own
    tools land in one log."""

    @property
    def store(self) -> FileCustomAgents:
        """The custom agents in `directory`, the same folder the environment build resolves workers through."""
        return FileCustomAgents(
            self.directory,
            activations=self.activations,
            custom_capability_types=self.custom_capability_types,
            known_skill_names=self.known_skill_names,
        )

    def get_instructions(self) -> AgentInstructions[AgentDepsT] | None:
        """Static, cache-stable guidance on the agent tools.

        Returns:
            `AGENT_GUIDANCE` when `guidance` is `None`, else `guidance`, or `None` when that is empty.
        """
        guidance = AGENT_GUIDANCE if self.guidance is None else self.guidance
        return guidance or None

    def get_toolset(self) -> AgentToolset[AgentDepsT] | None:
        """The toolset providing `write_custom_agent` and `list_custom_agents` over `store`.

        Returns:
            The toolset.
        """
        return AgentCreationToolset[AgentDepsT](self.store)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        """Not spec-serializable: the capability holds live custom agents on disk.

        Returns:
            Always `None`.
        """
        return None


class ProfileVoice(AbstractCapability[AgentDepsT]):
    """The selected profile's instructions, joined to the agent's voice per request.

    Always on: mounted every request, silent while no profile is selected. It reads the selection when
    the request is composed, like the plugins, so a selection reaches the very next turn.
    """

    def __init__(self, state: StateSettings) -> None:
        """Read the selection under `state`'s home.

        Args:
            state: Where the selection lives.
        """
        self._state = state

    def get_instructions(self) -> AgentInstructions[AgentDepsT] | None:
        """A per-request read of the selection snapshot's instructions.

        Returns:
            A function answering the selected profile's instructions, or an empty string while none are in effect.
        """
        state = self._state

        def compose(ctx: RunContext[AgentDepsT]) -> str:
            del ctx
            return selected_profile_voice(state) or ""

        return compose


PROFILE_GUIDANCE = (
    "You can author profiles at runtime with `write_custom_profile(name, document_yaml)`. A profile is "
    "a YAML document the owner can put in effect as one act: `plugins` (which functional plugins are "
    "on), optional `capabilities`/`scripts`/`agents` lists (which custom items are held "
    "active, a list you omit is left alone), `skills` (the chat agent's skill catalog while "
    "selected, call `list_skills` first to see what is on file, an unknown name is refused), "
    "optional `instructions` (standing text joined to the voice while selected), optional "
    "`workflows` (pinned for display), and a `description`. Every profile you author lands "
    "pending: the owner activates it with `harness activate profile <name>` and separately "
    "SELECTS it to put it in effect. You can do neither yourself. Call `list_custom_profiles` "
    "to see each one's true status."
)
"""Static guidance for `ProfileCreation`: the profile document's grammar, and the two owner acts
(activate, then select) that stand between a custom profile and the live setup."""


@dataclass
class ProfileCreation(AbstractCapability[AgentDepsT]):
    """Custom profiles at runtime: the `write_custom_profile` and `list_custom_profiles` tools."""

    directory: Path
    """Directory holding the custom `<name>.yaml` documents and the `manifest.json` index."""

    guidance: str | None = None
    """Static system-prompt guidance on authoring. Cache-stable. Leave `None` for
    `PROFILE_GUIDANCE`, or set `''` to omit guidance entirely."""

    known_skill_names: Collection[str] | None = None
    """The name space `skills` is checked against, threaded to `self.store`. `None` falls back to the registry alone."""

    activations: ActivationLog = field(kw_only=True)
    """Where every act on an item in this directory is recorded, so the owner's acts and the agent's own
    tools land in one log."""

    @property
    def store(self) -> FileCustomProfiles:
        """The custom profiles in `directory`, the same ones the owner's `harness` acts reach."""
        return FileCustomProfiles(self.directory, activations=self.activations, known_skill_names=self.known_skill_names)

    def get_instructions(self) -> AgentInstructions[AgentDepsT] | None:
        """Static, cache-stable guidance on the profile tools.

        Returns:
            `PROFILE_GUIDANCE` when `guidance` is `None`, else `guidance`, or `None` when that is empty.
        """
        guidance = PROFILE_GUIDANCE if self.guidance is None else self.guidance
        return guidance or None

    def get_toolset(self) -> AbstractToolset[AgentDepsT] | None:
        """The toolset providing `write_custom_profile` and `list_custom_profiles` over `store`.

        Returns:
            The toolset.
        """
        return ProfileCreationToolset[AgentDepsT](self.store)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        """Not spec-serializable: the capability holds live custom profiles on disk.

        Returns:
            Always `None`.
        """
        return None


class ProfileCreationToolset(FunctionToolset[AgentDepsT]):
    """The `write_custom_profile` and `list_custom_profiles` tools."""

    def __init__(self, profiles: FileCustomProfiles) -> None:
        """Register both tools over `profiles`.

        Args:
            profiles: The custom profiles the tools write and list.
        """
        super().__init__()
        self._profiles = profiles
        self.add_function(
            self.write_custom_profile,
            name="write_custom_profile",
            metadata={"code_arg_name": "document_yaml", "code_arg_language": "yaml"},
        )
        self.add_function(self.list_custom_profiles, name="list_custom_profiles")

    async def write_custom_profile(self, name: str, document_yaml: str) -> str:
        """Author one profile from a YAML profile document.

        The document may declare `description`, `instructions`, `plugins` (toggleable plugin names),
        `capabilities`/`scripts`/`agents` (custom-item names to hold active, omit a list to leave
        that item type alone), `skills` (skill names the chat agent holds while selected), and
        `workflows` (pinned for display). It lands pending: validated immediately, in effect only
        after the owner activates AND selects it.

        Args:
            name: Identifier for the profile. Lowercase letters, digits, and underscores,
                starting with a letter. Reusing a name replaces the previous document.
            document_yaml: The complete YAML profile document.
        """
        try:
            record = self._profiles.write(name, document_yaml)
        except ValueError as exc:
            raise ModelRetry(str(exc)) from exc
        if record.last_error is not None:
            return (
                f"Profile {name!r} was written but failed validation: {record.last_error}\n"
                f"Fix the document and call write_custom_profile again with the same name."
            )
        return (
            f"Profile {name!r} written and validated (plugins: {', '.join(record.plugins) or 'none'}). It stays "
            f"disabled until the owner runs `harness activate profile {name}`, and governs the "
            f"composition only once the owner also selects it."
        )

    async def list_custom_profiles(self) -> str:
        """List the custom profiles, with their status and plugin lists."""
        records = self._profiles.list_all()
        if not records:
            return "No custom profiles yet."
        lines: list[str] = []
        for record in records:
            suffix = f". ERROR: {record.last_error}" if record.last_error is not None else ""
            lines.append(f"- {record.name} [{record.status}] plugins: {', '.join(record.plugins) or 'none'}{suffix}")
        return "\n".join(lines)
