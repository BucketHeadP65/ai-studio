"""The generation pipeline, which seeds, drafts, quote checks and curates cases into a dataset.

`GenerationPipeline.generate` turns a `collection`, optional `page_ids` and the archetypes to cover
into a balanced `EvaluationDataset`. It draws structural seeds per coverage cell, drafts cases from each
seed, keeps the drafts that pass the quote check, then dedups and stratifies them.
The pipeline does no file IO, so the caller persists the returned dataset.
`GenerationConfig` holds every setting a generation run reads.
"""

from __future__ import annotations

import math

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from engram.evals.generation.domain.allocation import allocate
from engram.evals.generation.domain.datasets import build_dataset, case_name
from engram.evals.generation.domain.drafting import accept
from engram.evals.generation.service.archetypes import resolve_archetype
from engram.evals.shared.contracts import DEFAULT_DIFFICULTY_THRESHOLDS, Archetype, DifficultyThresholds

if TYPE_CHECKING:
    from collections.abc import Sequence

    from engram.evals.generation.domain.allocation import QuotaAllocation
    from engram.evals.generation.domain.coverage import StratumInventory
    from engram.evals.generation.domain.datasets import AuthoredCase, EvaluationDataset
    from engram.evals.generation.internal_contracts import CaseDrafter, SupportsCuration, SupportsInventory, SupportsSeeding

# How many times over a cell's computed seed count is drawn, so the cell still meets its quota after
# the quote check and dedup drop cases.
_SEED_LIMIT_BUFFER = 2.0


class GenerationConfig(BaseModel):
    """The settings of a generation run.

    Attributes:
        target_distribution: The target share of the dataset per archetype, read as a weight against
            `total_cases`. The coverage allocator splits it over feasible strata. Never change it in
            place. Freezing the model does not freeze the `dict` it holds.
        total_cases: The overall case budget the assembled dataset is capped at.
        target_per_seed: How many candidate cases to draft per structural seed.
        dedup_threshold: The cosine similarity at or above which two questions are near duplicates.
            The curator is built with it, and the pipeline never reads it.
        thresholds: The difficulty band cutoffs `accept` and the coverage allocator use, which decide
            which structural shapes count as `easy`, `medium` or `hard`.
        salt: The default sampling salt the generate handler falls back to. The pipeline never reads
            it and draws with the salt its constructor receives. A command's `--salt` overrides it, and
            a fresh generation with neither set draws with a new random salt.
        min_pool_warn: The seed-pool size below which the coverage report flags a feasible, non-empty
            stratum as thin. Only the `evals coverage` handler reads it.
    """

    model_config = ConfigDict(frozen=True)

    target_distribution: Mapping[Archetype, float]
    total_cases: int = 50
    target_per_seed: int = 3
    dedup_threshold: float = 0.92
    thresholds: DifficultyThresholds = DEFAULT_DIFFICULTY_THRESHOLDS
    salt: str | None = None
    min_pool_warn: int = 3


def _seed_limit(quota: int, target_per_seed: int) -> int:
    """Seeds to draw for one coverage cell: `quota / target_per_seed` times `_SEED_LIMIT_BUFFER`, rounded up, at least one."""
    return max(math.ceil(quota / target_per_seed * _SEED_LIMIT_BUFFER), 1)


def _next_free_name(seed_key: str, ordinal: int, used_names: frozenset[str], taken: set[str]) -> str:
    """The first `{seed_key}#{ordinal}` name at or after `ordinal` that is in neither `used_names` nor `taken`."""
    name = case_name(seed_key, ordinal)
    while name in used_names or name in taken:
        ordinal += 1
        name = case_name(seed_key, ordinal)
    return name


@dataclass(frozen=True, slots=True)
class GeneratedDraw:
    """What one pipeline run generated, and the census plus allocation it drew from.

    A caller stamps the dataset version from `inventory` and `allocation`, which are exactly what the
    run drew from, rather than from a fresh read of the knowledge base.

    Attributes:
        dataset: The assembled dataset, which the caller persists.
        inventory: The per-stratum knowledge base census the run drew its seeds from.
        allocation: The per-stratum case quota the run generated to, derived from `inventory`.
    """

    dataset: EvaluationDataset
    inventory: StratumInventory
    allocation: QuotaAllocation


