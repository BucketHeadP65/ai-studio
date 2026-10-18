"""The log entries of the registry of projects."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _RegistryEvent(BaseModel):
    """A log entry of the registry of projects, frozen and closed: an unknown key makes a different entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectRegistered(_RegistryEvent):
    """A project folder joined the registry of projects."""

    kind: Literal["project_registered"] = "project_registered"
    project_id: str
    root_path: str


class ProjectUnregistered(_RegistryEvent):
    """A project folder left the registry of projects. Its files and projected rows are untouched."""

    kind: Literal["project_unregistered"] = "project_unregistered"
    project_id: str


RegistryEvent = Annotated[ProjectRegistered | ProjectUnregistered, Field(discriminator="kind")]
"""One log entry of the registry of projects, told apart by its `kind`."""


class RegistryRecord(BaseModel):
    """One line of the registry of projects: the log entry, and when it landed."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    event: RegistryEvent
