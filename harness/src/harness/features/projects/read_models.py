"""What the projects slice's reads return: a node of a file tree, an annotated file's note counts and a skill's card."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FileEntry(BaseModel):
    """One node of a project's file tree."""

    path: str = Field(description="Root-relative path, forward-slash separated.")
    kind: Literal["dir", "file"] = Field(description="Whether this node is a directory or a file.")
    size: int = Field(description="Byte size for a file. 0 for a directory.")


class FileNoteSummary(BaseModel):
    """One annotated file's note aggregate: counts by status, and its most recent agent reply."""

    path: str = Field(description="Root-relative path of the annotated file.")
    open_notes: int = Field(description="Count of notes whose thread is open.")
    resolved_notes: int = Field(description="Count of notes whose thread is resolved.")
    latest_agent_reply_at: datetime | None = Field(
        default=None, description="The most recent agent reply across every note on this file. `None` when it has none."
    )


class SkillCard(BaseModel):
    """One discovered skill, valid or not."""

    name: str = Field(description="Skill name from frontmatter or directory.")
    description: str = Field(default="", description="Frontmatter description. Empty when the skill is broken.")
    library: str = Field(description="Library directory, project-relative or 'builtin'.")
    path: str = Field(description="SKILL.md path relative to the project root. Builtin paths are absolute.")
    valid: bool = Field(description="Whether the vendored loader accepts it.")
    error: str | None = Field(default=None, description="The loader's complaint when invalid.")