class GenerationPipeline:
    """Builds a dataset from a knowledge base census: allocate, seed each cell, draft, quote check, curate, build."""

    def __init__(  # noqa: PLR0913 (each is a distinct injected collaborator, bundling them would obscure the pipeline's wiring)
        self,
        repo: SupportsSeeding,
        drafter: CaseDrafter,
        curator: SupportsCuration,
        config: GenerationConfig,
        reader: SupportsInventory,
        *,
        salt: str = "",
    ) -> None:
        """Bind the structural seeder, the drafter, the curator, the run config and the inventory reader.

        Args:
            repo: The structural seeder each cell draws its seeds from.
            drafter: The `CaseDrafter` that writes a question, an answer and support quotes from a seed.
            curator: The curation stage that deduplicates and stratifies the raw case pool.
            config: The run config (distribution, budgets, difficulty thresholds).
            reader: The inventory reader that censuses the knowledge base per stratum for the allocation.
            salt: The sampling salt every stratified seed draw hashes with. A run is reproducible from its
                knowledge base, salt and budget, and an empty salt draws in the unsalted order.
        """
        self._repo = repo
        self._drafter = drafter
        self._curator = curator
        self._config = config
        self._reader = reader
        self._salt = salt

    async def generate(
        self, *, collection: str, page_ids: tuple[str, ...], archetypes: Sequence[Archetype], used_names: frozenset[str] = frozenset()
    ) -> GeneratedDraw:
        """Generate a coverage-scaled, quote-checked dataset over the requested archetypes, with no file IO.

        Censuses the knowledge base, allocates a case quota to each feasible stratum, and fills each cell
        with drafts from its salted seed draw whose support quotes verify against the seed's source text.
        A kept case is named `{seed_key}#{ordinal}` at the first free ordinal at or after the draft's
        position among its seed's drafts. A name is free when it is in neither `used_names` nor this
        run's names, so a redraft never reuses a retired name. The pooled cases are deduplicated and
        stratified to the allocation.

        Args:
            collection: The collection the seeds are drawn from.
            page_ids: Restrict seeds to these pages, or `()` for the whole collection.
            archetypes: The archetypes to cover, or empty to cover every allocated cell. A cell outside a
                non-empty set is skipped, and the quotas of the other cells stay as allocated.
            used_names: The case names the dataset's version chain has ever used. Empty for a fresh
                generation.

        Returns:
            A `GeneratedDraw` pairing the assembled dataset with the census and allocation it was drawn from.
        """
        inventory = await self._reader.inventory(collection=collection)
        allocation = allocate(self._config.target_distribution, self._config.total_cases, inventory, self._config.thresholds)
        requested = set(archetypes)
        pool: list[AuthoredCase] = []
        # Names assigned so far this run, so the next-free-ordinal search never collides within the run.
        taken: set[str] = set()
        for cell in allocation.cells:
            if requested and cell.stratum.archetype not in requested:
                continue
            spec = resolve_archetype(cell.stratum.archetype.value)
            limit = _seed_limit(cell.quota, self._config.target_per_seed)
            seeds = await spec.seed(
                self._repo,
                collection=collection,
                page_ids=page_ids,
                limit=limit,
                salt=self._salt,
                band=cell.stratum.difficulty,
                anchor_page_type=cell.stratum.anchor_page_type,
            )
            for seed in seeds:
                drafts = await self._drafter.draft(seed, instructions=spec.instructions, target=self._config.target_per_seed)
                for ordinal, draft in enumerate(drafts):
                    case = accept(draft, seed=seed, thresholds=self._config.thresholds)
                    if case is not None:
                        name = _next_free_name(seed.seed_key, ordinal, used_names, taken)
                        taken.add(name)
                        # The name rides on the case, so it survives curation and `build_dataset` keeps it.
                        pool.append(case.model_copy(update={"case_name": name}))
        deduped = await self._curator.dedup(pool)
        stratified = self._curator.stratify_cells(deduped, allocation)
        return GeneratedDraw(dataset=build_dataset(stratified), inventory=inventory, allocation=allocation)
