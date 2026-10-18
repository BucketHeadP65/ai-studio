"""Template resolution: named starting points for a new knowledge base, each a directory under `templates/`.

A template is `templates/<slug>/` in the monorepo, or an external checkout named by the
`ENGRAM_TEMPLATES` environment variable: `knowledge-base-schema.json`, `_meta/*.md` guidance, and the
workflows a knowledge base built from it can import. Copying a template's files into a fresh
knowledge base folder is the caller's job. This module only resolves which template a slug names.
"""

from __future__ import annotations

import os

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class Template:
    """One resolved template: its slug, display title (the README's first heading), and directory."""

    slug: str
    title: str
    path: Path


def templates_root(environ: Mapping[str, str] = os.environ) -> Path:
    """The directory holding every template: `ENGRAM_TEMPLATES` when set, else the repo's `templates/`.

    Without an override, it answers `<repo root>/templates`, where the repo root is the first ancestor
    of this file carrying a `.git` entry. A checkout with no `.git` up the tree, or no `templates/` at
    the repo root, answers a path that does not exist. `list_templates` reports that as no templates.

    Args:
        environ: The environment to read `ENGRAM_TEMPLATES` from.

    Returns:
        The templates root directory (may not exist).
    """
    override = environ.get("ENGRAM_TEMPLATES")
    if override:
        return Path(override)
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate / "templates"
    return here.parents[-1] / "templates"


def list_templates(environ: Mapping[str, str] = os.environ) -> tuple[Template, ...]:
    """Every template under `templates_root(environ)`, sorted by slug.

    A subdirectory counts as a template only once it carries its own `knowledge-base-schema.json`. Any other
    entry (a stray file, a directory mid-authoring) is silently skipped.

    Args:
        environ: The environment to read `ENGRAM_TEMPLATES` from.

    Returns:
        The known templates, alphabetical by slug.
    """
    root = templates_root(environ)
    if not root.is_dir():
        return ()
    return tuple(
        Template(slug=entry.name, title=_template_title(entry), path=entry)
        for entry in sorted(root.iterdir())
        if entry.is_dir() and (entry / "knowledge-base-schema.json").is_file()
    )


def load_template(slug: str, environ: Mapping[str, str] = os.environ) -> Template:
    """The template named `slug`.

    Args:
        slug: The template's directory name under `templates_root(environ)`.
        environ: The environment to read `ENGRAM_TEMPLATES` from.

    Returns:
        The resolved `Template`.

    Raises:
        LookupError: No template is named `slug`. The message names the known slugs.
    """
    templates = list_templates(environ)
    for template in templates:
        if template.slug == slug:
            return template
    known = ", ".join(sorted(b.slug for b in templates)) or "none"
    msg = f"no template named {slug!r}. Known templates are {known}."
    raise LookupError(msg)


def _template_title(template_dir: Path) -> str:
    """The template's display title: its `README.md`'s first `# ` heading, or its slug."""
    readme = template_dir / "README.md"
    if readme.is_file():
        for line in readme.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    return template_dir.name
