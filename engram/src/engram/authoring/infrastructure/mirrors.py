"""The authoring slice's mirror reader: the text of one source mirror, found by its source key."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.contracts import MirrorDoc

if TYPE_CHECKING:
    from pathlib import Path

MIRRORS_FOLDER = "Sources"
"""The folder under a knowledge base root that holds its source mirrors, the index's own default."""

_FENCE = "---"


def _frontmatter_head(path: Path) -> str:
    """The frontmatter of the file at `path`, read line by line up to its closing fence, empty when it opens with none."""
    lines: list[str] = []
    with path.open(encoding="utf-8") as handle:
        if handle.readline().strip() != _FENCE:
            return ""
        for line in handle:
            if line.strip() == _FENCE:
                break
            lines.append(line)
    return "".join(lines)


def mirror_text(knowledge_base_path: Path, source_key: str) -> str | None:
    """The whole text of the mirror directly under `Sources/` whose source key is `source_key`, or None when none is.

    A mirror's key is its frontmatter `source_key`, else its file name stem, the rule the index applies.
    Folders under `Sources/` are never read, the same as the index's quote check. Only a file whose
    frontmatter or stem mentions the key is read whole. A mirror that declares no `provider` is skipped, as
    the index skips it, and so is a file that cannot be read or decoded.

    Args:
        knowledge_base_path: The knowledge base root.
        source_key: The key a page names its mirror by.

    Returns:
        The mirror's text, frontmatter included, or None.
    """
    folder = knowledge_base_path / MIRRORS_FOLDER
    if not source_key or not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.md")):
        try:
            if not path.is_file() or (path.stem != source_key and source_key not in _frontmatter_head(path)):
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        mirror = MirrorDoc.from_text(path.relative_to(knowledge_base_path).as_posix(), text, "")
        if mirror.provider and mirror.source_key == source_key:
            return text
    return None
