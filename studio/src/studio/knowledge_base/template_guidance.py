"""Copying a template's guidance pages into a new knowledge base."""

from __future__ import annotations

import shutil

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from engram.init.contracts import Template


def copy_template_guidance(template: Template, target: Path) -> None:
    """Copy a template's `_meta/*.md` guidance pages into `target`, to run after the scaffold is written.

    A copied page replaces the scaffold's page of the same name.

    Args:
        template: The template to copy from.
        target: The new knowledge base's folder.
    """
    template_meta = template.path / "_meta"
    if not template_meta.is_dir():
        return
    target_meta = target / "_meta"
    target_meta.mkdir(parents=True, exist_ok=True)
    for md_file in template_meta.glob("*.md"):
        shutil.copy2(md_file, target_meta / md_file.name)
