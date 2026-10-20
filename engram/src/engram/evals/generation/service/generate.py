"""The generate use case: draw a fresh dataset version, or top up a parent version where coverage has gaps.

`DatasetGeneration` runs the generation pipeline and writes the result as the next version of a dataset chain,
with its stamp and its `.current` pointer. It never changes an earlier version file.
"""

from __future__ import annotations

import re
import secrets

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from engram.evals.generation.domain.allocation import allocate
from engram.evals.generation.domain.coverage import stratum_label
from engram.evals.generation.domain.datasets import DatasetStamp, EvaluationDataset, dataset_version_path, require_slug, strip_version
from engram.evals.generation.domain.freshness import retiring_names
from engram.evals.generation.domain.topup import merge_top_up, parent_case_refs, plan_top_up
from engram.evals.generation.infrastructure.files import (
    load_dataset,
    load_stamp,
    read_current,
    save_dataset_version,
    used_case_names,
    write_current,
)
from engram.evals.generation.service.freshness import freshness_verdicts
from engram.evals.generation.service.pipeline import GenerationPipeline
from engram.evals.shared.contracts import Archetype

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.evals.commands import (
        DrafterFactory,
        FreshnessReaderFactory,
        GenerateEvalDataset,
        InventoryReaderFactory,
        SeedRepositoryFactory,
    )
    from engram.evals.generation.domain.allocation import QuotaAllocation
    from engram.evals.generation.domain.datasets import EvaluationCase
    from engram.evals.generation.domain.topup import TopUpPlan
    from engram.evals.generation.service.curate import CaseCurator
    from engram.evals.generation.service.pipeline import GenerationConfig

CURRENT_DATASET = "current"
"""The `top_up_from` value that names the chain's live version through its `<name>.current` pointer."""


@dataclass(frozen=True, slots=True)
class GeneratedVersion:
    """What one generation produced: the version written, or on a dry-run top-up the parent it planned against."""

    path: Path
    """The version file written. On a dry-run top-up, the parent version file."""
    n_cases: int
    """The case count of the dataset at `path`."""
    requested_archetypes: tuple[Archetype, ...]
    """The archetypes the run covered, all of them when the command named none."""
    version: str
    """The file name of the version written, empty on a dry-run top-up."""
    retired: int = 0
    """How many parent cases a top-up retired, 0 for a fresh run."""
    plan: TopUpPlan | None = None
    """The top-up plan on a dry-run top-up, `None` otherwise."""


