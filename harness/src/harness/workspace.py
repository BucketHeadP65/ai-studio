"""One project's own home for durable process state, which holds its session log.

Every project a session opens against gets its own `<root>/.aifactory/state/`, holding the log of that session's turns.
The harness checkout's own `.state/` home is separate, and this module never touches it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harness.adapters.contracts import SessionLog
from harness.settings import StateSettings, aifactory_state

if TYPE_CHECKING:
    from pathlib import Path

    from core.cqrs.contracts import TopicSink


@dataclass
class Workspace:
    """One project's on-disk state: where it lives, and the handles open on it."""

    root: Path
    """The project's own checkout root. A step's shell is confined to it."""
    state: StateSettings
    """The home at `root/.aifactory/state`, and every path this project writes under it."""
    session_log: SessionLog


def open_workspace(root: Path, *, topics: TopicSink | None = None) -> Workspace:
    """Open `root`'s `.aifactory/state/` home, creating and gitignoring it as needed.

    A second call over the same root creates nothing new.
    It adds the `state/` line to `.aifactory/.gitignore` only when the line is missing, and leaves the rest of that file as it was.

    Args:
        root: The project's checkout root.
        topics: The caller's installed change channel, so this workspace's session log announces changes the same way the
            checkout's own does. `None` for a caller with no channel to give, such as a read-only sweep or a bare registration.

    Returns:
        The workspace, with its session log open.

    Raises:
        OSError: The state home cannot be created, or `.aifactory/.gitignore` cannot be read or written.
    """
    state = StateSettings(home=aifactory_state(root))
    state.home.mkdir(parents=True, exist_ok=True)
    _ensure_state_gitignored(root)
    return Workspace(
        root=root,
        state=state,
        session_log=SessionLog(state.session_log, forgotten_marker=state.forgotten_marker, topics=topics),
    )


def _ensure_state_gitignored(root: Path) -> None:
    """Add `state/` to `root/.aifactory/.gitignore`, once, without disturbing anything else there."""
    gitignore = root / ".aifactory" / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if "state/" in existing.splitlines():
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{separator}state/\n", encoding="utf-8")
