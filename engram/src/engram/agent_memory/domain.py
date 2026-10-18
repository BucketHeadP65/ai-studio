"""The rules that turn a store path and its content into a memory page.

A store path is `{namespace}/{agent}/{category}/{slug}.md`. The category is always the second-to-last segment
and must be one of `CATEGORIES`. Content is markdown with an optional YAML frontmatter block, of which `source`
and `summary` are read. The title is the first `# ` heading, else the slug. The summary is the frontmatter's,
else the first line that is neither empty nor a heading.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.markdown import split_frontmatter

if TYPE_CHECKING:
    from collections.abc import Sequence

INDEX_FILENAME = "MEMORY.md"
"""The file name of a scope's index, which is rendered from the notes and never stored."""
CATEGORIES = ("world", "project", "reference")
"""The categories a note is filed under, in order. A store path names one as its second-to-last segment."""
SOURCES = ("owner-note", "session", "dream")
"""The values a note's frontmatter `source` may take."""
_ID_PREFIX = "mem."
_SUMMARY_LIMIT = 200
_MIN_PATH_SEGMENTS = 2


@dataclass(frozen=True)
class NotePieces:
    """What a memory page is built from, derived from one store path and its content."""

    page_id: str
    """The page id, from `page_id_of` over the store path."""
    category: str
    """One of `CATEGORIES`."""
    title: str
    """The first `# ` heading, else the slug."""
    summary: str
    """The frontmatter summary, else the first plain line, cut to 200 characters."""
    source: str | None
    """One of `SOURCES`, or `None` when the frontmatter names none."""
    body: str
    """The whole content, frontmatter included."""


def page_id_of(path: str) -> str:
    """Return the page id for a store path: slashes become dots under the `mem.` prefix.

    It is the exact inverse of `path_of` only over paths whose segments hold no dot. `derive` refuses a dotted
    segment before a path reaches the store.

    Args:
        path: A store path, or a store path prefix such as `"global/main/"`.

    Returns:
        The page id, or the page id prefix for a path prefix.
    """
    return _ID_PREFIX + path.removesuffix(".md").replace("/", ".")


def path_of(page_id: str) -> str:
    """Return the store path for a page id, the exact inverse of `page_id_of` over dot-free paths.

    Args:
        page_id: A page id built by `page_id_of`.

    Returns:
        The store path, ending in `.md`.
    """
    return page_id.removeprefix(_ID_PREFIX).replace(".", "/") + ".md"


def is_index(path: str) -> bool:
    """Say whether a store path names a scope's index file.

    Args:
        path: A store path.

    Returns:
        True when the last segment is `MEMORY.md`.
    """
    return path.rsplit("/", 1)[-1] == INDEX_FILENAME


def derive(path: str, content: str) -> NotePieces:
    """Derive the pieces of a memory page from a store path and its content.

    Args:
        path: The store path, `{namespace}/{agent}/{category}/{slug}.md`.
        content: The note's markdown, with optional frontmatter.

    Returns:
        The page id, category, title, summary, source and body.

    Raises:
        ValueError: The path has no known category segment, a segment other than the `.md` extension holds a dot,
            or the frontmatter names a `source` outside `SOURCES`. The message says how to fix it.
    """
    segments = path.split("/")
    if len(segments) < _MIN_PATH_SEGMENTS or segments[-2] not in CATEGORIES:
        msg = f"memory paths end in <category>/<slug>.md where category is one of: {', '.join(CATEGORIES)}. Got {path!r}"
        raise ValueError(msg)
    stripped_segments = [*segments[:-1], segments[-1].removesuffix(".md")]
    if any("." in segment for segment in stripped_segments):
        msg = f"memory path segments cannot contain dots (they map to the page-id separator). Use hyphens instead: {path!r}"
        raise ValueError(msg)
    category = segments[-2]
    front, body_text = split_frontmatter(content)
    source = value if isinstance(value := front.get("source"), str) else None
    if source is not None and source not in SOURCES:
        msg = f"memory note source must be one of: {', '.join(SOURCES)}. Got {source!r}"
        raise ValueError(msg)
    lines = body_text.splitlines()
    title = next((line.removeprefix("# ").strip() for line in lines if line.startswith("# ")), "")
    if not title:
        title = segments[-1].removesuffix(".md")
    summary = value if isinstance(value := front.get("summary"), str) else ""
    if not summary:
        summary = next((line.strip() for line in lines if line.strip() and not line.startswith("#")), title)
    return NotePieces(
        page_id=page_id_of(path),
        category=category,
        title=title,
        summary=summary[:_SUMMARY_LIMIT],
        source=source,
        body=content,
    )


def render_index(entries: Sequence[tuple[str, str, str]]) -> str:
    """Render a scope's index: one markdown line per live note.

    Args:
        entries: `(path, title, summary)` for each note, in the order they are listed.

    Returns:
        The markdown lines joined by newlines.
    """
    return "\n".join(f"- [{title}]({path}): {summary}" for path, title, summary in entries)
