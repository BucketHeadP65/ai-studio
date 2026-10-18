"""Where this app is on disk, and the knobs a deployment sets.

Deployment facts, not application logic: the checkout geometry, the paths runtime state lives
at, and the settings classes. Nothing here imports a slice or an adapter, so anything can import
it without a cycle.
"""

from __future__ import annotations

import os

from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from core.ai.inspection.contracts import ToolFacts, Watch
from core.ai.workflows.contracts import DEFAULT_TASK_QUEUE, Tier
from core.observability import LoggingSettings
from engram import policy_instructions
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

CHECKOUT = Path(__file__).resolve().parents[2]
"""This app's checkout: the root a step's shell is confined to, and where this machine's runtime state is written."""

AI_FACTORY_STUDIO_ORIGINS = ("http://localhost:3031", "http://127.0.0.1:3031")
"""The exact origins AI Factory Studio is served from. CORS and the terminal's origin gate both admit only these."""


CONSOLE_CLOSED_ENV = "HARNESS_CONSOLE_CLOSED"
"""Set to `1` on a shell whose caller has no person to ask, which is every shell a run holds.

The `harness` CLI refuses its console verbs when it sees it. Those verbs change what every later turn can do:
which plugins are on, which profile governs, which custom item is active.
A shell handed to a worker or to a scheduled step carries it."""


def console_is_closed(*, environ: Mapping[str, str] = os.environ) -> bool:
    """Whether this process was started by a caller with nobody to ask, so the console verbs refuse.

    Args:
        environ: The environment to read. The process's own by default.

    Returns:
        True when `HARNESS_CONSOLE_CLOSED` is `1`.
    """
    return environ.get(CONSOLE_CLOSED_ENV) == "1"


def repo_path(env: str, default: Path, *, environ: Mapping[str, str] = os.environ) -> Path:
    """A sibling checkout's root: the environment's when the named export carries one, else `default`.

    An empty value counts as absent, so `APP_STUDIO_REPO=` with nothing after it answers `default`.

    Args:
        env: The environment variable to read, such as `APP_STUDIO_REPO`.
        default: The root to answer when the variable is unset or empty.
        environ: The environment to read. The process's own by default.

    Returns:
        The variable's path with `~` expanded, or `default`.
    """
    named = environ.get(env)
    return Path(named).expanduser() if named else default


# Each root defaults to a sibling of this checkout, and its export overrides it.
ENGRAM_REPO = repo_path("APP_ENGRAM_REPO", CHECKOUT.parent / "engram")
ENGRAM_SETTINGS = ENGRAM_REPO / "config" / "application_settings.json"
"""The gitignored settings file every app reads its infrastructure and its knowledge bases from."""
ENGRAM_SETTINGS_EXAMPLE = ENGRAM_REPO / "config" / "application_settings.example.json"
"""The committed example, read in place of the settings file when that is missing."""
STUDIO_REPO = repo_path("APP_STUDIO_REPO", CHECKOUT.parent / "studio")
WORKSPACE = repo_path("APP_WORKSPACE", CHECKOUT.parent)
"""The directory the agent's shell and files plugins work in."""
WORKFLOW_KNOWLEDGE_BASE = repo_path("APP_WORKFLOW_KNOWLEDGE_BASE", CHECKOUT / ".state" / "workflow-knowledge-base")
"""The knowledge base whose schema validates a saved workflow. It sits under the default state home unless the export names one."""
REPO_DOCS_SCHEMA = CHECKOUT.parent / "docs" / "knowledge-base-schema.json"
"""The schema of this repository's own `repo-docs` knowledge base: its slug, its disclosure declarations and its purpose."""
SPILL_DIR = CHECKOUT / ".state" / "tool-output"

ROOT = Path(__file__).parent
"""This package's own directory, which holds what ships with the code."""
SKILLS = CHECKOUT / "skills"
"""The registry of skills: every skill a step, agent or profile in this app may name, and the files this repository's Claude Code reads."""


