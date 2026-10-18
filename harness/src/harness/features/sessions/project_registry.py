"""The registry of projects: which project folders this harness knows, and where they live.

It is an append-only log with two log entries, one when a folder joins and one when it leaves. Leaving never touches a
project's files or its projected rows. It only stops the folder from being offered again.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from harness.features.sessions.domain import ProjectRegistered, ProjectUnregistered
from harness.features.sessions.infrastructure.projects_log import ProjectsLog
from harness.features.sessions.read_models import RegisteredProject
from harness.features.sessions.service import project_id_of
from harness.workspace import open_workspace

if TYPE_CHECKING:
    from pathlib import Path


_REGISTRY: ProjectsLog | None = None
"""Set once by the composition root. One process, one registry of projects."""


def install(log: ProjectsLog) -> None:
    """Install the registry of projects this process reads and writes. Only the composition root calls this.

    Args:
        log: The registry of projects' own file.
    """
    global _REGISTRY  # noqa: PLW0603 (one process holds one registry of projects, which the composition root sets)
    _REGISTRY = log


def current() -> ProjectsLog:
    """The installed registry of projects, never a silently empty one.

    Returns:
        The installed log.

    Raises:
        RuntimeError: when nothing installed one.
    """
    if _REGISTRY is None:  # pragma: no cover (the lifespan always runs in practice)
        msg = "project registry not installed"
        raise RuntimeError(msg)
    return _REGISTRY


def registered_projects() -> tuple[RegisteredProject, ...]:
    """Every project currently on the registry of projects, folded from its whole history.

    Returns:
        The projects on it now.
    """
    live: dict[str, str] = {}
    for event in current().events():
        match event:
            case ProjectRegistered(project_id=project_id, root_path=root_path):
                live[project_id] = root_path
            case ProjectUnregistered(project_id=project_id):
                live.pop(project_id, None)
    return tuple(RegisteredProject(project_id=project_id, root_path=root_path) for project_id, root_path in live.items())


def _config_path(root: Path) -> Path:
    """Where `root`'s project config lives."""
    return root / ".aifactory" / "config.json"


def _ensure_default_config(root: Path) -> None:
    """Drop an empty (all-defaults) config file at `root`, unless one is already there."""
    path = _config_path(root)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")


def display_name(root: Path) -> str:
    """A project's display name.

    Args:
        root: The project's folder.

    Returns:
        The name in its config, or its folder name when none is set or the config cannot be read.
    """
    try:
        payload = json.loads(_config_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    name = payload.get("name") if isinstance(payload, dict) else None
    return name if isinstance(name, str) and name else root.name


def set_display_name(root: Path, name: str) -> None:
    """Write a project's display name into its config, keeping whatever else is already there.

    Args:
        root: The project's folder.
        name: The new display name.
    """
    path = _config_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["name"] = name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def register(root: Path, *, project_id: str) -> RegisteredProject:
    """Open a folder's workspace, drop a default config when it has none, and put it on the registry of projects.

    A project already on it is returned as it is, and nothing is recorded a second time.

    Args:
        root: The project's folder.
        project_id: The project's id.

    Returns:
        The project as the registry of projects holds it.
    """
    open_workspace(root)
    _ensure_default_config(root)
    resolved = str(root.resolve())
    existing = next((project for project in registered_projects() if project.project_id == project_id), None)
    if existing is not None:
        return existing
    current().record(ProjectRegistered(project_id=project_id, root_path=resolved))
    return RegisteredProject(project_id=project_id, root_path=resolved)


def unregister(project_id: str) -> None:
    """Record that a project left the registry of projects. Its files and projected rows are untouched.

    Args:
        project_id: The project's id.
    """
    current().record(ProjectUnregistered(project_id=project_id))


def auto_register_checkout(checkout: Path) -> None:
    """Put the harness checkout itself on the registry of projects when it is not there yet.

    The checkout keeps its own state home, so this gives it a config file and a place on the registry of projects
    without ever opening a project workspace on it.

    Args:
        checkout: The harness checkout.
    """
    project_id = project_id_of(checkout)
    if any(existing.project_id == project_id for existing in registered_projects()):
        return
    _ensure_default_config(checkout)
    current().record(ProjectRegistered(project_id=project_id, root_path=str(checkout.resolve())))


def boot(checkout: Path) -> None:
    """Install the registry of projects over the checkout's own file, and put the checkout on it.

    Args:
        checkout: The harness checkout.
    """
    install(ProjectsLog(checkout / ".state" / "projects-log.jsonl"))
    auto_register_checkout(checkout)
