"""The agent's plugins: the toggleable half of the built-in setup, and its off switches.

The catalog below names every built-in the setup mounts. The owner turns a toggleable plugin on or
off with `harness plugins` or in AI Factory Studio's control room. The change reaches the next chat
turn with no restart, because `Switched` reads the persisted set each time a request is composed.
The always-on plugins cannot be toggled and stay on however the others are set.
"""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability, DynamicCapability
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic_ai.tools import RunContext

    from harness.settings import StateSettings


@dataclass(frozen=True)
class Plugin:
    """One named piece of the built-in setup, as the catalog and the UI describe it."""

    name: str
    description: str


TOGGLEABLE: tuple[Plugin, ...] = (
    Plugin("knowledge-base", "The knowledge bases' reads, search and the leveled fetch."),
    Plugin("leveled-reads", "The leveled-read governor, what a descent may cost in context."),
    Plugin("sql", "SQL over the same knowledge bases, run_sql and describe_schema."),
    Plugin("pydantic-ai-docs", "The current pydantic-ai docs, one tool away."),
    Plugin("recall", "Search over past conversations and runs."),
    Plugin("friction", "The agent's own friction reports, flag_friction."),
    Plugin("skills", "The agent's skill files, loaded from the skills directory."),
    Plugin("list-skills", "The skills registry listing, list_skills."),
    Plugin("shell", "Run commands in the workspace."),
    Plugin("files", "The workspace as files, reads riding code mode and writes as acts."),
    Plugin("author-capabilities", "Capability authoring, write_custom_capability."),
    Plugin("author-scripts", "Script authoring, write_custom_step."),
    Plugin("author-agents", "Agent authoring, write_custom_agent."),
    Plugin("author-profiles", "Profile authoring, write_custom_profile."),
    Plugin("delegate", "Delegation to the worker, delegate_task on a tier of the model menu."),
    Plugin("agent-scripts", "Scripted delegation, run_agent_script over the tier workers."),
    Plugin("external-agent", "Drive another ACP agent (Claude Code, Codex, Pi), run_external_agent."),
    Plugin("memory", "The agent's standing memory: the injected index, read, search, and write tools."),
    Plugin("web-search", "web_search, Tavily when a key is set, DuckDuckGo (keyless) otherwise."),
    Plugin("digest-now", "digest_now: land a source, sync a knowledge base, and start its digest workflow."),
    Plugin("follow-me", "The companion's hands: the four page verbs and the approval-gated console acts, in the master session only."),
)
"""Every plugin the owner may turn on or off. Order is the display order."""

ALWAYS_ON: tuple[Plugin, ...] = (
    Plugin("workflows", "The workflow process, the library's verbs, the validator, and the runner."),
    Plugin("spend-limits", "The money brake, per run and per day."),
    Plugin("code-mode", "Look-and-compute tools folded into one program per request."),
    Plugin("loop-detection", "The nudge when the agent circles."),
    Plugin("output-limits", "Oversized tool output spilled with a read-back handle, never flooded."),
    Plugin("step-persistence", "Every turn recorded, so the conversation survives a restart."),
    Plugin("instructions", "The agent's standing instructions."),
)
"""What is always on. Listed so the setup can be read whole, never toggled."""

DEFAULT_DISABLED: frozenset[str] = frozenset({"sql"})
"""The plugins off on a fresh deployment. SQL starts off, so the read tools are the only retrieval
tools until the owner turns it on."""

_TOGGLEABLE_NAMES = frozenset(plugin.name for plugin in TOGGLEABLE)
_ALWAYS_ON_NAMES = frozenset(plugin.name for plugin in ALWAYS_ON)


def _plugins_path(state: StateSettings) -> Path:
    """The persisted set of plugins turned off, under `state`'s home."""
    return state.disabled_plugins


def _selection_path(state: StateSettings) -> Path:
    """Beside the plugins file: the selected profile's snapshot, when one is in effect."""
    return state.selected_profile


@dataclass(frozen=True)
class Selection:
    """The profile in effect, as it stood when it was selected.

    An edit to the profile document afterwards leaves the setup in effect unchanged. Selecting the
    profile again applies the edit.
    """

    name: str
    plugins: tuple[str, ...]
    instructions: str | None = None
    skills: tuple[str, ...] = ()