def aifactory_state(root: Path) -> Path:
    """Where a project's own durable process state lives: `root/.aifactory/state`.

    `workspace.open_workspace` creates it. This checkout's own home is the `state` section's instead.

    Args:
        root: The project's checkout root.

    Returns:
        The path of the project's state home.
    """
    return root / ".aifactory" / "state"


class ExternalAgentEntry(BaseModel):
    """One ACP agent this deployment may drive: how to start it, and how its permissions are answered.

    `env` is merged over a trimmed default environment by the spawn helper, so an agent's
    provider credentials belong here rather than in the launching shell. `permission` is `gate` by
    default: spawning another coding agent on this machine is opted into, never inherited.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    permission: Literal["auto", "gate"] = "gate"
    timeout_seconds: float = 600.0


class ExternalAgentSettings(RootModel[dict[str, ExternalAgentEntry]]):
    """The whole registry file: an agent name to how it is started and governed."""


type ExternalAgentRegistry = Callable[[], Mapping[str, ExternalAgentEntry]]
"""How a caller reads the registry. Each call reads the file again, so an edit reaches the next turn with no restart."""


def read_external_agents(path: Path) -> dict[str, ExternalAgentEntry]:
    """The agents `path` configures, read fresh. Empty when the file is not there.

    Args:
        path: The registry file.

    Returns:
        Each agent's name mapped to its entry, empty when the file cannot be read.

    Raises:
        ValueError: The file exists but is not a registry, such as a torn file or an entry that does not validate.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return ExternalAgentSettings.model_validate_json(text).root
    except ValidationError as exc:
        msg = f"{path} is not a valid external-agent registry: {exc}"
        raise ValueError(msg) from exc


class RunSettings(BaseModel):
    """Which engine runs a workflow, and where to find it.

    The same workflow, registry and gates either way. `in_process` is the default: the run is a
    coroutine of this app, and the run log on disk carries a waiting gate across a restart.
    `temporal` is the opt-in for a deployment that wants a run to be server state, surviving the
    app dying, and the app starts a local server itself when `temporal_address` answers nothing and
    names this machine.
    """

    run_engine: Literal["in_process", "temporal"] = "in_process"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = DEFAULT_TASK_QUEUE
    """The queue the worker serves. A run started on a queue nobody serves simply waits."""
    archive_days: int = 30
    """How long an ended run rests in the archive before the sweep removes it. `0` turns
    archiving off. A pinned record is never swept, whatever its age."""


class StoreSettings(BaseModel):
    """How the store ranks candidates.

    Hybrid by default: every stored card carries a vector, and the vector leg is what answers a
    request that shares no word with any card, so a search over a non-empty library never comes back
    empty. The query side needs the embedding credential this process ingests with. Without it a
    search refuses, and the store never falls back to keywords on its own. A deployment that
    holds no credential sets `APP_STORE__HYBRID_SEARCH=false` and ranks by BM25 alone.
    """

    hybrid_search: bool = True


class ModelSettings(BaseModel):
    """Where DeepSeek is routed on the gateway, and the ceiling on one response."""

    deepseek_gateway_route: str = "DeepSeek"
    """The gateway route segment DeepSeek requests go through, as the owner named it on the gateway.
    Overridable as `APP_MODELS__DEEPSEEK_GATEWAY_ROUTE`."""

    max_tokens: int = 8192
    """The ceiling on one response, sent on every request. Billing is on the tokens actually used, so
    this does not change what a turn costs, but a broker checks the balance against this ceiling
    before running the request, and refuses when the balance cannot cover the worst case. A ceiling
    far above what the work needs therefore refuses requests the balance could have paid for.
    Overridable as `APP_MODELS__MAX_TOKENS`."""


class SelfAuthoringSettings(BaseModel):
    """The owner's own surface: where their reviewed custom items live, and who a save here is by."""

    custom_root: Path | None = None
    """Override for the custom-item root. `None` resolves to `<checkout>/custom`."""
    saved_by: str = "human:owner"
    """Who a workflow saved through this deployment's own chat is attributed to. A deployment whose
    person has a name sets `APP_SELF_AUTHORING__SAVED_BY` to it."""


