"""The settings tree: one `ApplicationSettings` composed of domain sections and an `infrastructure` section.

`infrastructure` holds every external system's endpoint and credential, `logging` how this app names itself in its
telemetry, and the other sections (`retrieval`, `ingestion`, `sources`, `evals`) hold policy, including which
deployments embed and summarise. The shapes engram
accepts are re-exported from `engram.config` and the infrastructure shapes from `core.settings`, so app code
imports every settings type from here. Sources, highest precedence first: init keyword arguments, `APP_*`
environment variables (`__` is the nested delimiter, so `APP_INFRASTRUCTURE__DATABASE__URL` sets
`infrastructure.database.url`),
then the gitignored `application_settings.json`, copied from the committed `application_settings.example.json`.
There is no `.env`. The knowledge base is never read from the file or the environment: a command names it with
`--knowledge-base`, or runs inside one.
"""

from __future__ import annotations

import json

from collections.abc import Mapping
from pathlib import Path
from typing import Literal, TypedDict, Unpack

from core.observability import LoggingSettings
from core.settings import (
    AzureOpenAiSettings,
    DatabaseSettings,
    DocumentIntelligenceSettings,
    GatewaySettings,
    HarnessServiceSettings,
    InfrastructureSettings,
    LogfireSettings,
    OpenRouterSettings,
)
from engram.config import (
    CoverageSettings,
    DbDialect,
    DifficultyThresholdsSettings,
    EvalsSettings,
    GenerationSettings,
    GraphBackend,
    IngestionSettings,
    KnowledgeBaseSettings,
    RecommendationSettings,
    RerankSettings,
    TuningSettings,
)
from engram.config import KnowledgeBaseEntry as _EngramKnowledgeBaseEntry
from engram.kb.contracts import KnowledgeBaseSchema
from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, TypeAdapter, ValidationError, model_validator
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, PydanticBaseSettingsSource, SettingsConfigDict

__all__ = [
    "ApplicationSettings",
    "AzureOpenAiSettings",
    "CoverageSettings",
    "DatabaseSettings",
    "DbDialect",
    "DifficultyThresholdsSettings",
    "DocumentIntelligenceSettings",
    "EvalsSettings",
    "GatewaySettings",
    "GenerationSettings",
    "GraphBackend",
    "HarnessServiceSettings",
    "InfrastructureSettings",
    "IngestionSettings",
    "KnowledgeBaseEntry",
    "KnowledgeBasePathOrigin",
    "KnowledgeBasePathSource",
    "KnowledgeBaseSettings",
    "LogfireSettings",
    "OpenRouterSettings",
    "RecommendationSettings",
    "RetrievalSettings",
    "Schedule",
    "SettingsFile",
    "SourceSettings",
    "StudioLoggingSettings",
    "TuningSettings",
    "default_settings_path",
    "fetch_application_settings",
    "find_knowledge_base_root",
    "is_valid_registry_row",
    "knowledge_base_slug",
    "read_registry",
]

# The repository root is this file's fifth parent, so the working directory never changes this path.
# The migrations read the same folder.
_CONFIG_DIR = Path(__file__).resolve().parents[4] / "engram" / "config"

_APPLICATION_SETTINGS_JSON = _CONFIG_DIR / "application_settings.json"
"""The gitignored settings file with machine paths and secrets, read at call time.

When it is absent, as on a fresh checkout, init arguments and `APP_*` environment variables alone build the settings.
"""


type KnowledgeBasePathSource = Literal["--knowledge-base", "working directory"]
"""Which source decided the active `knowledge_base.path`: an explicit flag, or the knowledge-base-schema.json in the working directory."""


class KnowledgeBasePathOrigin(BaseModel):
    """How a command resolved its knowledge base, registered in the container under its own type.

    It is never read from the settings file or the environment. A `dangling-code-path` validation message names it.
    """

    model_config = ConfigDict(frozen=True)

    source: KnowledgeBasePathSource = "working directory"


class SettingsFile(BaseModel):
    """The settings file the registry of knowledge bases is read from and written to.

    The knowledge-bases routes read this file again on every request, never the settings tree they were built with.
    """

    model_config = ConfigDict(frozen=True)

    path: Path


