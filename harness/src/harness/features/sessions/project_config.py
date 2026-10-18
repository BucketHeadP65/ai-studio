"""Per-project configuration: `.aifactory/config.json` at a project root.

It holds the context policy, recall, skill directories, boot setup and memory policy. Nothing here writes the file.
A project edits it directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from core.observability import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from pathlib import Path

_logger = get_logger(__name__)


class CompactionConfig(BaseModel):
    """Per-project context policy the agent build reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: Literal["tiered", "provider-native"] = Field(default="tiered", description="Which compaction the agent mounts.")
    target_tokens: int = Field(default=120_000, description="Compact down to this size when a pass triggers.")
    summarizer_model: str | None = Field(default=None, description="Model for the summary tier. None rides the agent's own model.")
    recall_max_matches: int = Field(default=3, description="Result cap for search_conversation_history.")


class SetupConfig(BaseModel):
    """What the agent opens this project with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str | None = Field(default=None, description="Profile to select at boot when none is selected. None leaves the plugins alone.")
    model: str | None = Field(default=None, description="Model to put the orchestrator tier on at boot. None keeps the default.")


class MemoryConfig(BaseModel):
    """Per-project policy for the agent's standing memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    global_memory: bool = Field(default=True, description="Whether the agent's standing memory is in play for this project.")


class ProjectConfig(BaseModel):
    """The contents of `.aifactory/config.json` at a project root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = Field(default=None, description="Display name. None falls back to the folder name.")
    skills: tuple[str, ...] = Field(default=(), description="Skill-library directories, relative to the project root.")
    compaction: CompactionConfig = Field(default_factory=CompactionConfig, description="Context policy.")
    setup: SetupConfig = Field(default_factory=SetupConfig, description="Boot-time setup.")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Standing-memory policy.")


def project_config(root: Path) -> ProjectConfig:
    """A project's config, read fresh on every call.

    An invalid file is logged as a warning and answered like a missing one.

    Args:
        root: The project's folder.

    Returns:
        The config, or all defaults when `.aifactory/config.json` is absent, unreadable or invalid.
    """
    path = root / ".aifactory" / "config.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ProjectConfig()
    try:
        return ProjectConfig.model_validate_json(raw)
    except ValidationError as exc:
        _logger.warning("project config at {path} is invalid, falling back to defaults: {reason}", path=str(path), reason=str(exc))
        return ProjectConfig()