def custom_root(settings: SelfAuthoringSettings) -> Path:
    """Resolve the custom-item root: `settings.custom_root`, or `<checkout>/custom`.

    Args:
        settings: The self-authoring section.

    Returns:
        The custom-item root.
    """
    return settings.custom_root if settings.custom_root is not None else CHECKOUT / "custom"


class TerminalSettings(BaseModel):
    """Whether `/api/terminal/ws` may open a pty at all.

    Off by default: it spawns a real process (`claude`, falling back to `$SHELL`) in a
    directory reached from AI Factory Studio, so a deployment opts in explicitly
    (`APP_TERMINAL__ENABLED=true`) rather than inheriting shell access.
    """

    enabled: bool = False


class WebSearchSettings(BaseModel):
    """Whether `web_search` reaches Tavily (a key is set) or falls keyless to DuckDuckGo.

    `APP_WEB_SEARCH__TAVILY_API_KEY` carries the key. A deployment that sets none searches
    keyless, which is the fallback rather than a failure.
    """

    tavily_api_key: str | None = None


class StudioApiSettings(BaseModel):
    """Where this deployment's studio knowledge base API (`studio serve-http`) listens.

    `digest_now` and the knowledge base read tools reach the studio through it. `digest_now` lands a source and
    starts a refresh, and the read tools list the registry of knowledge bases.
    """

    base_url: str = "http://127.0.0.1:8100"


KNOWLEDGE_BASE_GUIDANCE = (
    "Search with the question's own words, and widen only with terms the knowledge base itself returned. "
    "Read a hit's summaries before its body, prefer a named section to a whole page, and stop on "
    "saturation: when a search comes back mostly ids you have already seen, stop retrieving and "
    "synthesize. Two to eight searches per question is the usual shape. That is a suggestion, so "
    "exceed it knowingly when the question warrants it rather than answering thin."
)
"""How to retrieve well from any knowledge base this deployment reads, written by the deployment.

`knowledge_base_guidance` prefers a knowledge base's own applied `_meta/active-policy.yaml` whenever there is one.
This is the floor beneath it, for every knowledge base this deployment opens for reading.
Overridable as `APP_KNOWLEDGE_BASE_GUIDANCE__GUIDANCE`. An empty value leaves a knowledge base with no applied policy
only its own domain text."""


class KnowledgeBaseGuidanceSettings(BaseModel):
    """The guidance floor alone, with no knowledge base name attached.

    `knowledge_base_guidance` (below) reads this for every knowledge base a deployment opens for reading,
    so it names none of them and reads with no environment at all.
    """

    guidance: str = KNOWLEDGE_BASE_GUIDANCE


class LibrarySearchSettings(BaseModel):
    """Whether one `search` or `fetch` call here may read more than one knowledge base.

    Off, engram makes `knowledge_base` a required argument on both tools, so the agent names the base it
    reads and a fan-out is refused. On, an omitted name reads every registered base at once.
    """

    cross_knowledge_base_search: bool = False


