"""The files of evaluation dataset generation: dataset versions, pointers, reports and frozen splits."""

from __future__ import annotations

import contextlib
import re

from typing import TYPE_CHECKING

import yaml

from pydantic import ValidationError
from pydantic_evals import Dataset

from engram.evals.generation.domain.coverage import CoverageReport
from engram.evals.generation.domain.datasets import DatasetStamp, require_basename, require_slug, stamp_path
from engram.evals.generation.domain.freshness import FreshnessReport
from engram.evals.generation.domain.frozen import FrozenSplit, frozen_version_path
from engram.evals.harness.contracts import AnswerAccuracy, RetrievalMetrics
from engram.evals.shared.contracts import QaCaseMeta, QaInput, RetrievalAnswer

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from engram.evals.generation.domain.datasets import EvaluationDataset


_CUSTOM_EVALUATOR_TYPES: tuple[type[RetrievalMetrics | AnswerAccuracy], ...] = (RetrievalMetrics, AnswerAccuracy)


def save_dataset(dataset: EvaluationDataset, path: Path) -> None:
    """Write a dataset to `path` as YAML, with the JSON schema file pydantic-evals writes beside it.

    Args:
        dataset: The dataset to persist.
        path: The YAML destination (the format is inferred from the `.yaml` extension).
    """
    dataset.to_file(path, custom_evaluator_types=_CUSTOM_EVALUATOR_TYPES)


def load_dataset(path: Path) -> EvaluationDataset:
    """Load a dataset from a YAML file, with its `RetrievalMetrics` and `AnswerAccuracy` evaluators restored.

    Args:
        path: The YAML file to load (the format is inferred from the `.yaml` extension).

    Returns:
        The dataset.
    """
    return Dataset[QaInput, RetrievalAnswer, QaCaseMeta].from_file(path, custom_evaluator_types=_CUSTOM_EVALUATOR_TYPES)


def save_dataset_version(dataset: EvaluationDataset, stamp: DatasetStamp, path: Path) -> None:
    """Write one dataset version: its YAML file and its JSON stamp beside it at `stamp_path(path)`.

    You must pass a new `<name>.v{N}.yaml` path for each version.

    Args:
        dataset: The dataset to write.
        stamp: The version's immutable identity sidecar.
        path: The version's YAML destination (from `dataset_version_path`).
    """
    save_dataset(dataset, path)
    stamp_path(path).write_text(stamp.model_dump_json(indent=2), encoding="utf-8")


def load_stamp(dataset_path: Path) -> DatasetStamp | None:
    """Load the stamp beside a dataset version, or None when the version has no stamp.

    Args:
        dataset_path: The version's YAML path.

    Returns:
        The `DatasetStamp`, or None when no stamp file exists.

    Raises:
        ValidationError: When the stamp file does not validate.
    """
    path = stamp_path(dataset_path)
    if not path.exists():
        return None
    return DatasetStamp.model_validate_json(path.read_text(encoding="utf-8"))


def _chain_stamps(version_path: Path) -> Iterator[tuple[Path, DatasetStamp | None]]:
    """Yield each version's path and stamp from `version_path` back to the root, stopping at a cycle."""
    seen: set[str] = set()
    current: Path | None = version_path
    while current is not None and current.name not in seen:
        seen.add(current.name)
        stamp = load_stamp(current)
        yield current, stamp
        if stamp is None or stamp.parent_version is None:
            return
        # `require_basename` also refuses `..`, which `with_name` accepts.
        current = version_path.with_name(require_basename(stamp.parent_version, source="the stamp's parent_version"))


def used_case_names(version_path: Path, live_names: Iterable[str]) -> frozenset[str]:
    """Every case name ever assigned across this dataset's whole version chain.

    The result is `live_names` plus every name retired in any stamp from `version_path` back to the
    root. A missing or unreadable stamp ends the walk and keeps the names found so far. A name leaves
    a dataset only through a retirement record, so a redraft that avoids these names never reuses one.

    Args:
        version_path: The current version's YAML path, the head of the chain to walk back from.
        live_names: The case names present in the current version, always included in the result.

    Returns:
        The frozenset of every case name the chain has ever used, the live names plus every retired name.

    Raises:
        ValueError: When a stamp's `parent_version` is not a bare file name.
    """
    names = set(live_names)
    with contextlib.suppress(OSError, ValidationError):
        for _path, stamp in _chain_stamps(version_path):
            if stamp is None:
                break
            names.update(record.case_name for record in stamp.retired)
    return frozenset(names)


def version_chain(dataset_path: Path) -> tuple[str, ...]:
    """Follow the `parent_version` links from a dataset version back to its root, root first.

    The walk stays in the version's directory and stops at a cycle. A version with no stamp, or a
    first version, gives only its own file name.

    Args:
        dataset_path: The audited version's YAML path.

    Returns:
        The version file names from the chain root to the audited version.

    Raises:
        ValueError: When a stamp does not validate, or its `parent_version` is not a bare file name.
    """
    chain = [path.name for path, _stamp in _chain_stamps(dataset_path)]
    chain.reverse()
    return tuple(chain)