def current_selection(state: StateSettings) -> Selection | None:
    """The selected profile's snapshot, or `None` when the hand-set plugins file governs.

    Args:
        state: Where this deployment's durable process state is written.

    Returns:
        The snapshot, or `None` when no profile is selected or its file cannot be read.
    """
    try:
        stored = json.loads(_selection_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or not isinstance(stored.get("name"), str):
        return None
    raw_plugins = stored.get("plugins")
    on = tuple(name for name in raw_plugins if isinstance(name, str)) if isinstance(raw_plugins, list) else ()
    raw_instructions = stored.get("instructions")
    raw_skills = stored.get("skills")
    skills = tuple(name for name in raw_skills if isinstance(name, str)) if isinstance(raw_skills, list) else ()
    return Selection(
        name=stored["name"],
        plugins=on,
        instructions=raw_instructions if isinstance(raw_instructions, str) else None,
        skills=skills,
    )


def apply_selection(state: StateSettings, selection: Selection) -> None:
    """Persist `selection` atomically. Its plugins and skills govern from the next turn.

    Args:
        state: Where this deployment's durable process state is written.
        selection: The profile snapshot to put in effect.
    """
    payload = {
        "name": selection.name,
        "plugins": list(selection.plugins),
        "instructions": selection.instructions,
        "skills": list(selection.skills),
    }
    _atomic_write(_selection_path(state), json.dumps(payload))


def clear_selection(state: StateSettings) -> None:
    """Drop the selection, so the hand-set plugins file governs again.

    Args:
        state: Where this deployment's durable process state is written.
    """
    _selection_path(state).unlink(missing_ok=True)


def disabled_plugins(state: StateSettings) -> frozenset[str]:
    """The plugins currently off, read fresh on every call so a change needs no restart.

    While a profile is selected, its snapshot governs: every toggleable plugin it did not turn on is
    off, and the hand-set file is ignored until the selection is cleared. A stored name that matches
    no toggleable plugin is dropped.

    Args:
        state: Where this deployment's durable process state is written.

    Returns:
        The names of the plugins that are off, `DEFAULT_DISABLED` when no readable file is stored.
    """
    selection = current_selection(state)
    if selection is not None:
        return _TOGGLEABLE_NAMES - frozenset(selection.plugins)
    try:
        stored = json.loads(_plugins_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_DISABLED
    if not isinstance(stored, list):
        return DEFAULT_DISABLED
    return frozenset(name for name in stored if isinstance(name, str) and name in _TOGGLEABLE_NAMES)


def plugin_enabled(state: StateSettings, name: str) -> bool:
    """Whether the named plugin is on right now.

    Args:
        state: Where this deployment's durable process state is written.
        name: The plugin's name in the catalog.

    Returns:
        True unless the plugin is currently off.
    """
    return name not in disabled_plugins(state)


def check_toggleable(name: str) -> None:
    """Refuse a plugin name that cannot be toggled, because it is always on or unknown.

    Args:
        name: The plugin's name.

    Raises:
        ValueError: `name` is always on or names no plugin at all.
    """
    if name in _ALWAYS_ON_NAMES:
        msg = f"{name!r} is always on and cannot be toggled"
        raise ValueError(msg)
    if name not in _TOGGLEABLE_NAMES:
        known = ", ".join(sorted(_TOGGLEABLE_NAMES))
        msg = f"unknown plugin {name!r}; the toggleable plugins are: {known}"
        raise ValueError(msg)


def set_plugin(state: StateSettings, name: str, *, enabled: bool) -> None:
    """Turn one plugin on or off, persisted atomically. Takes effect on the very next turn.

    While a profile is selected the change is refused. The plugins follow the profile until the
    selection is cleared.

    Args:
        state: Where this deployment's durable process state is written.
        name: The plugin's name in the catalog.
        enabled: Whether to turn the plugin on.

    Raises:
        ValueError: `name` is always on (never toggleable), names no plugin at all, or a profile is
            selected.
    """
    check_toggleable(name)
    selection = current_selection(state)
    if selection is not None:
        msg = f"the plugins follow the selected profile {selection.name!r}; clear the selection to hand-toggle"
        raise ValueError(msg)
    off = set(disabled_plugins(state))
    if enabled:
        off.discard(name)
    else:
        off.add(name)
    _atomic_write(_plugins_path(state), json.dumps(sorted(off)))


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path`, creating its home, so a concurrent reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Switched(DynamicCapability[AgentDepsT]):
    """The wrapped capability, mounted per run only while its plugin is on and its run predicate agrees.

    The plugin state is read each time a run is composed, so a change through `harness plugins` or
    AI Factory Studio reaches the next turn with no rebuild. A plugin turned off mounts nothing: no
    tools, no instructions and no hooks. Its id is `plugin-{plugin}`, the stable identity durable
    execution requires of a dynamic capability.
    """

    def __init__(
        self,
        wrapped: AbstractCapability[AgentDepsT],
        *,
        plugin: str,
        state: StateSettings,
        only: Callable[[RunContext[AgentDepsT]], bool] | None = None,
    ) -> None:
        """Put `wrapped` behind the switch named `plugin`.

        Args:
            wrapped: The capability mounted while the plugin is on.
            plugin: The plugin's name in the catalog, which also names the capability's id.
            state: Where the persisted set of plugins turned off is read from.
            only: An extra condition read off the run context, such as the session's type. It must
                give the same answer for the same run dependencies. `None` adds no condition.
        """
        self.wrapped = wrapped
        self.plugin = plugin
        self.state = state
        self.only = only
        # A colon in the id breaks pydantic-ai, which reserves it as its own instruction-id delimiter.
        super().__init__(self._resolve, id=f"plugin-{plugin}")

    def _resolve(self, ctx: RunContext[AgentDepsT]) -> AbstractCapability[AgentDepsT] | None:
        if not plugin_enabled(self.state, self.plugin):
            return None
        if self.only is not None and not self.only(ctx):
            return None
        return self.wrapped