class StateSettings(BaseModel):
    """Where this deployment's durable process state is written, all of it under one home.

    Every path is a property over `home`, so pointing the home somewhere else moves the transcripts,
    the logs, the archive and the registries together and nothing is left behind pointing at the old
    one.
    """

    home: Path = CHECKOUT / ".state"

    @property
    def steps(self) -> Path:
        """Where a file-store transcript keeps its runs under this home: one directory per run, its snapshots inside."""
        return self.home / "steps"

    @property
    def session_log(self) -> Path:
        """The append-only timeline of what this process did."""
        return self.home / "session-log.jsonl"

    @property
    def evidence_log(self) -> Path:
        """The append-only record of what actually ran, and how it ended."""
        return self.home / "evidence.jsonl"

    @property
    def activation_log(self) -> Path:
        """The append-only record of every custom-item activation act."""
        return self.home / "activations.jsonl"

    @property
    def external_agents(self) -> Path:
        """The ACP agents this deployment may drive."""
        return self.home / "external-agents.json"

    @property
    def draft(self) -> Path:
        """The proposal currently on screen, so a restart keeps the one that was checked."""
        return self.home / "draft.json"

    @property
    def forgotten_marker(self) -> Path:
        """When the person last pressed Forget."""
        return self.home / "forgotten-at"

    @property
    def archive(self) -> Path:
        """Where ended runs rest, one file per run under its workflow's own folder."""
        return self.home / "runs" / "archive"

    @property
    def spend(self) -> Path:
        """What every run cost, one folder per workflow."""
        return self.home / "runs" / "spend"

    @property
    def spend_counters(self) -> Path:
        """What each spend ceiling has counted so far, shared by every process on this home and kept across a restart."""
        return self.home / "spend-counters.json"

    def waiting_run(self, *, project: bool) -> Path:
        """The one run that may be waiting at a gate when the process dies, in this home.

        A project home keeps its own file, so two homes never overwrite each other's. The
        deployment's own home keeps the single file every unscoped route and the boot restore read.

        Args:
            project: Whether this home is a project's rather than the deployment's own.

        Returns:
            The path of the waiting run's file.
        """
        return self.home / ("waiting-run.json" if project else "run-log.json")

    @property
    def sessions(self) -> Path:
        """Where a pooled session keeps its own draft and its own waiting run, one file each per id."""
        return self.home / "sessions"

    @property
    def model_tiers(self) -> Path:
        """What the owner set each named tier to run on."""
        return self.home / "model-tiers.json"

    @property
    def disabled_plugins(self) -> Path:
        """The plugins the owner turned off."""
        return self.home / "disabled-plugins.json"

    @property
    def selected_profile(self) -> Path:
        """The profile in effect, snapshotted at selection time."""
        return self.home / "selected-profile.json"

    def run_events(self, run_id: str) -> Path:
        """One run's own append-only event file.

        Args:
            run_id: The run.

        Returns:
            The path of the run's event file.
        """
        return self.home / "runs" / run_id / "events.jsonl"


class InspectionSettings(BaseModel):
    """What the inspection watches, what it knows about the tools, and how often it looks."""

    watches: tuple[Watch, ...] = Field(
        default=(), description="The standing rules: which runs get which rules, how far the inspector reads, and when."
    )
    world_changing: tuple[str, ...] = Field(
        default=(), description="Tools whose call changes the world, so the same call after one is not a duplicate."
    )
    result_cap_chars: int | None = Field(
        default=None, description="The return size past which a tool result can overflow the context. None never flags."
    )
    inspector_tiers: tuple[Tier, ...] = Field(
        default=("helper", "worker"), min_length=1, description="The tiers the inspector is asked on, cheapest first."
    )
    dispatch_seconds: int = Field(default=15, ge=1, description="How often the dispatch reads the record's events.")
    sweep_seconds: int = Field(default=3600, ge=60, description="How often all watches reconcile completed runs over their windows.")
    job_poll_seconds: float = Field(default=1.0, gt=0, description="Maximum idle wait before each independent inspection lane checks its jobs.")
    batch: int = Field(default=200, ge=1, description="How many events one dispatch pass reads.")

    def facts(self) -> ToolFacts:
        """The tool facts the rules take.

        Returns:
            The facts built from `world_changing` and `result_cap_chars`.
        """
        return ToolFacts(world_changing=frozenset(self.world_changing), result_cap_chars=self.result_cap_chars)


HARNESS_SETTINGS_JSON = CHECKOUT / "config" / "harness_settings.json"
"""The deployment's own defaults, committed: every section, so this app boots with no environment at all."""


class SkillHintSettings(BaseModel):
    """Whether the chat agent is told which skill a decision model picks for each turn, by which model, and how sure the pick must be."""

    enabled: bool = True
    model: str = "jev-1.13"
    """The name the System One API serves the decision model under."""
    threshold: float = Field(default=0.6, ge=0, le=1)
    """The least averaged probability a pick needs before the hint names it."""
    deadline_seconds: float = Field(default=2.0, gt=0, le=30)
    """How long the pick may hold back the turn's first model request."""


