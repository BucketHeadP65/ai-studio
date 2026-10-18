"""Write a generator check as YAML with a markdown summary beside it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write

from engram.evals.generator_check.domain import generator_check_markdown

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.generator_check.domain import GeneratorCheck


def write_generator_check(check: GeneratorCheck, *, human: Path, generated: Path, collection: str, out_dir: Path) -> tuple[Path, Path]:
    """Write `<generated stem>.generator-check.yaml` and `<generated stem>.generator-check.md` under `out_dir`.

    Args:
        check: The generator check.
        human: The human-judged dataset file.
        generated: The generated dataset file, whose stem names the report.
        collection: The knowledge base searched.
        out_dir: The directory the two files land in, created when missing.

    Returns:
        The YAML path and the markdown path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = out_dir / f"{generated.stem}.generator-check.yaml"
    markdown_path = out_dir / f"{generated.stem}.generator-check.md"
    document = {"human": human.name, "generated": generated.name, "collection": collection, **check.model_dump(mode="json")}
    atomic_write(yaml_path, yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    atomic_write(markdown_path, generator_check_markdown(check, human=human.name, generated=generated.name, collection=collection))
    return yaml_path, markdown_path
