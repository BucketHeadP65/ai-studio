"""A project's skill directories: the catalog, a skill's body, and a new skill's scaffold.

A project names its skill directories in its configuration, relative to its root. The catalog adds the harness's own
(`harness.settings.SKILLS`) and engram's own (`engram.skills.contracts.SKILLS`). A configured directory that is not
there is skipped with a warning, so a stale entry costs that one directory and never the whole listing. A skill whose
`SKILL.md` fails to load stays in the catalog as invalid, carrying the loader's complaint.
"""

from __future__ import annotations

import os

from pathlib import Path
from typing import TYPE_CHECKING

from core.files import atomic_write
from core.observability import get_logger
from engram.skills.contracts import SKILLS as ENGRAM_SKILLS
from pydantic_ai_harness.skills._loader import parse_skill

from harness.features.projects.domain import short_content_hash
from harness.features.projects.infrastructure.project_files import resolve_in_project
from harness.features.projects.read_models import SkillCard
from harness.settings import SKILLS

if TYPE_CHECKING:
    from harness.features.projects.internal_contracts import SkillDirectories

_logger = get_logger(__name__)

_STUB = """---
description: What this skill is for and when to use it.
---

Describe the skill's instructions here.
"""

BUILTIN_SKILL_DIRECTORY = "builtin"
"""The `library` value that names the harness's own skill directory, which is read-only."""
ENGRAM_SKILL_DIRECTORY = "engram"
"""The `library` value that names engram's own skill directory."""


def catalog(root: Path, config: SkillDirectories) -> list[SkillCard]:
    """Every skill in the project's configured skill directories, the harness's own and engram's own.

    Args:
        root: The project root.
        config: The project's configuration.

    Returns:
        The cards: each configured directory's in its configured order, then the harness's, then engram's.
    """
    cards: list[SkillCard] = []
    for rel in config.skills:
        cards.extend(_library_cards(root / rel, library=rel, root=root))
    cards.extend(_library_cards(SKILLS, library=BUILTIN_SKILL_DIRECTORY, root=None))
    cards.extend(_library_cards(ENGRAM_SKILLS, library=ENGRAM_SKILL_DIRECTORY, root=None))
    return cards


def read_body(root: Path, library: str, name: str) -> tuple[str, str, bool]:
    """`<library>/<name>/SKILL.md`'s full content, its hash, and whether `write_body` can save it back.

    A `library` of `"builtin"` reads the harness's own skill directory and is always read-only. Any other value is
    resolved against `root`, and a `library` or `name` that escapes it is refused.

    Args:
        root: The project root.
        library: The skill directory, project-relative or `"builtin"`.
        name: The skill's directory name.

    Returns:
        The content, its hash, and whether it can be saved back.

    Raises:
        PermissionError: The resolved location escapes `root`.
        FileNotFoundError: No `SKILL.md` is there.
    """
    if library == BUILTIN_SKILL_DIRECTORY:
        skill_file = SKILLS / name / "SKILL.md"
        if not skill_file.is_file():
            msg = f"no builtin skill named {name!r}"
            raise FileNotFoundError(msg)
        content = skill_file.read_text(encoding="utf-8")
        return content, short_content_hash(content), False
    skill_file = resolve_in_project(root, f"{library}/{name}/SKILL.md")
    if not skill_file.is_file():
        msg = f"no skill at {library}/{name}"
        raise FileNotFoundError(msg)
    content = skill_file.read_text(encoding="utf-8")
    return content, short_content_hash(content), True


def write_body(root: Path, library: str, name: str, content: str, expected_hash: str | None) -> str:
    """Write `<library>/<name>/SKILL.md`'s content, refusing a stale `expected_hash`.

    `library` must be one of the project's configured skill directories. The caller refuses `"builtin"` before calling,
    as for `scaffold`.

    Args:
        root: The project root.
        library: The configured skill directory, project-relative.
        name: The skill's directory name.
        content: The skill's new full content.
        expected_hash: The hash the file must have now, or `None` to write unconditionally.

    Returns:
        The new content hash.

    Raises:
        PermissionError: The resolved location escapes `root`.
        FileNotFoundError: The skill's directory does not exist.
        ValueError: `expected_hash` is given and does not match the file's current hash.
    """
    skill_file = resolve_in_project(root, f"{library}/{name}/SKILL.md")
    if not skill_file.parent.is_dir():
        msg = f"no skill directory at {library}/{name}"
        raise FileNotFoundError(msg)
    if expected_hash is not None and skill_file.is_file():
        current = skill_file.read_text(encoding="utf-8")
        if short_content_hash(current) != expected_hash:
            msg = f"skill {library}/{name} has changed since it was read"
            raise ValueError(msg)
    atomic_write(skill_file, content)
    return short_content_hash(content)


def scaffold(root: Path, library: str, name: str) -> str:
    """Create `<library>/<name>/SKILL.md` under `root` as a minimal valid stub.

    Args:
        root: The project root.
        library: The configured skill directory, project-relative.
        name: The new skill's directory name.

    Returns:
        The new `SKILL.md`'s path relative to the project root.

    Raises:
        PermissionError: `library` or `name` resolves outside `root`.
        FileExistsError: A skill already exists at `<library>/<name>`.
    """
    skill_dir = resolve_in_project(root, f"{library}/{name}")
    if skill_dir.exists():
        msg = f"a skill already exists at {library}/{name}"
        raise FileExistsError(msg)
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(_STUB, encoding="utf-8")
    return skill_file.relative_to(Path(os.path.realpath(root))).as_posix()


def _library_cards(directory: Path, *, library: str, root: Path | None) -> list[SkillCard]:
    """Every skill under one skill directory, or `[]` when the directory does not exist."""
    if not directory.is_dir():
        if root is not None:
            _logger.warning("project config names a skills directory that does not exist: {path}", path=str(directory))
        return []
    real_root = Path(os.path.realpath(root)) if root is not None else None
    cards: list[SkillCard] = []
    for child in sorted(directory.iterdir(), key=lambda entry: entry.name):
        if not child.is_dir():
            continue
        skill_file = child / "SKILL.md"
        if skill_file.is_file():
            cards.append(_card(skill_file, library=library, real_root=real_root))
    return cards


def _card(skill_file: Path, *, library: str, real_root: Path | None) -> SkillCard:
    """One skill's card, valid with its parsed name and description, or invalid with the loader's complaint."""
    path = Path(os.path.realpath(skill_file)).relative_to(real_root).as_posix() if real_root is not None else str(skill_file)
    try:
        definition = parse_skill(skill_file.read_text(encoding="utf-8"), str(skill_file))
    except (OSError, ValueError) as exc:
        return SkillCard(name=skill_file.parent.name, description="", library=library, path=path, valid=False, error=str(exc))
    return SkillCard(name=definition.name, description=definition.description, library=library, path=path, valid=True, error=None)
