"""Read and write a recommendation as a YAML file."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from engram.evals.recommendation.domain.recommendation import Recommendation

if TYPE_CHECKING:
    from pathlib import Path


def save_recommendation(recommendation: Recommendation, path: Path) -> None:
    """Write a recommendation to `path` as YAML, the inverse of `load_recommendation`.

    The parent folder of `path` is created when it does not exist.

    Args:
        recommendation: The recommendation to write.
        path: The YAML file to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(recommendation.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def load_recommendation(path: Path) -> Recommendation:
    """Read a recommendation from a YAML file, the inverse of `save_recommendation`.

    Args:
        path: The YAML file to read.

    Returns:
        The recommendation the file holds.

    Raises:
        pydantic.ValidationError: When the file does not hold a valid recommendation.
    """
    return Recommendation.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