class Schedule(BaseModel):
    """A knowledge base's declared refresh cadence: exactly one of `every_seconds` or `daily_at`.

    `every_seconds` is at least 300. `daily_at` is a 24-hour `"HH:MM"` time read as UTC, and it fires at most once a day.
    """

    model_config = ConfigDict(frozen=True)

    every_seconds: int | None = Field(default=None, ge=300)
    """Seconds between refreshes. It must be at least 300."""
    daily_at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    """The UTC time of day to refresh, as `HH:MM` on a 24-hour clock."""

    @model_validator(mode="after")
    def _check_exactly_one(self) -> Schedule:
        if (self.every_seconds is None) == (self.daily_at is None):
            msg = "schedule must set exactly one of every_seconds or daily_at"
            raise ValueError(msg)
        return self


_OLD_PRIVACY_KEY = "library"
"""The key a registered knowledge base must not carry, whose meaning is the opposite of `private`."""

_OLD_PRIVACY_KEY_REFUSAL = (
    f"{_OLD_PRIVACY_KEY} is the name private carried before, and it means the opposite (false there is true here), "
    "so reading it as either answer would be a guess. Rewrite the settings file's knowledge base rows with "
    "`uv run python -m studio.migrations.wave4`."
)
"""What a caller is told when a registered knowledge base carries the `library` key."""


class KnowledgeBaseEntry(_EngramKnowledgeBaseEntry):
    """A registered knowledge base as engram declares it, plus two studio-only fields: a refresh `schedule` and `private`.

    `path` and `description` are validated as engram validates them, so a registered knowledge base always
    constructs as `KnowledgeBaseSettings`. `schedule` is when to refresh. `private: true` keeps the knowledge base
    out of a host's read over several knowledge bases, whatever its own `retrieval.private` says.
    """

    schedule: Schedule | None = None
    private: bool = False

    @model_validator(mode="before")
    @classmethod
    def _refuse_the_old_privacy_key(cls, data: object) -> object:
        """Refuse a row that carries a `library` key, so `read_registry` reports it as broken and never reads it as shared."""
        if isinstance(data, Mapping) and _OLD_PRIVACY_KEY in data:
            raise ValueError(_OLD_PRIVACY_KEY_REFUSAL)
        return data


_ENTRY_ADAPTER = TypeAdapter(KnowledgeBaseEntry)


def is_valid_registry_row(raw: JsonValue) -> bool:
    """Whether `raw` parses as a `KnowledgeBaseEntry`, the same check `read_registry` applies to each row.

    Args:
        raw: One unvalidated row of the `knowledge_bases` map.

    Returns:
        True when the row is valid.
    """
    try:
        _ENTRY_ADAPTER.validate_python(raw)
    except ValidationError:
        return False
    return True


def read_registry(settings_path: Path) -> tuple[dict[str, KnowledgeBaseEntry], dict[str, str | None]]:
    """Every row of the `knowledge_bases` map in `settings_path`, split into valid and broken.

    Each row is validated on its own, so one malformed row, such as a moved folder or a hand-edited shape, lands
    in `broken` and never fails the whole read.

    Args:
        settings_path: The settings file.

    Returns:
        The valid rows by name, and each broken row's name mapped to its raw `path` string, or `None` when that
        is missing or not a string.

    Raises:
        OSError: When the file cannot be read.
        ValueError: When the file is not valid JSON.
    """
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    valid: dict[str, KnowledgeBaseEntry] = {}
    broken: dict[str, str | None] = {}
    for name, raw in data.get("knowledge_bases", {}).items():
        try:
            valid[name] = _ENTRY_ADAPTER.validate_python(raw)
        except ValidationError:
            raw_path = raw.get("path") if isinstance(raw, dict) else None
            broken[name] = raw_path if isinstance(raw_path, str) else None
    return valid, broken


