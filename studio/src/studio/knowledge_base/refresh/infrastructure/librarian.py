"""The librarian pass: after a sync, an agent may move each newly landed mirror into a folder under `Sources/`.

It runs only when the schema sets `sources.librarian`, between the sync and the index.
The agent sees the existing folders and the mirror's title and item type, and answers a folder or nothing.
A failed answer, a folder outside `Sources/`, a taken name or a failed move leaves the file where the sync put it.
Every failure is logged and never raised, and the pass never deletes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from core.ai import create_azure_model
from core.markdown import split_frontmatter
from core.observability import get_logger
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from studio.knowledge_base.settings import AzureOpenAiSettings, IngestionSettings

__all__ = ["LibrarianAgent", "LibrarianPlacement", "LibrarianPlacer", "LibrarianReport", "NewMirror", "librarian_agent", "new_mirrors_at"]

_logger = get_logger(__name__)

_ARCHIVE_DIR = "_archive"
"""The folder of archived mirrors, which the librarian never lists and never moves a mirror into."""

_INSTRUCTIONS = (
    "You place one newly landed source mirror into a knowledge base's Sources/ folder tree for a human who "
    "browses it in Obsidian. You are given the existing folder names under Sources/ and the new "
    "mirror's title and kind. Answer the destination folder as a path relative to Sources/, using "
    "'/' between levels (for example 'Papers/NLP'). Reuse an existing folder when the mirror clearly "
    "belongs there, name a new one when it does not fit any of them, or answer an empty string to "
    "leave the mirror where it landed."
)


class LibrarianPlacement(BaseModel):
    """One mirror's destination: a `Sources/`-relative folder path, empty meaning "leave it in place"."""

    model_config = ConfigDict(frozen=True)

    folder: str = ""


LibrarianAgent = Agent[None, LibrarianPlacement]
"""The placement agent's type: no dependencies, one `LibrarianPlacement` answer."""


