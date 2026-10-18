"""Write a dataset's score as YAML with a markdown summary beside it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write

from engram.evals.scoring.domain import DEFAULT_SEARCH_SETUP, score_markdown

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.scoring.domain import DatasetScore


def score_report_stem(dataset: Path, setup: str) -> str:
    """The name both files of a dataset's score report under `setup` start with: `<dataset stem>.<setup>`.

    The default search setup leaves the setup out: `<dataset stem>`.

    Args:
        dataset: The scored dataset file.
        setup: The name of the search setup the dataset was scored under.

    Returns:
        The report's name before `.score.yaml` and `.score.md`.
    """
    return dataset.stem if setup == DEFAULT_SEARCH_SETUP else f"{dataset.stem}.{setup}"


def write_score_report(score: DatasetScore, *, dataset: Path, collection: str, out_dir: Path) -> tuple[Path, Path]:
    """Write `<dataset stem>.<setup>.score.yaml` and `<dataset stem>.<setup>.score.md` under `out_dir`.

    A score under the default search setup leaves the setup out of both names: `<dataset stem>.score.yaml` and `.md`.

    Args:
        score: The dataset's score, whose search setup names the report.
        dataset: The scored dataset file, whose stem names the report.
        collection: The knowledge base searched.
        out_dir: The directory the two files land in, created when missing.

    Returns:
        The YAML path and the markdown path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = score_report_stem(dataset, score.setup)
    yaml_path = out_dir / f"{stem}.score.yaml"
    markdown_path = out_dir / f"{stem}.score.md"
    document = {"dataset": dataset.name, "collection": collection, **score.model_dump(mode="json")}
    atomic_write(yaml_path, yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    atomic_write(markdown_path, score_markdown(score, dataset=dataset.name, collection=collection))
    return yaml_path, markdown_path