def read_current(datasets_dir: Path, name: str) -> Path | None:
    """Resolve the live version a dataset's `<name>.current` pointer names, or None when absent.

    The pointer holds one file name, resolved against `datasets_dir`.

    Args:
        datasets_dir: The directory the dataset's version chain lives in.
        name: The dataset's logical name.

    Returns:
        The pointed-to version's path, or None when no pointer file exists.

    Raises:
        ValueError: When `name` is not a plain name, or the pointer does not hold a bare file name.
    """
    pointer = datasets_dir / f"{require_slug(name, source='the dataset chain name')}.current"
    if not pointer.exists():
        return None
    pointed = pointer.read_text(encoding="utf-8").strip()
    return datasets_dir / require_basename(pointed, source=f"the {pointer.name} pointer body")


def write_current(datasets_dir: Path, name: str, version_path: Path) -> None:
    """Point a dataset's `<name>.current` file at a version, the only mutable file in the chain.

    The pointer holds the file name only, so the directory can move.

    Args:
        datasets_dir: The directory the dataset's version chain lives in.
        name: The dataset's logical name.
        version_path: The version's YAML path to point at.

    Raises:
        ValueError: When `name` is not a plain name.
    """
    (datasets_dir / f"{require_slug(name, source='the dataset chain name')}.current").write_text(version_path.name, encoding="utf-8")


def save_coverage_report(report: CoverageReport, path: Path) -> None:
    """Write a `CoverageReport` to `path` as YAML, the inverse of `load_coverage_report`.

    The same report always writes the same bytes.

    Args:
        report: The coverage report to persist.
        path: The YAML destination.
    """
    path.write_text(yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def load_coverage_report(path: Path) -> CoverageReport:
    """Load a `CoverageReport` from a YAML file, the inverse of `save_coverage_report`.

    Args:
        path: The YAML file to load.

    Returns:
        The report.

    Raises:
        ValidationError: When the file does not validate as a coverage report.
    """
    return CoverageReport.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# The largest freshness report file `load_freshness_report` parses, checked before `yaml.safe_load`.
_MAX_REPORT_BYTES = 10 * 1024 * 1024


def save_freshness_report(report: FreshnessReport, path: Path) -> None:
    """Write a `FreshnessReport` to `path` as YAML, the inverse of `load_freshness_report`.

    The same report always writes the same bytes.

    Args:
        report: The freshness report to persist.
        path: The YAML destination.
    """
    path.write_text(yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False), encoding="utf-8")


def load_freshness_report(path: Path) -> FreshnessReport:
    """Load a `FreshnessReport` from a YAML file, the inverse of `save_freshness_report`.

    A file larger than 10 MiB is refused before it is parsed.

    Args:
        path: The YAML file to load.

    Returns:
        The report.

    Raises:
        ValueError: When the file is larger than 10 MiB, or does not validate as a freshness report.
    """
    raw = path.read_bytes()
    if len(raw) > _MAX_REPORT_BYTES:
        msg = f"freshness report {path} is {len(raw)} bytes, exceeding the {_MAX_REPORT_BYTES}-byte cap"
        raise ValueError(msg)
    return FreshnessReport.model_validate(yaml.safe_load(raw.decode("utf-8")))


def next_frozen_version(datasets_dir: Path, name: str) -> int:
    """The next free frozen-split version for a dataset chain (1 on a clean directory).

    It is the highest existing `<name>.frozen.v{M}.json` number plus one.

    Args:
        datasets_dir: The directory the dataset's version chain lives in.
        name: The dataset's logical name.

    Returns:
        The next free frozen-split version number.
    """
    pattern = re.compile(rf"^{re.escape(name)}\.frozen\.v(\d+)\.json$")
    versions = [int(match.group(1)) for path in datasets_dir.glob(f"{name}.frozen.v*.json") if (match := pattern.match(path.name))]
    return max(versions, default=0) + 1


def save_frozen_split(split: FrozenSplit, datasets_dir: Path) -> Path:
    """Persist a frozen split as the next `<name>.frozen.v{M}.json` and move the `.frozen.current` pointer.

    A new freeze always writes a new version. Earlier versions are never changed or deleted.
    The pointer holds the file name only, so the directory can move.

    Args:
        split: The frozen split to write. Its `dataset_name` names the chain.
        datasets_dir: The directory the dataset's version chain lives in.

    Returns:
        The written `<name>.frozen.v{M}.json` path.

    Raises:
        ValueError: When the split's `dataset_name` is not a plain name.
    """
    require_slug(split.dataset_name, source="the frozen split's dataset name")
    version = next_frozen_version(datasets_dir, split.dataset_name)
    path = frozen_version_path(datasets_dir, split.dataset_name, version)
    path.write_text(split.model_dump_json(indent=2), encoding="utf-8")
    (datasets_dir / f"{split.dataset_name}.frozen.current").write_text(path.name, encoding="utf-8")
    return path


def load_frozen_split(datasets_dir: Path, name: str) -> FrozenSplit | None:
    """Load the current frozen split for a dataset, or `None` when it was never frozen.

    A missing pointer, or a pointer to a missing file, gives `None`.

    Args:
        datasets_dir: The directory the dataset's version chain lives in.
        name: The dataset's logical name.

    Returns:
        The current `FrozenSplit`, or `None` when no frozen split has been written.

    Raises:
        ValueError: When `name` is not a plain name, the pointer does not hold a bare file name, or the
            split file does not validate.
    """
    pointer = datasets_dir / f"{require_slug(name, source='the frozen chain name')}.frozen.current"
    if not pointer.exists():
        return None
    pointed = pointer.read_text(encoding="utf-8").strip()
    path = datasets_dir / require_basename(pointed, source=f"the {pointer.name} pointer body")
    if not path.exists():
        return None
    return FrozenSplit.model_validate_json(path.read_text(encoding="utf-8"))