def librarian_agent(azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> LibrarianAgent:
    """Build the placement agent over the Azure chat deployment the summary stage uses.

    Args:
        azure_openai: The Azure OpenAI resource the chat deployment runs on.
        ingestion: The settings that name the summary model.

    Returns:
        The agent, named `librarian`.
    """
    model = create_azure_model(ingestion.summary_model, endpoint=azure_openai.endpoint)
    return Agent(model, output_type=LibrarianPlacement, instructions=_INSTRUCTIONS, name="librarian")


@dataclass(frozen=True, slots=True)
class NewMirror:
    """One mirror file at the top of `Sources/`, waiting for the agent's answer."""

    path: Path
    title: str
    item_type: str
    """The mirror's `item_type` frontmatter value."""


@dataclass(frozen=True, slots=True)
class LibrarianReport:
    """What one placement pass did: the new paths of the mirrors it moved, and the file names it failed to move."""

    moved: tuple[Path, ...] = ()
    left: tuple[str, ...] = ()


class LibrarianPlacer:
    """Asks the placement agent where each new mirror goes and moves it there. `place` never raises."""

    def __init__(self, agent_factory: Callable[[], LibrarianAgent]) -> None:
        """Hold what builds the agent.

        Args:
            agent_factory: Builds one agent per pass.
        """
        self._agent_factory = agent_factory

    async def place(self, sources_dir: Path, new_mirrors: Sequence[NewMirror]) -> LibrarianReport:
        """Ask for and apply a folder for each of `new_mirrors`, one after another.

        The folder tree is read again before each mirror, so a folder one mirror created is offered to the next.
        A mirror the agent leaves where it is appears in neither `moved` nor `left`.
        A mirror that fails to move is listed in `left`, and the rest are still tried.

        Args:
            sources_dir: The knowledge base's `Sources/` folder.
            new_mirrors: The mirrors the sync just wrote at its top level.

        Returns:
            The mirrors moved and the file names left behind.
        """
        if not new_mirrors:
            return LibrarianReport()
        agent = self._agent_factory()
        moved: list[Path] = []
        left: list[str] = []
        for mirror in new_mirrors:
            folder = await self._decide(agent, sources_dir, mirror)
            if folder is None:
                left.append(mirror.path.name)
                continue
            if not folder:
                continue
            destination = self._apply(sources_dir, folder, mirror)
            if destination is None:
                left.append(mirror.path.name)
                continue
            moved.append(destination)
        return LibrarianReport(moved=tuple(moved), left=tuple(left))

    def _apply(self, sources_dir: Path, folder: str, mirror: NewMirror) -> Path | None:
        """Move `mirror` under `folder`, `None` when the folder is refused, the name is taken or the move fails."""
        try:
            destination = _resolve_destination(sources_dir, folder, mirror.path.name)
        except ValueError as error:
            _logger.warning("librarian: refusing destination for {mirror}: {reason}", mirror=mirror.path.name, reason=str(error))
            return None
        if destination.exists():
            _logger.warning("librarian: destination already occupied for {mirror}: {destination}", mirror=mirror.path.name, destination=str(destination))
            return None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            mirror.path.replace(destination)
        except OSError as error:
            _logger.warning("librarian: could not move {mirror} to {destination}: {reason}", mirror=mirror.path.name, destination=str(destination), reason=str(error))
            return None
        return destination

    async def _decide(self, agent: LibrarianAgent, sources_dir: Path, mirror: NewMirror) -> str | None:
        """The agent's `folder` answer, empty to leave the mirror in place, `None` when the agent failed."""
        prompt = (
            f"Existing folders under Sources/: {', '.join(_folder_tree(sources_dir)) or '(none yet)'}\n"
            f"New mirror title: {mirror.title}\n"
            f"Kind: {mirror.item_type}"
        )
        try:
            result = await agent.run(prompt)
        except Exception as error:  # noqa: BLE001 (an agent failure leaves the mirror in place, never fails the run)
            _logger.warning("librarian: placement agent failed for {mirror}", error=error, mirror=mirror.path.name)
            return None
        return result.output.folder


def _folder_tree(sources_dir: Path) -> list[str]:
    """Every folder under `sources_dir` except the archive, as sorted relative paths joined with `/`."""
    if not sources_dir.is_dir():
        return []
    return sorted(
        str(path.relative_to(sources_dir))
        for path in sources_dir.rglob("*")
        if path.is_dir() and _ARCHIVE_DIR not in path.relative_to(sources_dir).parts
    )


def _resolve_destination(sources_dir: Path, folder: str, filename: str) -> Path:
    """The mirror's path under `folder`, raising `ValueError` for any folder that leaves `sources_dir` or names the archive."""
    pure = PurePosixPath(folder.strip("/"))
    if not pure.parts or pure.is_absolute() or ".." in pure.parts or _ARCHIVE_DIR in pure.parts:
        msg = f"destination folder escapes Sources/ or names the archive: {folder!r}"
        raise ValueError(msg)
    destination = sources_dir / pure / filename
    if not destination.resolve().is_relative_to(sources_dir.resolve()):
        msg = f"destination folder escapes Sources/ through a symlink: {folder!r}"
        raise ValueError(msg)
    return destination


def new_mirrors_at(sources_dir: Path, filenames: Sequence[str]) -> tuple[NewMirror, ...]:
    """Read the `title` and `item_type` of each named file at the top of `sources_dir`.

    A file that is gone, cannot be read, or lacks either field is left out.

    Args:
        sources_dir: The knowledge base's `Sources/` folder.
        filenames: The file names to read.

    Returns:
        One mirror per file that could be read, in the order given.
    """
    mirrors: list[NewMirror] = []
    for name in filenames:
        path = sources_dir / name
        mirror = _new_mirror(path)
        if mirror is not None:
            mirrors.append(mirror)
    return tuple(mirrors)


def _new_mirror(path: Path) -> NewMirror | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        _logger.warning("librarian: could not read {path}: {reason}", path=str(path), reason=str(error))
        return None
    frontmatter, _ = split_frontmatter(text)
    title = frontmatter.get("title")
    item_type = frontmatter.get("item_type")
    if not isinstance(title, str) or not isinstance(item_type, str):
        return None
    return NewMirror(path=path, title=title, item_type=item_type)
