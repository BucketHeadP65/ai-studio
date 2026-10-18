"""The frozen split: a held-out subset of one dataset version that a tuning run scores once, at its end.

`studio evals freeze` carves the split and writes it as `<name>.frozen.v{M}.json` with a `<name>.frozen.current` pointer.
A tuning run never trains on, proposes against or accepts on the frozen cases.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.carving import carve_strata, case_stratum

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from engram.evals.generation.domain.datasets import EvaluationCase
    from engram.evals.shared.contracts import Archetype, RatedDifficulty

# A stratum key: the case's archetype (`None` when unlabelled) and its difficulty band, the pair the
# freeze report labels its frozen counts by.
type _Stratum = tuple[Archetype | None, RatedDifficulty]


class FrozenSplit(BaseModel):
    """One frozen split, saved as `<name>.frozen.v{M}.json` beside the dataset's versions.

    Every tuning run under one split scores against the same held-out cases.

    Attributes:
        dataset_name: The dataset chain's logical name (for example `research`).
        dataset_version: The pinned immutable dataset version the frozen cases were drawn from (the
            version chain's 1-based number).
        case_names: The frozen case names, which a tuning run leaves out of training and scores at its end.
        salt: The carve salt, recorded so the draw stays reproducible.
        frozen_fraction: The fraction of the version the carve targeted (default `0.2`).
        frozen_at: The ISO-8601 time of the freeze.
        staleness_at_freeze: The dataset's stale fraction against the live knowledge base at freeze time.
        parent_frozen_version: The prior frozen version this freeze supersedes, or `None` for the first.
    """

    model_config = ConfigDict(frozen=True)

    dataset_name: str
    dataset_version: int
    case_names: tuple[str, ...]
    salt: str
    frozen_fraction: float
    frozen_at: str
    staleness_at_freeze: float
    parent_frozen_version: int | None = None


def carve_frozen(cases: Sequence[EvaluationCase], *, fraction: float, salt: str, min_per_stratum: int) -> tuple[str, ...]:
    """Pick a stratified, salted-hash subset of the case names to freeze.

    The subset holds `round(fraction * n)` names, capped at the case count, spread over the
    `(archetype, difficulty)` strata in proportion to their size. A stratum with fewer than
    `min_per_stratum` cases folds into its difficulty band. The same cases and salt always give the same names.

    Args:
        cases: The immutable dataset version's cases the frozen subset is drawn from.
        fraction: The target fraction of the version to freeze (for example `0.2`).
        salt: The salt the salted-hash draw is keyed on, recorded on the split for reproduction.
        min_per_stratum: The floor below which a stratum folds into its parent difficulty band.

    Returns:
        The frozen case names, in band order and then salted-hash order.
    """
    members = list(cases)
    total = min(round(fraction * len(members)), len(members))
    selected, _folded = carve_strata(members, total=total, min_per_stratum=min_per_stratum, salt=salt)
    return tuple(case.name or "" for case in selected)


def frozen_stratum_counts(cases: Sequence[EvaluationCase], frozen_names: Sequence[str]) -> dict[str, int]:
    """Count the cases per raw `(archetype, difficulty)` stratum, for the freeze report.

    Counts the raw stratum of each frozen case, never the folded draw band.

    Args:
        cases: The frozen version's cases (the frozen names are a subset).
        frozen_names: The carved frozen case names.

    Returns:
        Each `archetype/difficulty` label mapped to how many frozen cases fell in it.
    """
    selected = set(frozen_names)
    counts: dict[str, int] = {}
    for case in cases:
        if (case.name or "") not in selected:
            continue
        label = _stratum_label(case_stratum(case))
        counts[label] = counts.get(label, 0) + 1
    return counts


def frozen_version_path(datasets_dir: Path, name: str, version: int) -> Path:
    """Resolve the JSON sidecar path of one frozen-split version.

    Args:
        datasets_dir: The directory the dataset's version chain lives in.
        name: The dataset's logical name.
        version: The 1-based frozen-split version number.

    Returns:
        The `<datasets_dir>/<name>.frozen.v{version}.json` path.
    """
    return datasets_dir / f"{name}.frozen.v{version}.json"


def _stratum_label(stratum: _Stratum) -> str:
    """A human-readable `archetype/difficulty` label for a stratum (`none` for an unlabelled archetype)."""
    archetype, difficulty = stratum
    return f"{'none' if archetype is None else archetype.value}/{difficulty}"