class _SectionOverrides(TypedDict, total=False):
    """The sections `HarnessSettings` accepts as init kwargs, each one optional."""

    run: RunSettings
    store: StoreSettings
    models: ModelSettings
    self_authoring: SelfAuthoringSettings
    terminal: TerminalSettings
    web_search: WebSearchSettings
    studio_api: StudioApiSettings
    knowledge_base_guidance: KnowledgeBaseGuidanceSettings
    library_search: LibrarySearchSettings
    state: StateSettings
    inspection: InspectionSettings
    skill_hint: SkillHintSettings
    logging: LoggingSettings


class HarnessSettings(BaseSettings):
    """Every knob this deployment sets, as one tree of sections.

    Sources, highest precedence first: init kwargs, `APP_*` environment variables with `__` between
    a section and its field (`APP_RUN__RUN_ENGINE`), then `config/harness_settings.json`. Every
    section is mandatory and the file carries all of them, so a process with no environment still
    builds. `nested_model_default_partial_update` lets one field be exported without redeclaring its
    whole section.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        json_file=HARNESS_SETTINGS_JSON,
        nested_model_default_partial_update=True,
        extra="ignore",
    )

    run: RunSettings
    store: StoreSettings
    models: ModelSettings
    self_authoring: SelfAuthoringSettings
    terminal: TerminalSettings
    web_search: WebSearchSettings
    studio_api: StudioApiSettings
    knowledge_base_guidance: KnowledgeBaseGuidanceSettings
    library_search: LibrarySearchSettings
    state: StateSettings
    inspection: InspectionSettings
    skill_hint: SkillHintSettings
    logging: LoggingSettings = Field(default_factory=lambda: LoggingSettings(service_name="harness"))
    """How this process names itself in its telemetry, how much it records, and whether it prints. Console stays off, because `harness acp` speaks over stdio."""

    def __init__(self, *, json_file: Path | None = None, **overrides: Unpack[_SectionOverrides]) -> None:
        """Build the tree, typing the `json_file` init seam.

        Args:
            json_file: The defaults file to read, `None` for the committed one. Popped in
                `settings_customise_sources`, never a field.
            **overrides: Explicit sections, each winning wholesale over both other sources.
        """
        super().__init__(json_file=json_file, **overrides)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the sources: init kwargs, then `APP_` environment variables, then the defaults file.

        Args:
            settings_cls: The settings class being built.
            init_settings: Values passed to `HarnessSettings(...)`.
            env_settings: Values from `APP_*`.
            dotenv_settings: Unused, this app reads no `.env`.
            file_secret_settings: Unused, this app reads no secrets directory.

        Returns:
            The sources in descending-precedence order.
        """
        del dotenv_settings, file_secret_settings
        named = getattr(init_settings, "init_kwargs", {}).pop("json_file", None)
        path = named if named is not None else HARNESS_SETTINGS_JSON
        return (init_settings, env_settings, JsonConfigSettingsSource(settings_cls, json_file=path))


def settings_file() -> Path:
    """The settings file to read: the gitignored one when it exists, else the committed example.

    The example names the local database and carries no keys, so a fresh checkout runs against the local
    Postgres, and whatever needs a key refuses its absence at use time. It is checked on every call.

    Returns:
        The path to read.
    """
    return ENGRAM_SETTINGS if ENGRAM_SETTINGS.is_file() else ENGRAM_SETTINGS_EXAMPLE


def knowledge_base_guidance(knowledge_base: Path | None, floor: str) -> Callable[[], str | None]:
    """The knowledge base's guidance: an applied policy when there is one, else `floor`.

    The applied `_meta/active-policy.yaml` wins. It is read on every call, so a policy applied or rolled back while this
    process runs takes effect on the next request. `knowledge_base` may be any knowledge base this deployment reads, not
    only the one it mounts as its own.

    Args:
        knowledge_base: The knowledge base checkout's root, or `None` when this deployment has none on disk, in which
            case only `floor` can answer.
        floor: This deployment's own guidance, beneath any applied policy. Empty leaves a knowledge base with no
            applied policy only its own domain text.

    Returns:
        A callable answering the guidance for this request, or `None` when neither source has any.
    """
    authored = floor or None
    if knowledge_base is None:
        return lambda: authored
    applied = policy_instructions(knowledge_base)

    def guidance() -> str | None:
        return applied() or authored

    return guidance