class DatasetGeneration:
    """Runs a fresh generation or a coverage-gap top-up and writes the next version of the dataset chain."""

    def __init__(  # noqa: PLR0913 (each factory opens a different session or builds a different collaborator, a wrapper object would just rename them)
        self,
        *,
        make_repo: SeedRepositoryFactory,
        curator: CaseCurator,
        config: GenerationConfig,
        make_drafter: DrafterFactory,
        make_reader: InventoryReaderFactory,
        make_freshness_reader: FreshnessReaderFactory,
    ) -> None:
        """Bind the factories, the curator and the run config.

        Args:
            make_repo: Opens a fresh structural seeder over one owner session per call, which the pipeline draws its seeds from.
            curator: The `CaseCurator` the pipeline dedups and stratifies the raw case pool with.
            config: The run config (distribution, budgets, difficulty thresholds, default salt). A command's `count` overrides its `total_cases`.
            make_drafter: Builds the `CaseDrafter` for the command's model.
            make_reader: Opens a fresh inventory reader over one owner session per call, for the census and the knowledge base fingerprint
                the version stamp records.
            make_freshness_reader: Opens a fresh freshness reader over one owner session per call. Only a top-up reads it, to retire
                and redraft the cases the knowledge base changed under.
        """
        self._make_repo = make_repo
        self._curator = curator
        self._config = config
        self._make_drafter = make_drafter
        self._make_reader = make_reader
        self._make_freshness_reader = make_freshness_reader

    async def generate(self, command: GenerateEvalDataset) -> GeneratedVersion:
        """Draw the whole allocation from scratch and write it as the chain's next version with its stamp.

        Args:
            command: The generate command, with its collection, output path, model, budget, salt and archetypes.

        Returns:
            The version written.

        Raises:
            ValueError: When the dataset chain name is not a plain name.
        """
        drafter = self._make_drafter(command.model)
        config = self._config_for(command)
        archetypes = command.archetypes or tuple(Archetype)
        salt = resolve_salt(command.salt, config.salt)

        async with self._make_repo() as repo, self._make_reader() as reader:
            pipeline = GenerationPipeline(repo, drafter, self._curator, config, reader, salt=salt)
            draw = await pipeline.generate(collection=command.collection, page_ids=command.page_ids, archetypes=archetypes)
            fingerprint = await reader.knowledge_base_fingerprint(collection=command.collection)

        stamp = DatasetStamp(
            salt=salt,
            knowledge_base_fingerprint=fingerprint,
            budget=config.total_cases,
            parent_version=None,
            coverage=coverage_summary(draw.allocation),
            retired=(),
        )
        version_path = write_version(command.out, draw.dataset, stamp)  # immutable version + stamp + pointer, outside the session
        return GeneratedVersion(
            path=version_path, n_cases=len(draw.dataset.cases), requested_archetypes=archetypes, version=version_path.name
        )

    async def top_up(self, command: GenerateEvalDataset, top_up_from: str) -> GeneratedVersion:
        """Plan a coverage-gap top-up of a parent version, then return the plan on a dry run or write the next version.

        The top-up retires the parent's stale and displaced cases, drafts only the under-covered strata with the
        parent's salt unless the command gives one, and never changes the parent file.

        Args:
            command: The generate command, with its collection, output path, model, budget and options.
            top_up_from: The parent version file, or `current` for the chain's live version.

        Returns:
            The version written, or on a dry run the parent version with the plan.

        Raises:
            ValueError: When the parent carries no dataset stamp, there is no `current` pointer to top up from,
                or the dataset chain name is not a plain name.
        """
        parent_path = resolve_top_up_source(top_up_from, command.out)
        parent_dataset = load_dataset(parent_path)
        parent_stamp = load_stamp(parent_path)
        if parent_stamp is None:
            msg = f"cannot top up {parent_path}: it carries no dataset stamp (an unversioned dataset cannot be topped up)"
            raise ValueError(msg)

        config = self._config_for(command)
        archetypes = command.archetypes or tuple(Archetype)
        salt = command.salt if command.salt is not None else parent_stamp.salt

        stale = await self._stale_case_names(command.collection, parent_dataset.cases)
        used = used_case_names(parent_path, [case.name for case in parent_dataset.cases if case.name])
        async with self._make_reader() as reader:
            inventory = await reader.inventory(collection=command.collection)
            fingerprint = await reader.knowledge_base_fingerprint(collection=command.collection)
            existing_page_ids = await reader.page_ids(collection=command.collection)
        allocation = allocate(config.target_distribution, config.total_cases, inventory, config.thresholds)
        plan = plan_top_up(
            parent_case_refs(parent_dataset.cases),
            parent_stamp,
            inventory,
            allocation,
            parent_version=parent_path.name,
            existing_page_ids=existing_page_ids,
            salt=salt,
            stale_case_names=stale,
        )

        if command.dry_run:
            return GeneratedVersion(
                path=parent_path,
                n_cases=len(parent_dataset.cases),
                requested_archetypes=archetypes,
                version="",
                retired=len(plan.retire),
                plan=plan,
            )

        fresh_cases = await self._generate_gap(command, config, salt, plan, used)
        merged = merge_top_up(parent_dataset.cases, plan, fresh_cases, allocation)
        merged_dataset = EvaluationDataset(name=parent_dataset.name, cases=merged, evaluators=list(parent_dataset.evaluators))
        stamp = DatasetStamp(
            salt=salt,
            knowledge_base_fingerprint=fingerprint,
            budget=config.total_cases,
            parent_version=parent_path.name,
            coverage=coverage_summary(allocation),
            retired=plan.retire,
        )
        version_path = write_version(command.out, merged_dataset, stamp)
        return GeneratedVersion(
            path=version_path,
            n_cases=len(merged),
            requested_archetypes=archetypes,
            version=version_path.name,
            retired=len(plan.retire),
        )

    def _config_for(self, command: GenerateEvalDataset) -> GenerationConfig:
        """The run config, with the command's `count` as its `total_cases` when the command gives one."""
        return self._config if command.count is None else self._config.model_copy(update={"total_cases": command.count})

    async def _stale_case_names(self, collection: str, cases: Sequence[EvaluationCase]) -> frozenset[str]:
        """The parent case names to retire as `stale`, leaving out `gold_gone` cases, which the top-up plan retires itself."""
        async with self._make_freshness_reader() as reader:
            verdicts = await freshness_verdicts(reader, cases, collection=collection)
        gold_gone = frozenset(verdict.case_name for verdict in verdicts if verdict.verdict == "gold_gone")
        return retiring_names(verdicts) - gold_gone

    async def _generate_gap(
        self, command: GenerateEvalDataset, config: GenerationConfig, salt: str, plan: TopUpPlan, used_names: frozenset[str]
    ) -> list[EvaluationCase]:
        """Draft fresh cases for the plan's gap strata by rerunning the pipeline over their archetypes, empty with no gap."""
        gap_archetypes = sorted({cell.stratum.archetype for cell in plan.generate}, key=lambda archetype: archetype.value)
        if not gap_archetypes:
            return []
        drafter = self._make_drafter(command.model)
        async with self._make_repo() as repo, self._make_reader() as reader:
            pipeline = GenerationPipeline(repo, drafter, self._curator, config, reader, salt=salt)
            fresh = await pipeline.generate(
                collection=command.collection, page_ids=command.page_ids, archetypes=tuple(gap_archetypes), used_names=used_names
            )
        return list(fresh.dataset.cases)