class RetrievalSettings(BaseModel):
    """Retrieval policy: which BM25 and link-graph grammar the serve slice emits, never the connection.

    `dialect` defaults to `PG_TEXTSEARCH` (`pg_textsearch` BM25) and `graph_backend` to `AGE` (the Apache AGE multi-hop
    walk), and `APP_RETRIEVAL__DIALECT=pg_search` with `APP_RETRIEVAL__GRAPH_BACKEND=cte` selects ParadeDB.
    `retrieval_profile` names the MCP tool set the server exposes, one of `full`, `search-hybrid`, `search-keyword`,
    `graph-only`, `keyword-graph` or `code-mode`, and an unknown name is refused when the MCP server is built. Only
    `code-mode` adds the `run_code` tool, a Monty sandbox over the read tools. `agent_tunes_k` set to `False` pins
    the search hit cap at `k_default`, and the served `search` then has no `k` at all. `rerank` says how a decision model re-ranks each search's hits.
    """

    dialect: DbDialect = DbDialect.PG_TEXTSEARCH
    graph_backend: GraphBackend = GraphBackend.AGE
    retrieval_profile: str = "full"
    agent_tunes_k: bool = Field(
        default=True,
        description="Whether agents see and set the search hit cap `k` themselves. When False the host pins it at `k_default`.",
    )
    k_default: int = Field(
        default=10,
        ge=1,
        le=100,
        description="The pinned search hit cap used when agents do not tune `k`. It is ignored while `agent_tunes_k` is True.",
    )
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    """How search hits are re-ranked by a decision model."""


class SourceSettings(RootModel[dict[str, dict[str, JsonValue]]]):
    """Every connector's and every scanner's own settings, one block per name the consumer documents.

    Each block is raw JSON here, and the connector or scanner that reads it validates it with its own model when
    it builds. The whole mapping reaches the connectors, so a connector that needs settings gets them without a
    field of its own in the app. An environment variable such as `APP_SOURCES__XAI__API_KEY=...` sets one nested
    value and leaves the rest of the mapping as the file declared it.
    """


class StudioLoggingSettings(LoggingSettings):
    """This app's logging section, named `studio` unless the settings file says otherwise.

    `console` stays off by default, because `studio mcp` speaks over stdio.
    """

    service_name: str = "studio"


class _SettingsOverrides(TypedDict, total=False):
    """The sub-model overrides `ApplicationSettings` accepts, each optional, an omitted one coming from the settings file."""

    knowledge_bases: dict[str, KnowledgeBaseEntry]
    retrieval: RetrievalSettings
    ingestion: IngestionSettings
    sources: SourceSettings
    evals: EvalsSettings
    infrastructure: InfrastructureSettings
    logging: StudioLoggingSettings


