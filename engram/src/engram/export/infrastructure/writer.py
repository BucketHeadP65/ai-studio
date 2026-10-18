"""Write projected OKF concepts to disk as frontmattered UTF-8 Markdown files.

Each file is a `---` fenced YAML frontmatter block followed by the concept body, written through
`core.markdown.render_frontmatter`, so key order is kept and `split_frontmatter` reads it back.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from core.markdown import render_frontmatter

if TYPE_CHECKING:
    from engram.export.domain.models import OkfBundle, OkfConcept


class FilesystemBundleWriter:
    """Writes an `OkfBundle` to an export directory as frontmattered Markdown files.

    Every write overwrites the file already there, so an export replaces the export directory's files rather than merging into them.
    """

    def write_bundle(self, bundle: OkfBundle, out_dir: str) -> None:
        """Write every concept and the root `index.md` under `out_dir`, creating folders as needed.

        Args:
            bundle: The projected concepts and root index.
            out_dir: The root of the export directory.
        """
        root = Path(out_dir)
        for concept in (*bundle.concepts, bundle.index):
            self._write_concept(root, concept)

    def _write_concept(self, root: Path, concept: OkfConcept) -> None:
        """Write one concept to `<root>/<concept.path>`."""
        path = root / concept.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_concept(concept), encoding="utf-8")


def render_concept(concept: OkfConcept) -> str:
    """Render a concept's file text: a `---` YAML frontmatter block, then its body.

    A blank body yields the frontmatter alone. Any other body is trimmed of trailing newlines and
    written between one leading and one trailing newline.

    Args:
        concept: The concept to render.

    Returns:
        The file text.
    """
    body = concept.body.rstrip("\n")
    return render_frontmatter(concept.frontmatter, f"\n{body}\n" if body else "")