def resolve_salt(command_salt: str | None, config_salt: str | None) -> str:
    """The command's salt, else the configured default, else a new random one, an empty string counting as set.

    Args:
        command_salt: The salt the command gave, or `None`.
        config_salt: The configured default salt, or `None`.

    Returns:
        The salt a run draws with.
    """
    if command_salt is not None:
        return command_salt
    if config_salt is not None:
        return config_salt
    return secrets.token_hex(8)


def logical_name(out: Path) -> str:
    """The dataset chain's logical name from a base path: its stem with any trailing `.v{N}` stripped.

    Args:
        out: The base path of the chain.

    Returns:
        The chain name.
    """
    return strip_version(out.stem)


def next_version(datasets_dir: Path, name: str) -> int:
    """The next free version number for a dataset chain in `datasets_dir` (1 on a clean directory).

    Args:
        datasets_dir: The directory holding the chain's version files.
        name: The chain name.

    Returns:
        The next version number.

    Raises:
        ValueError: When `name` is not a plain name.
    """
    require_slug(name, source="the dataset chain name")
    pattern = re.compile(rf"^{re.escape(name)}\.v(\d+)\.yaml$")
    versions = [int(match.group(1)) for path in datasets_dir.glob(f"{name}.v*.yaml") if (match := pattern.match(path.name))]
    return max(versions, default=0) + 1


def coverage_summary(allocation: QuotaAllocation) -> dict[str, int]:
    """The version stamp's coverage summary: the allocated case quota per `archetype/difficulty/anchor_page_type` stratum.

    Args:
        allocation: The quota allocation of the run.

    Returns:
        The quota of each stratum, keyed by its label.
    """
    return {stratum_label(cell.stratum): cell.quota for cell in allocation.cells}


def resolve_top_up_source(top_up_from: str, out: str) -> Path:
    """The parent version to top up from: the `current` pointer of the `out` chain, or the given path.

    Args:
        top_up_from: The parent version file, or `current`.
        out: The base path of the chain, which names the chain `current` points into.

    Returns:
        The parent version file.

    Raises:
        ValueError: When `top_up_from` is `current` and the chain has no `current` pointer.
    """
    if top_up_from == CURRENT_DATASET:
        base = Path(out)
        name = logical_name(base)
        resolved = read_current(base.parent, name)
        if resolved is None:
            msg = f"no current dataset pointer for {name!r} in {base.parent} to top up from"
            raise ValueError(msg)
        return resolved
    return Path(top_up_from)


def write_version(out: str, dataset: EvaluationDataset, stamp: DatasetStamp) -> Path:
    """Write the `out` chain's next version with its stamp, move the chain's `.current` pointer, and return the path.

    Args:
        out: The base path of the chain.
        dataset: The dataset to write.
        stamp: The stamp to write beside it.

    Returns:
        The version file written.

    Raises:
        ValueError: When the chain name is not a plain name.
    """
    base = Path(out)
    datasets_dir = base.parent
    name = logical_name(base)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    version_path = dataset_version_path(datasets_dir, name, next_version(datasets_dir, name))
    save_dataset_version(dataset, stamp, version_path)
    write_current(datasets_dir, name, version_path)
    return version_path