class ApplicationSettings(BaseSettings):
    """The whole settings tree: the domain sections and the `infrastructure` section.

    Sources, highest precedence first: init arguments, `APP_*` environment variables (`__` is the nested
    delimiter), then the settings file. A variable such as `APP_RETRIEVAL__DIALECT` overrides one nested value
    without redeclaring its section. An explicit sub-instance such as `infrastructure=InfrastructureSettings(...)` wins over
    the file wholesale, and `json_file` points the file source at another file. Every section but `knowledge_bases`
    and `logging` is required, and a settings file leaving a required one out raises a `ValidationError` naming it. A
    knowledge base is not configuration: a command names the one it acts on and passes it to the container.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        json_file=_APPLICATION_SETTINGS_JSON,
        nested_model_default_partial_update=True,
        extra="ignore",
    )

    knowledge_bases: dict[str, KnowledgeBaseEntry] = {}
    retrieval: RetrievalSettings
    ingestion: IngestionSettings
    sources: SourceSettings
    evals: EvalsSettings
    infrastructure: InfrastructureSettings
    logging: StudioLoggingSettings = Field(default_factory=StudioLoggingSettings)
    """How this app names itself in its telemetry, how much it records, and whether it prints to the console."""

    def __init__(self, *, json_file: Path | None = None, **overrides: Unpack[_SettingsOverrides]) -> None:
        """Build the settings tree from its sources.

        Args:
            json_file: The settings file to read in place of the default. It is never a field. `None` reads
                `_APPLICATION_SETTINGS_JSON`, resolved at call time.
            **overrides: Explicit sub-model overrides (`knowledge_bases`, `retrieval`, `ingestion`, `sources`,
                `evals`, `infrastructure`, `logging`). An explicit sub-instance wins wholesale, and an omitted section comes from
                the settings file.
        """
        super().__init__(json_file=json_file, **overrides)

    @model_validator(mode="after")
    def _check_knowledge_bases(self) -> ApplicationSettings:
        """Refuse a registered knowledge base with a blank name, or two of them on the same path."""
        if not self.knowledge_bases:
            return self
        if any(not name for name in self.knowledge_bases):
            msg = "knowledge bases entries must have non-empty names"
            raise ValueError(msg)
        paths = [entry.path for entry in self.knowledge_bases.values()]
        if len(paths) != len(set(paths)):
            msg = "knowledge bases entries must have unique paths"
            raise ValueError(msg)
        return self

    def active_knowledge_base_name(self, knowledge_base: KnowledgeBaseSettings) -> str | None:
        """The registered name whose path is `knowledge_base.path`.

        Args:
            knowledge_base: The knowledge base a command acts on.

        Returns:
            The name, or `None` when no registered knowledge base has that path.
        """
        for name, entry in self.knowledge_bases.items():
            if entry.path == knowledge_base.path:
                return name
        return None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 (the app reads no .env)
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003 (the app reads no secrets directory)
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the sources: init arguments first, then `APP_*` environment variables, then the settings file.

        A `json_file` init argument points the file source at that file, and it is never a settings field. Without
        it, the source reads `_APPLICATION_SETTINGS_JSON`, resolved at call time.

        Args:
            settings_cls: The settings class being built.
            init_settings: Values from `ApplicationSettings(...)` init arguments.
            env_settings: Values from `APP_*` environment variables.
            dotenv_settings: Unused, the app reads no `.env`.
            file_secret_settings: Values from the secrets directory, unused.

        Returns:
            The sources in descending-precedence order.
        """
        json_file = getattr(init_settings, "init_kwargs", {}).pop("json_file", None)
        json_path = json_file if json_file is not None else _APPLICATION_SETTINGS_JSON
        return (init_settings, env_settings, JsonConfigSettingsSource(settings_cls, json_file=json_path))


def default_settings_path() -> Path:
    """The path of the gitignored settings file.

    A caller that both reads and writes the settings file targets this path, and a test passes its own path instead.

    Returns:
        The path of `_APPLICATION_SETTINGS_JSON`.
    """
    return _APPLICATION_SETTINGS_JSON


def fetch_application_settings(*, json_file: Path | None = None) -> ApplicationSettings:
    """Build a fresh `ApplicationSettings` from the settings file plus `APP_*`.

    A fresh tree on every call (no cache). The knowledge base a command acts on is not here: the
    command resolves it and hands it to `create_container`.

    Args:
        json_file: Optional override path for the single `JsonConfigSettingsSource`, threaded
            through for hermetic callers. `None` falls back to `_APPLICATION_SETTINGS_JSON`.

    Returns:
        A freshly built `ApplicationSettings` instance.
    """
    return ApplicationSettings(json_file=json_file)


def find_knowledge_base_root(start: Path) -> Path | None:
    """The nearest folder at or above `start` that holds a `knowledge-base-schema.json`.

    It walks up only, so a knowledge base nested below `start` is never found.

    Args:
        start: The folder to start from.

    Returns:
        The knowledge base root, or `None` when the filesystem root is reached first.
    """
    current = start.resolve()
    while True:
        if (current / "knowledge-base-schema.json").is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent


def knowledge_base_slug(root: Path) -> str:
    """The slug `root`'s schema declares, or the folder name when there is no schema or it does not parse.

    It never raises on a broken schema, so a refusal can always name the knowledge base.

    Args:
        root: The knowledge base root.

    Returns:
        The slug, or the folder name.
    """
    candidate = root / "knowledge-base-schema.json"
    if not candidate.is_file():
        return root.name
    try:
        return KnowledgeBaseSchema.from_path(candidate).slug
    except (ValidationError, ValueError, OSError):
        return root.name
