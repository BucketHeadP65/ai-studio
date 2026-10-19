r"""The evals command handlers: generate, coverage, freshness, freeze, tune, rollback, recommend and verify.

Each handler narrows the command the bus delivers, runs it and returns a report.
Each exposes an `events` list the bus drains after `handle` returns, and every dependency is injected.
A handler that reads the database opens a fresh reader or repository per command through a factory.
"""

from __future__ import annotations

import re
import secrets
import time

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from core.cqrs.contracts import narrow_command

from engram.evals.generation.contracts import (
    CURRENT_DATASET,
    AuditedVersion,
    DatasetGeneration,
    EvaluationDataset,
    FrozenSplit,
    StratumInventory,
    allocate,
    band_inventory,
    band_target,
    build_coverage_report,
    build_report,
    carve_frozen,
    case_freshness_refs,
    covered_by_band,
    dataset_version_path,
    evaluate_premises,
    freshness_verdicts,
    frozen_stratum_counts,
    load_dataset,
    load_frozen_split,
    logical_name,
    next_frozen_version,
    premise_reads,
    ranked_stale_pages,
    read_current,
    render_coverage_markdown,
    render_freshness_markdown,
    require_slug,
    save_coverage_report,
    save_freshness_report,
    save_frozen_split,
    staleness_fraction,
    stratum_label,
    strip_version,
    version_chain,
)
from engram.evals.generator_check.contracts import SetupsTiedError, check_generator, write_generator_check
from engram.evals.rca.contracts import diagnose
from engram.evals.recommendation.contracts import (
    BaselineSnapshot,
    case_snapshots,
    gate_recommendation,
    load_recommendation,
    remap_diagnosis,
    render_draft_adr,
    render_verify_markdown,
    resolve_baseline_policy,
    resolve_rerun_policy,
    save_recommendation,
    simulate_restructures,
    structural_diagnosis,
    verify_recommendation,
)
from engram.evals.scoring.contracts import NDCG_K, score_dataset, write_score_report
from engram.evals.shared.contracts import RECALL_PREFIX, EvaluationCaseResultRow, EvaluationRunSummary, primary_at_k_key
from engram.evals.tuning.contracts import TuningLoop, effective_baseline_policy, save_policy
from engram.kb.contracts import ApplyRetrievalPolicy, ApplyRetrievalPolicyHandler, load_knowledge_base_schema

from .commands import (
    CheckFreshness,
    CheckGenerator,
    ComputeCoverage,
    CoverageComputed,
    CoverageComputedReport,
    DatasetFrozenReport,
    DatasetScored,
    FreezeDataset,
    FreshnessCheckedReport,
    GenerateEvalDataset,
    GenerateEvalDatasetReport,
    GenerationCompleted,
    GenerationStarted,
    GeneratorChecked,
    PolicyRolledBackReport,
    RecommendationCompleted,
    RecommendationReport,
    RecommendationStarted,
    RecommendStructure,
    RollbackPolicy,
    RoundEvaluated,
    ScoreDataset,
    TuneRetrieval,
    TuningCompleted,
    TuningReport,
    TuningStarted,
    VerificationCompleted,
    VerificationStarted,
    VerifyRecommendation,
    VerifyReport,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from core.cqrs.contracts import Command, Event

    from engram.config import RecommendationSettings
    from engram.evals.generation.contracts import (
        CaseCurator,
        CasePremise,
        CoverageReport,
        EvaluationCase,
        FreshnessReport,
        GenerationConfig,
        QuotaAllocation,
    )
    from engram.evals.rca.contracts import ReflectorFactory, ReportLike
    from engram.evals.scoring.contracts import HitSources, Searcher
    from engram.evals.tuning.contracts import TuningObjective, TuningResult

    from .commands import (
        DrafterFactory,
        FreshnessReader,
        FreshnessReaderFactory,
        InventoryReaderFactory,
        KnowledgeBaseGatewayFactory,
        PolicyEvaluatorFactory,
        ProposerFactory,
        RecommendationProposerFactory,
        RetrievalPolicyApply,
        RunRepositoryFactory,
        SeedRepositoryFactory,
    )


class GenerateEvalDatasetHandler:
    """Handles `GenerateEvalDataset`: run the generation pipeline and write its dataset as a new version."""

    def __init__(
        self,
        make_repo: SeedRepositoryFactory,
        curator: CaseCurator,
        config: GenerationConfig,
        make_drafter: DrafterFactory,
        make_reader: InventoryReaderFactory,
        make_freshness_reader: FreshnessReaderFactory,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the factories, the curator, the run config and the clock.

        Args:
            make_repo: Opens a fresh structural seeder over one owner session per command, which the
                pipeline draws its seeds from.
            curator: The `CaseCurator` the pipeline dedups and stratifies the raw case pool with.
            config: The run config (distribution, budgets, difficulty thresholds, default salt). A
                command's `count` overrides its `total_cases`.
            make_drafter: Builds the `CaseDrafter` for the command's model.
            make_reader: Opens a fresh inventory reader over one owner session per command, for the
                census and the knowledge base fingerprint the version stamp records.
            make_freshness_reader: Opens a fresh freshness reader over one owner session per command.
                Only a top-up reads it, to retire and redraft the cases the knowledge base changed under.
            clock: Monotonic seconds for the report duration.
        """
        self._generation = DatasetGeneration(
            make_repo=make_repo,
            curator=curator,
            config=config,
            make_drafter=make_drafter,
            make_reader=make_reader,
            make_freshness_reader=make_freshness_reader,
        )
        self._clock = clock
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> GenerateEvalDatasetReport:
        """The bus entry point, which narrows `command` to `GenerateEvalDataset`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `GenerateEvalDataset`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, GenerateEvalDataset, handler="GenerateEvalDatasetHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: GenerateEvalDataset) -> GenerateEvalDatasetReport:
        """Run a fresh generation or a coverage-gap top-up, write the new version, and return its report.

        A fresh run draws the whole allocation. A top-up (`command.top_up_from`) retires the parent's
        stale and displaced cases, drafts only the under-covered strata with the parent's salt unless the
        command gives one, and never changes the parent file. A dry-run top-up returns its plan on the
        report and writes nothing. Both emit `GenerationStarted` before and `GenerationCompleted` after.

        Args:
            command: The generate command, with its collection, output path, model, budget and top-up options.

        Returns:
            The report: the version written (the parent on a dry run), its case count, and for a top-up the
            retired count and, on a dry run, the plan.

        Raises:
            ValueError: When a top-up's parent carries no dataset stamp, there is no `current` pointer
                to top up from, or the dataset chain name is not a plain name.
        """
        started = self._clock()
        self._emit(GenerationStarted(collection=command.collection, out=command.out))
        top_up_from = command.top_up_from
        generated = await (
            self._generation.top_up(command, top_up_from) if top_up_from is not None else self._generation.generate(command)
        )
        report = GenerateEvalDatasetReport(
            out=str(generated.path),
            n_cases=generated.n_cases,
            requested_archetypes=generated.requested_archetypes,
            version=generated.version,
            retired=generated.retired,
            plan=generated.plan,
            duration_seconds=self._clock() - started,
        )
        self._emit(GenerationCompleted(report=report))
        return report

    def _emit(self, event: Event) -> None:
        """Append an event to the bus-drained collector."""
        self.events.append(event)


# -- coverage handler ----------------------------


class ComputeCoverageHandler:
    """Handles `ComputeCoverage`: census the knowledge base, optionally audit a dataset, and write a coverage report.

    It never writes the database or changes a dataset file. Its only writes are the report YAML and its
    markdown summary. An audit counts a dataset's coverage per `(archetype, difficulty)`, folding the
    census and the target over anchor page type, and a census with no dataset keeps each anchor page type apart.
    """

    def __init__(self, make_reader: InventoryReaderFactory, config: GenerationConfig, datasets_dir: Path) -> None:
        """Bind the reader factory, the generation config and the datasets directory.

        Args:
            make_reader: Opens a fresh inventory reader over one owner session per command.
            config: The generation config supplying the target distribution, case budget, difficulty band
                cutoffs and thin-pool floor the target allocation and the report use.
            datasets_dir: The directory dataset version chains live in, where the `current` pointer is resolved.
        """
        self._make_reader = make_reader
        self._config = config
        self._datasets_dir = datasets_dir
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> CoverageComputedReport:
        """The bus entry point, which narrows `command` to `ComputeCoverage`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `ComputeCoverage`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, ComputeCoverage, handler="ComputeCoverageHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: ComputeCoverage) -> CoverageComputedReport:
        """Census the knowledge base, build the coverage report, write it, and return its summary.

        Args:
            command: The coverage command, with its collection, optional dataset to audit and output directory.

        Returns:
            The report's summary, with its gaps, over-represented, infeasible and thin strata and the two
            files written.

        Raises:
            ValueError: When the dataset is `current` and the collection has no current pointer, or the
                collection is not a plain name.
        """
        async with self._make_reader() as reader:
            inventory = await reader.inventory(collection=command.collection)
        allocation = allocate(self._config.target_distribution, self._config.total_cases, inventory, self._config.thresholds)
        report = self._build_report(command, inventory, allocation)
        yaml_path, markdown_path = self._write_artifacts(command.out, command.collection, report)

        result = CoverageComputedReport(
            out=str(yaml_path),
            markdown=str(markdown_path),
            collection=report.collection,
            dataset_version=report.dataset_version,
            gaps=tuple(stratum_label(stratum) for stratum in report.gaps),
            over_represented=tuple(stratum_label(stratum) for stratum in report.over_represented),
            infeasible=tuple(stratum_label(stratum) for stratum in report.infeasible),
            debt=report.debt,
            version_chain=report.version_chain,
            thin=tuple(stratum_label(stratum) for stratum in report.thin),
        )
        self._emit(CoverageComputed(report=result))
        return result

    def _write_artifacts(self, out: str, collection: str, report: CoverageReport) -> tuple[Path, Path]:
        """Write the coverage report YAML and its markdown summary under `out`, named after the collection."""
        require_slug(collection, source="the coverage artifact's collection")
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = out_dir / f"{collection}.coverage.yaml"
        markdown_path = out_dir / f"{collection}.coverage.md"
        save_coverage_report(report, yaml_path)
        markdown_path.write_text(render_coverage_markdown(report), encoding="utf-8")
        return yaml_path, markdown_path

    def _build_report(self, command: ComputeCoverage, inventory: StratumInventory, allocation: QuotaAllocation) -> CoverageReport:
        """Build the census-only coverage report, or with `command.dataset` set, an audit of that dataset's version."""
        if command.dataset is None:
            target = {cell.stratum: cell.quota for cell in allocation.cells}
            return build_coverage_report(
                inventory=inventory, target=target, thresholds=self._config.thresholds, min_pool=self._config.min_pool_warn
            )
        dataset_path = self._resolve_dataset(command.dataset, command.collection)
        covered = covered_by_band(load_dataset(dataset_path))
        return build_coverage_report(
            inventory=band_inventory(inventory, covered),
            target=band_target(allocation),
            thresholds=self._config.thresholds,
            audit=AuditedVersion(dataset_version=dataset_path.name, version_chain=version_chain(dataset_path)),
            min_pool=self._config.min_pool_warn,
        )

    def _resolve_dataset(self, dataset: str, collection: str) -> Path:
        """Resolve the audited dataset path: the `current` pointer for the collection, or an explicit path."""
        if dataset == CURRENT_DATASET:
            resolved = read_current(self._datasets_dir, collection)
            if resolved is None:
                msg = f"no current dataset pointer for collection {collection!r} in {self._datasets_dir}"
                raise ValueError(msg)
            return resolved
        return Path(dataset)

    def _emit(self, event: Event) -> None:
        """Append an event to the bus-drained collector."""
        self.events.append(event)


# -- freshness handler -------------------------------------

# How many most-invalidating pages the report carries for the CLI's top-stale-pages display.
_TOP_STALE_PAGES = 5


class ScoreDatasetHandler:
    """Handles `ScoreDataset`: score a dataset with search alone under its search setup, per document, and write the score report.

    It never writes the database or changes the dataset file.
    """

    def __init__(self, search: Searcher, sources: HitSources) -> None:
        """Bind the search and the page-to-source lookup.

        Args:
            search: The search every case runs through.
            sources: Reads the source document each page names.
        """
        self._search = search
        self._sources = sources
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> DatasetScored:
        """The bus entry point, which narrows `command` to `ScoreDataset`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `ScoreDataset`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, ScoreDataset, handler="ScoreDatasetHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: ScoreDataset) -> DatasetScored:
        """Score every case of the dataset and write the report.

        Args:
            command: The dataset, the knowledge base searched, the search setup and the output directory.

        Returns:
            The setup, the means, the case count, what named no source document and the two files written.
        """
        dataset = Path(command.dataset)
        score = await score_dataset(
            dataset, collection=command.collection, search=self._search, sources=self._sources, setup_name=command.setup
        )
        yaml_path, markdown_path = write_score_report(score, dataset=dataset, collection=command.collection, out_dir=Path(command.out))
        return DatasetScored(
            dataset=dataset.name,
            collection=command.collection,
            setup=score.setup,
            cases=len(score.cases),
            means=score.means,
            unmapped_hits=score.unmapped_hits,
            unmappable_gold=score.unmappable_gold,
            cases_without_gold=score.cases_without_gold,
            out=str(yaml_path),
            markdown=str(markdown_path),
        )


class CheckGeneratorHandler:
    """Handles `CheckGenerator`: score both datasets under every search setup, compare their rankings and write the reports.

    It writes one score report per dataset and setup, then the check report. It never writes the database or changes
    either dataset file.
    """

    def __init__(self, search: Searcher, sources: HitSources) -> None:
        """Bind the search and the page-to-source lookup.

        Args:
            search: The search every case runs through.
            sources: Reads the source document each page names.
        """
        self._search = search
        self._sources = sources
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> GeneratorChecked:
        """The bus entry point, which narrows `command` to `CheckGenerator`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `CheckGenerator`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, CheckGenerator, handler="CheckGeneratorHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: CheckGenerator) -> GeneratorChecked:
        """Score both datasets under every search setup, compare how each ranks the setups and write the reports.

        Args:
            command: The two datasets, the knowledge base searched and the output directory.

        Returns:
            Both rankings, tau-b, the verdict and the two check files written.

        Raises:
            ValueError: When the two datasets would write a score report of the same name.
            FileNotFoundError: When either dataset file does not exist. The message names the missing path.
            SetupsTiedError: When either dataset gives every setup the same nDCG@10 at the precision the check report
                prints. The message adds the folder the score reports were already written to.
        """
        human = Path(command.human)
        generated = Path(command.generated)
        out_dir = Path(command.out)
        scorer = _ReportedScorer(self._search, self._sources, out_dir)
        try:
            check = await check_generator(human, generated, collection=command.collection, scorer=scorer)
        except SetupsTiedError as refusal:
            msg = f"{refusal}. The score report of every setup is already in {out_dir}"
            raise SetupsTiedError(msg) from refusal
        yaml_path, markdown_path = write_generator_check(
            check, human=human, generated=generated, collection=command.collection, out_dir=out_dir
        )
        return GeneratorChecked(
            human=human.name,
            generated=generated.name,
            collection=command.collection,
            check=check,
            out=str(yaml_path),
            markdown=str(markdown_path),
        )


class _ReportedScorer:
    """Scores a dataset under one search setup, writes its score report and answers its mean nDCG@10."""

    def __init__(self, search: Searcher, sources: HitSources, out_dir: Path) -> None:
        self._search = search
        self._sources = sources
        self._out_dir = out_dir

    async def __call__(self, dataset: Path, *, collection: str, setup: str) -> float:
        score = await score_dataset(dataset, collection=collection, search=self._search, sources=self._sources, setup_name=setup)
        write_score_report(score, dataset=dataset, collection=collection, out_dir=self._out_dir)
        return score.means[f"ndcg@{NDCG_K}"]


class CheckFreshnessHandler:
    """Handles `CheckFreshness`: check a dataset version against the live knowledge base and write a freshness report.

    It never writes the database or changes a dataset file, and it succeeds even when every case is
    stale, reporting staleness as a finding. A case whose drafting text cannot be rebuilt counts as
    `mirror_stale`. The structural premises it rechecks are reported and never change a verdict.
    """

    def __init__(self, make_reader: FreshnessReaderFactory, datasets_dir: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Bind the reader factory, the datasets directory and the clock.

        Args:
            make_reader: Opens a fresh freshness reader over one owner session per command.
            datasets_dir: The directory dataset version chains live in, where the `current` pointer is resolved.
            clock: Monotonic seconds for the report duration.
        """
        self._make_reader = make_reader
        self._datasets_dir = datasets_dir
        self._clock = clock
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> FreshnessCheckedReport:
        """The bus entry point, which narrows `command` to `CheckFreshness`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `CheckFreshness`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, CheckFreshness, handler="CheckFreshnessHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: CheckFreshness) -> FreshnessCheckedReport:
        """Check the resolved dataset version against the live knowledge base and write its freshness report.

        Args:
            command: The freshness command, with its collection, optional dataset and output directory.

        Returns:
            The report's summary: verdict counts, stale fraction, the most invalidating pages, premise
            counts and the two files written.

        Raises:
            ValueError: When the dataset resolves to the `current` pointer and the collection has none,
                or the collection is not a plain name.
        """
        started = self._clock()
        dataset_path = self._resolve_dataset(command.dataset, command.collection)
        cases = load_dataset(dataset_path).cases

        async with self._make_reader() as reader:
            verdicts = await freshness_verdicts(reader, cases, collection=command.collection)
            premises = await _sweep_premises(reader, cases)
        report = build_report(command.collection, dataset_path.name, verdicts, premises=premises)
        yaml_path, markdown_path = self._write_artifacts(command.out, dataset_path, report)

        return FreshnessCheckedReport(
            collection=report.collection,
            dataset_version=report.dataset_version,
            counts={str(verdict): count for verdict, count in report.counts.items()},
            staleness_fraction=report.staleness_fraction,
            out=str(yaml_path),
            markdown=str(markdown_path),
            top_stale_pages=ranked_stale_pages(report.stale_by_page)[:_TOP_STALE_PAGES],
            duration_seconds=self._clock() - started,
            n_premise_checked=len(report.structural),
            n_premise_broken=report.n_premise_broken,
        )

    def _write_artifacts(self, out: str, dataset_path: Path, report: FreshnessReport) -> tuple[Path, Path]:
        """Write the freshness report YAML and its markdown summary under `out`, named after the version."""
        out_dir = Path(out)
        out_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = out_dir / f"{dataset_path.stem}.freshness.yaml"
        markdown_path = out_dir / f"{dataset_path.stem}.freshness.md"
        save_freshness_report(report, yaml_path)
        markdown_path.write_text(render_freshness_markdown(report), encoding="utf-8")
        return yaml_path, markdown_path

    def _resolve_dataset(self, dataset: str | None, collection: str) -> Path:
        """Resolve the swept dataset path: the `current` pointer (None or `"current"`), or an explicit path."""
        if dataset is None or dataset == CURRENT_DATASET:
            resolved = read_current(self._datasets_dir, collection)
            if resolved is None:
                msg = f"no current dataset pointer for collection {collection!r} in {self._datasets_dir}"
                raise ValueError(msg)
            return resolved
        return Path(dataset)


async def _sweep_premises(reader: FreshnessReader, cases: Sequence[EvaluationCase]) -> tuple[CasePremise, ...]:
    """One structural premise check per checkable case, against the live graph, for the report only."""
    refs = case_freshness_refs(cases)
    reads = premise_reads(refs)
    return evaluate_premises(
        refs,
        present_pairs=await reader.edge_pairs_present(pairs=reads.edge_pairs),
        inbound_fans=await reader.inbound_fans(keys=reads.inbound_keys),
        outbound_fans=await reader.outbound_fans(keys=reads.outbound_keys),
    )


async def _dataset_staleness(make_reader: FreshnessReaderFactory | None, cases: Sequence[EvaluationCase], *, collection: str) -> float | None:
    """The stale fraction of `cases` against the live knowledge base, or None when `make_reader` is None."""
    if make_reader is None:
        return None
    async with make_reader() as reader:
        verdicts = await freshness_verdicts(reader, cases, collection=collection)
    return staleness_fraction(verdicts)


# -- freeze handler -----------------------------------------

# The trailing `.v{N}` version number on a dataset file stem, extracted for the frozen split's pin.
_VERSION_NUMBER = re.compile(r"\.v(\d+)$")


def _dataset_version_number(dataset_path: Path) -> int:
    """The 1-based version number parsed from a `<name>.v{N}.yaml` path (0 for an unnumbered file)."""
    match = _VERSION_NUMBER.search(dataset_path.stem)
    return int(match.group(1)) if match else 0


class FreezeDatasetHandler:
    """Handles `FreezeDataset`: carve a frozen held-out split of a dataset version into the datasets directory.

    The version is resolved from the `current` pointer, a `NAME:vN` chain version or a path, and its
    freshness is recorded as `staleness_at_freeze`. The split is written as a new `<name>.frozen.v{M}.json`
    file and the chain's `.frozen.current` pointer moves to it. It never writes the database, changes a
    dataset version or touches an earlier frozen version, which a new freeze supersedes but never deletes.
    """

    def __init__(
        self,
        make_reader: FreshnessReaderFactory,
        datasets_dir: Path,
        *,
        frozen_fraction: float,
        frozen_min_per_stratum: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        make_salt: Callable[[], str] = lambda: secrets.token_hex(8),
    ) -> None:
        """Bind the reader factory, the datasets directory, the freeze settings and the time and salt sources.

        Args:
            make_reader: Opens a fresh freshness reader over one owner session per command, for the
                `staleness_at_freeze` stamp.
            datasets_dir: The directory dataset version chains live in, where the frozen split is written.
            frozen_fraction: The default fraction of a version to freeze. A command's `--fraction` overrides it.
            frozen_min_per_stratum: The floor below which a thin stratum folds into its parent difficulty band.
            now: The wall clock for the freeze stamp.
            make_salt: Makes a fresh carve salt for each freeze.
        """
        self._make_reader = make_reader
        self._datasets_dir = datasets_dir
        self._frozen_fraction = frozen_fraction
        self._frozen_min_per_stratum = frozen_min_per_stratum
        self._now = now
        self._make_salt = make_salt
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> DatasetFrozenReport:
        """The bus entry point, which narrows `command` to `FreezeDataset`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `FreezeDataset`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, FreezeDataset, handler="FreezeDatasetHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: FreezeDataset) -> DatasetFrozenReport:
        """Measure the resolved version's staleness, carve the frozen split, and write it.

        Args:
            command: The freeze command, with its collection, optional dataset and optional fraction.

        Returns:
            The freeze summary: the frozen version, the frozen case count per stratum, the staleness at
            freeze and the file written.

        Raises:
            ValueError: When the dataset resolves to a `current` pointer that is absent, a `NAME:vN`
                version is not a number, or the dataset chain name is not a plain name.
        """
        dataset_path = self._resolve_dataset(command.dataset, command.collection)
        cases = load_dataset(dataset_path).cases
        staleness = await _dataset_staleness(self._make_reader, cases, collection=command.collection) or 0.0
        fraction = command.fraction if command.fraction is not None else self._frozen_fraction
        salt = self._make_salt()
        case_names = carve_frozen(cases, fraction=fraction, salt=salt, min_per_stratum=self._frozen_min_per_stratum)

        name = logical_name(dataset_path)
        frozen_version = next_frozen_version(self._datasets_dir, name)
        split = FrozenSplit(
            dataset_name=name,
            dataset_version=_dataset_version_number(dataset_path),
            case_names=case_names,
            salt=salt,
            frozen_fraction=fraction,
            frozen_at=self._now().isoformat(),
            staleness_at_freeze=staleness,
            parent_frozen_version=frozen_version - 1 if frozen_version > 1 else None,
        )
        sidecar = save_frozen_split(split, self._datasets_dir)

        return DatasetFrozenReport(
            dataset_version=dataset_path.name,
            frozen_version=frozen_version,
            n_frozen=len(case_names),
            per_stratum=frozen_stratum_counts(cases, case_names),
            staleness_at_freeze=staleness,
            sidecar=str(sidecar),
        )

    def _resolve_dataset(self, dataset: str | None, collection: str) -> Path:
        """Resolve the path of the version to freeze: the `.current` pointer, a `NAME:vN` chain version, or a path."""
        if dataset is None or dataset == CURRENT_DATASET:
            return self._resolve_current(collection)
        if ":" in dataset:
            name, _, spec = dataset.partition(":")
            if spec == CURRENT_DATASET:
                return self._resolve_current(name)
            return dataset_version_path(self._datasets_dir, name, int(spec.removeprefix("v")))
        return Path(dataset)

    def _resolve_current(self, name: str) -> Path:
        """Resolve a dataset chain's `<name>.current` pointer, failing loudly when it is absent."""
        resolved = read_current(self._datasets_dir, name)
        if resolved is None:
            msg = f"no current dataset pointer for {name!r} in {self._datasets_dir}"
            raise ValueError(msg)
        return resolved


def _frozen_case_names(dataset_path: Path) -> tuple[str, ...]:
    """The case names of the current frozen split of the dataset file's chain, `()` when it was never frozen."""
    name = strip_version(dataset_path.stem)
    split = load_frozen_split(dataset_path.parent, name)
    return () if split is None else split.case_names


class TuneRetrievalHandler:
    """Handles `TuneRetrieval`: run the tuning loop, record each round as a run, and save the winning policy."""

    def __init__(
        self,
        objective: TuningObjective,
        make_proposer: ProposerFactory,
        make_evaluator: PolicyEvaluatorFactory,
        make_run_repo: RunRepositoryFactory,
        *,
        make_freshness_reader: FreshnessReaderFactory | None = None,
        make_reflector: ReflectorFactory | None = None,
        apply_policy: RetrievalPolicyApply | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_run_id: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """Bind the objective, the factories, the policy handler and the time and id sources.

        Args:
            objective: The `TuningObjective` (accept and stop rules, split fraction, metric names). A
                command's `max_rounds` and `token_budget` override it.
            make_proposer: Builds the `PolicyProposer` for the command's model.
            make_evaluator: Builds the `PolicyEvaluator` for the command's model.
            make_run_repo: Opens a fresh run repository over one owner session per command.
            make_freshness_reader: Opens a fresh freshness reader over one owner session per command, for
                the `dataset_staleness` the report records. None leaves it None and refuses every apply.
            make_reflector: Builds the reflector for the command's model, or None to tune without one.
            apply_policy: The `ApplyRetrievalPolicy` handler that makes the winner the active policy.
                None builds the shipped one.
            clock: Monotonic seconds for the report duration.
            now: The wall clock for the recorded run timestamps.
            new_run_id: Makes a fresh run id for each recorded round.
        """
        self._objective = objective
        self._make_proposer = make_proposer
        self._make_evaluator = make_evaluator
        self._make_run_repo = make_run_repo
        self._make_freshness_reader = make_freshness_reader
        self._make_reflector = make_reflector
        self._apply_policy: RetrievalPolicyApply = apply_policy if apply_policy is not None else ApplyRetrievalPolicyHandler()
        self._clock = clock
        self._now = now
        self._new_run_id = new_run_id
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> TuningReport:
        """The bus entry point, which narrows `command` to `TuneRetrieval`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `TuneRetrieval`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, TuneRetrieval, handler="TuneRetrievalHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: TuneRetrieval) -> TuningReport:
        """Run the tuning loop, record each round, save the winning policy, and return the report.

        The loop starts from the knowledge base's active policy when it has one, else the shipped baseline.
        With `command.apply_runtime` set, the winner becomes the active policy only when a frozen split
        exists, the dataset and the frozen split are measured at or below `apply_staleness_max`, the
        winner is not the incumbent, and it beat the incumbent on the frozen split. A refusal is
        reported, never raised, and the dataset's staleness never stops the run itself.

        Args:
            command: The tune command, with its dataset, knowledge base, model, overrides and apply consent.

        Returns:
            The tuning report: rounds, recalls, stop reason, staleness and whether the winner was applied.
        """
        started = self._clock()
        self._emit(TuningStarted(dataset=command.dataset, out=command.out))

        objective = self._objective
        overrides = {
            key: value for key, value in (("max_rounds", command.max_rounds), ("token_budget", command.token_budget)) if value is not None
        }
        if overrides:
            objective = objective.model_copy(update=overrides)

        proposer = self._make_proposer(command.model)
        evaluator = self._make_evaluator(command.model)
        dataset = load_dataset(Path(command.dataset))
        dataset_staleness = await _dataset_staleness(self._make_freshness_reader, dataset.cases, collection=command.collection)

        baseline = effective_baseline_policy(Path(command.knowledge_base))
        frozen_names = _frozen_case_names(Path(command.dataset))
        reflector = self._make_reflector(command.model) if self._make_reflector is not None else None
        result = await TuningLoop(evaluator, proposer, objective, reflector=reflector).run(
            dataset=dataset, baseline=baseline, frozen_names=frozen_names
        )
        applied, refusal = await self._maybe_apply_runtime(
            command, result, dataset=dataset, frozen_names=frozen_names, dataset_staleness=dataset_staleness
        )

        # Record one run per round, then save the winning policy.
        async with self._make_run_repo() as repo:
            for round_ in result.rounds:
                summary = EvaluationRunSummary(
                    run_id=self._new_run_id(),
                    dataset=command.dataset,
                    model_name=command.model or "<default>",
                    variant={"tuning_round": round_.index, "accepted": round_.accepted, "strategy": round_.policy.strategy},
                    metrics=dict(round_.train_metrics),
                    started_at=self._now(),
                    finished_at=self._now(),
                )
                await repo.add_run(summary, ())
                self._emit(RoundEvaluated(index=round_.index, accepted=round_.accepted))
        save_policy(result.winning_policy, Path(command.out))

        report = TuningReport(
            out=command.out,
            n_rounds=len(result.rounds),
            accepted_rounds=sum(1 for round_ in result.rounds if round_.accepted),
            stop_reason=result.stop_reason.value if result.stop_reason else None,
            baseline_recall=result.baseline_train_metrics[objective.primary_metric],
            winner_recall=result.winner_train_metrics[objective.primary_metric],
            winner_val_recall=result.winner_val_metrics[objective.primary_metric] if result.winner_val_metrics else None,
            duration_seconds=self._clock() - started,
            dataset_staleness=dataset_staleness,
            frozen_baseline_recall=result.frozen_baseline[objective.primary_metric] if result.frozen_baseline is not None else None,
            frozen_winner_recall=result.frozen_winner[objective.primary_metric] if result.frozen_winner is not None else None,
            frozen_beat=result.frozen_beat,
            applied_runtime=applied,
            apply_refusal=refusal,
            n_findings=sum(len(round_.findings) for round_ in result.rounds),
            n_predictions_graded=sum(len(round_.prediction_outcomes) for round_ in result.rounds),
            n_prediction_hits=sum(1 for round_ in result.rounds for outcome in round_.prediction_outcomes if outcome.hit),
        )
        self._emit(TuningCompleted(report=report))
        return report

    async def _maybe_apply_runtime(
        self,
        command: TuneRetrieval,
        result: TuningResult,
        *,
        dataset: EvaluationDataset,
        frozen_names: tuple[str, ...],
        dataset_staleness: float | None,
    ) -> tuple[bool, str | None]:
        """Make the winner the active policy when all four guardrails pass, returning `(applied, refusal)`."""
        if not command.apply_runtime:
            return False, None
        refusal = await self._apply_refusal(
            command, result, dataset=dataset, frozen_names=frozen_names, dataset_staleness=dataset_staleness
        )
        if refusal is not None:
            return False, refusal
        await self._apply_policy(
            ApplyRetrievalPolicy(knowledge_base_path=Path(command.knowledge_base), policy=result.winning_policy)
        )
        return True, None

    async def _apply_refusal(
        self,
        command: TuneRetrieval,
        result: TuningResult,
        *,
        dataset: EvaluationDataset,
        frozen_names: tuple[str, ...],
        dataset_staleness: float | None,
    ) -> str | None:
        """The first refusing guardrail's reason, in guardrail order, or `None` when all four pass."""
        if not frozen_names:
            return "no frozen split for the dataset (run `evals freeze` first)"
        dataset_refusal = self._freshness_refusal("dataset", dataset_staleness)
        if dataset_refusal is not None:
            return dataset_refusal
        selected = frozenset(frozen_names)
        frozen_cases = [case for case in dataset.cases if (case.name or "") in selected]
        frozen_staleness = await _dataset_staleness(self._make_freshness_reader, frozen_cases, collection=command.collection)
        frozen_refusal = self._freshness_refusal("frozen-split", frozen_staleness)
        if frozen_refusal is not None:
            return frozen_refusal
        if result.winning_policy == result.baseline_policy:
            return "the winner is the incumbent baseline (nothing to apply)"
        if not result.frozen_beat:
            return "the winner did not beat the incumbent on the frozen split"
        return None

    def _freshness_refusal(self, label: str, staleness: float | None) -> str | None:
        """The freshness check for one leg: refuse unmeasured or above-threshold staleness."""
        threshold = self._objective.apply_staleness_max
        if staleness is None:
            return f"{label} staleness unmeasured (freshness check unwired)"
        if staleness > threshold:
            return f"{label} staleness {staleness:.3f} exceeds apply_staleness_max {threshold:.3f}"
        return None

    def _emit(self, event: Event) -> None:
        """Append an event to the bus-drained collector."""
        self.events.append(event)


class RollbackPolicyHandler:
    """Handles `RollbackPolicy`: restore the knowledge base's previous retrieval policy."""

    def __init__(self, apply_policy: RetrievalPolicyApply | None = None) -> None:
        """Bind the policy handler.

        Args:
            apply_policy: The `ApplyRetrievalPolicy` handler the restore goes through. None builds the shipped one.
        """
        self._apply_policy: RetrievalPolicyApply = apply_policy if apply_policy is not None else ApplyRetrievalPolicyHandler()
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> PolicyRolledBackReport:
        """The bus entry point, which narrows `command` to `RollbackPolicy`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `RollbackPolicy`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, RollbackPolicy, handler="RollbackPolicyHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: RollbackPolicy) -> PolicyRolledBackReport:
        """Restore the knowledge base's previous retrieval policy, its latest history snapshot.

        Args:
            command: The rollback command naming the knowledge base.

        Returns:
            The snapshot now live, and the snapshot the outgoing policy was kept as.

        Raises:
            ValueError: When the knowledge base has no policy history.
        """
        report = await self._apply_policy(ApplyRetrievalPolicy(knowledge_base_path=Path(command.knowledge_base)))
        restored = report.rolled_back_from
        if restored is None:  # pragma: no cover (a restore always sets rolled_back_from)
            msg = "the restore returned no rolled_back_from"
            raise RuntimeError(msg)
        return PolicyRolledBackReport(
            restored_from=str(restored), history_snapshot=str(report.history_snapshot) if report.history_snapshot is not None else None
        )


# -- recommendation handler ----------------------------------------------

_N_TOOL_CALLS = "n_tool_calls"


class RecommendStructureHandler:
    """Handles `RecommendStructure`: diagnose one baseline run structurally and draft a gated recommendation.

    It writes the recommendation YAML, a draft ADR beside it and the baseline run. It never applies a
    schema edit.
    """

    def __init__(
        self,
        make_evaluator: PolicyEvaluatorFactory,
        make_gateway: KnowledgeBaseGatewayFactory,
        make_proposer: RecommendationProposerFactory,
        make_run_repo: RunRepositoryFactory,
        settings: RecommendationSettings,
        knowledge_base_path: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_run_id: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """Bind the factories, the recommendation settings, the knowledge base root and the time and id sources.

        Args:
            make_evaluator: Builds the `PolicyEvaluator` for the command's model, which runs the baseline.
            make_gateway: Opens a fresh knowledge base gateway over one owner session per command.
            make_proposer: Builds the `RecommendationProposer` for the command's model.
            make_run_repo: Opens a fresh run repository over one owner session per command.
            settings: The recommendation settings (section budget, hop horizon, evidence floor, proposal cap).
            knowledge_base_path: The knowledge base root the active policy and the schema are read from.
            clock: Monotonic seconds for the report duration.
            now: The wall clock for the recommendation and run timestamps.
            new_run_id: Makes the run id of the recorded baseline run.
        """
        self._make_evaluator = make_evaluator
        self._make_gateway = make_gateway
        self._make_proposer = make_proposer
        self._make_run_repo = make_run_repo
        self._settings = settings
        self._knowledge_base_path = knowledge_base_path
        self._clock = clock
        self._now = now
        self._new_run_id = new_run_id
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> RecommendationReport:
        """The bus entry point, which narrows `command` to `RecommendStructure`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `RecommendStructure`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, RecommendStructure, handler="RecommendStructureHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: RecommendStructure) -> RecommendationReport:
        """Run the baseline evaluation, diagnose it structurally, draft and gate a recommendation, and write it.

        The baseline policy is `command.policy` when given, else the knowledge base's active policy, else
        the shipped baseline. With `command.simulate` set, each measurable restructure proposal also
        carries the simulated split's measured rank change.

        Args:
            command: The recommend command, with its dataset, collection, model, optional policy and output path.

        Returns:
            The recommendation report: the two files written, the evidence grade, the proposal and finding
            counts and the run id.
        """
        started = self._clock()
        self._emit(RecommendationStarted(dataset=command.dataset, out=command.out))

        policy, policy_source, evidence_grade = resolve_baseline_policy(command.policy, self._knowledge_base_path)
        dataset = load_dataset(Path(command.dataset))
        name_by_question = {case.inputs.question: (case.name or case.inputs.question) for case in dataset.cases}

        evaluator = self._make_evaluator(command.model)
        report = await evaluator.evaluate(policy, dataset=dataset)
        base = diagnose(report)
        recall_key = primary_at_k_key(base.overall, prefix=RECALL_PREFIX)

        spec = load_knowledge_base_schema(self._knowledge_base_path)
        async with self._make_gateway() as gateway:
            structural = await structural_diagnosis(
                base, report, gateway, evidence_grade=evidence_grade, collection=command.collection, settings=self._settings
            )
            knowledge_base = await gateway.stamp(collection=command.collection)

        # The proposer and the gate must see dataset case names, which verification later joins on.
        remapped = remap_diagnosis(structural, name_by_question)
        proposer = self._make_proposer(command.model)
        draft = await proposer.propose(remapped, spec)

        run_id = self._new_run_id()
        created_at = self._now()
        baseline = BaselineSnapshot(
            dataset=command.dataset,
            run_id=run_id,
            policy_source=policy_source,
            evidence_grade=evidence_grade,
            knowledge_base=knowledge_base,
            overall=base.overall,
            cases=case_snapshots(report, name_by_question, recall_key),
        )
        recommendation = gate_recommendation(
            draft,
            remapped,
            spec,
            baseline=baseline,
            collection=command.collection,
            created_at=created_at,
            min_cases_per_proposal=self._settings.min_cases_per_proposal,
            max_proposals=self._settings.max_proposals,
        )
        if command.simulate:
            question_by_name = {name: question for question, name in name_by_question.items()}
            async with self._make_gateway() as simulation_gateway:
                recommendation = await simulate_restructures(
                    recommendation,
                    remapped,
                    simulation_gateway,
                    question_by_name=question_by_name,
                    token_budget=self._settings.section_token_budget,
                    collection=command.collection,
                )

        out_path = Path(command.out)
        save_recommendation(recommendation, out_path)
        adr_path = out_path.with_name(out_path.stem + ".adr.md")
        adr_path.write_text(render_draft_adr(recommendation), encoding="utf-8")

        async with self._make_run_repo() as repo:
            summary = EvaluationRunSummary(
                run_id=run_id,
                dataset=command.dataset,
                model_name=command.model or "<default>",
                variant={
                    "recommendation": True,
                    "evidence_grade": evidence_grade,
                    "policy_source": policy_source,
                    "collection": command.collection,
                },
                metrics=dict(base.overall),
                started_at=created_at,
                finished_at=self._now(),
            )
            await repo.add_run(summary, _case_rows(report, name_by_question))

        report_model = RecommendationReport(
            recommendation_path=str(out_path),
            adr_path=str(adr_path),
            collection=command.collection,
            dataset=command.dataset,
            evidence_grade=evidence_grade,
            n_proposals=len(recommendation.proposals),
            n_findings=len(recommendation.findings),
            n_simulated=sum(1 for proposal in recommendation.proposals if proposal.evidence.simulation is not None),
            top_proposals=tuple(proposal.id for proposal in recommendation.proposals),
            run_id=run_id,
            duration_seconds=self._clock() - started,
        )
        self._emit(RecommendationCompleted(report=report_model))
        return report_model

    def _emit(self, event: Event) -> None:
        """Append an event to the bus-drained collector."""
        self.events.append(event)


class VerifyRecommendationHandler:
    """Handles `VerifyRecommendation`: rerun a recommendation's dataset and attribute the measured change.

    It reruns the dataset under the policy source the baseline recorded, splits the baseline cases into
    retained and dropped, judges each prediction mechanically and attributes knowledge base drift through
    the index manifest. It writes the `.verify.md` report beside the recommendation and records the run
    with its case rows.
    """

    def __init__(
        self,
        make_evaluator: PolicyEvaluatorFactory,
        make_gateway: KnowledgeBaseGatewayFactory,
        make_run_repo: RunRepositoryFactory,
        knowledge_base_path: Path,
        *,
        make_freshness_reader: FreshnessReaderFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_run_id: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """Bind the factories, the knowledge base root and the time and id sources.

        Args:
            make_evaluator: Builds the `PolicyEvaluator` for the command's model, which runs the rerun.
            make_gateway: Opens a fresh knowledge base gateway over one owner session per command.
            make_run_repo: Opens a fresh run repository over one owner session per command.
            knowledge_base_path: The knowledge base root the active policy is read from.
            make_freshness_reader: Opens a fresh freshness reader over one owner session per command, for
                the `dataset_staleness` the report records. None leaves it None. Staleness never stops
                the verification.
            clock: Monotonic seconds for the report duration.
            now: The wall clock for the report and run timestamps.
            new_run_id: Makes the run id of the recorded verify run.
        """
        self._make_evaluator = make_evaluator
        self._make_gateway = make_gateway
        self._make_run_repo = make_run_repo
        self._knowledge_base_path = knowledge_base_path
        self._make_freshness_reader = make_freshness_reader
        self._clock = clock
        self._now = now
        self._new_run_id = new_run_id
        self.events: list[Event] = []

    async def __call__(self, command: Command) -> VerifyReport:
        """The bus entry point, which narrows `command` to `VerifyRecommendation`, refusing any other, and handles it.

        Args:
            command: The command to handle, which must be a `VerifyRecommendation`.

        Returns:
            What `handle` returns.
        """
        command = narrow_command(command, VerifyRecommendation, handler="VerifyRecommendationHandler")  # pragma: no cover (the bus routes by type)
        return await self.handle(command)

    async def handle(self, command: VerifyRecommendation) -> VerifyReport:
        """Rerun the dataset, compare it with the baseline snapshot, judge the predictions, and write the report.

        A baseline that ran under an explicit policy file is rerun under the active policy, or the shipped
        baseline, and the report notes the change.

        Args:
            command: The verify command, with its recommendation path, optional dataset and model.

        Returns:
            The verify report: retained and dropped counts, prediction verdicts, mean changes, the drift
            warning, staleness and the run id.
        """
        started = self._clock()
        self._emit(VerificationStarted(recommendation=command.recommendation))

        recommendation_path = Path(command.recommendation)
        recommendation = load_recommendation(recommendation_path)
        dataset_path = Path(command.dataset) if command.dataset else Path(recommendation.baseline.dataset)
        policy, policy_note = resolve_rerun_policy(recommendation.baseline.policy_source, self._knowledge_base_path)

        dataset = load_dataset(dataset_path)
        dataset_staleness = await _dataset_staleness(self._make_freshness_reader, dataset.cases, collection=recommendation.collection)
        name_by_question = {case.inputs.question: (case.name or case.inputs.question) for case in dataset.cases}
        evaluator = self._make_evaluator(command.model)
        report = await evaluator.evaluate(policy, dataset=dataset)
        base = diagnose(report)
        recall_key = primary_at_k_key(base.overall, prefix=RECALL_PREFIX)
        current_recalls = {snap.case_name: snap.recall for snap in case_snapshots(report, name_by_question, recall_key)}

        gold_pages = list(dict.fromkeys(gold for snapshot in recommendation.baseline.cases for gold in snapshot.gold_ids))
        async with self._make_gateway() as gateway:
            kinds = await gateway.page_types(gold_pages, collection=recommendation.collection)
            current_stamp = await gateway.stamp(collection=recommendation.collection)
            changed = await gateway.changed_paths(recommendation.created_at, collection=recommendation.collection)

        outcome = verify_recommendation(
            recommendation, current_recalls=current_recalls, existing_pages=set(kinds), current_stamp=current_stamp, changed_paths=changed
        )

        verified_at = self._now()
        out_path = recommendation_path.with_name(recommendation_path.stem + ".verify.md")
        out_path.write_text(render_verify_markdown(recommendation, outcome, verified_at=verified_at, policy_note=policy_note), encoding="utf-8")

        run_id = self._new_run_id()
        async with self._make_run_repo() as repo:
            summary = EvaluationRunSummary(
                run_id=run_id,
                dataset=str(dataset_path),
                model_name=command.model or "<default>",
                variant={
                    "recommendation_verify": True,
                    "recommendation": command.recommendation,
                    "collection": recommendation.collection,
                    "policy_source": recommendation.baseline.policy_source,
                },
                metrics=dict(base.overall),
                started_at=verified_at,
                finished_at=self._now(),
            )
            await repo.add_run(summary, _case_rows(report, name_by_question))

        holds = [verdict.holds for verdict in outcome.verdicts]
        report_model = VerifyReport(
            recommendation=command.recommendation,
            out=str(out_path),
            dataset=str(dataset_path),
            collection=recommendation.collection,
            n_retained=len(outcome.retained),
            n_dropped=len(outcome.dropped),
            predictions_held=sum(1 for held in holds if held is True),
            predictions_failed=sum(1 for held in holds if held is False),
            predictions_unmeasurable=sum(1 for held in holds if held is None),
            targeted_mean_delta=outcome.targeted_mean_delta,
            untargeted_mean_delta=outcome.untargeted_mean_delta,
            drift_warning=outcome.drift.warning,
            run_id=run_id,
            duration_seconds=self._clock() - started,
            dataset_staleness=dataset_staleness,
        )
        self._emit(VerificationCompleted(report=report_model))
        return report_model

    def _emit(self, event: Event) -> None:
        """Append an event to the bus-drained collector."""
        self.events.append(event)



def _case_rows(report: ReportLike, name_by_question: Mapping[str, str]) -> list[EvaluationCaseResultRow]:
    """One populated `EvaluationCaseResultRow` per report case, named by its dataset case name."""
    rows: list[EvaluationCaseResultRow] = []
    for case in report.cases:
        scores = {name: float(result.value) for name, result in case.scores.items()}
        rows.append(
            EvaluationCaseResultRow(
                case_name=name_by_question.get(case.inputs.question, case.inputs.question),
                scores=dict(scores),
                n_tool_calls=int(scores.get(_N_TOOL_CALLS, 0.0)),
                total_tokens=int(sum(value for name, value in case.metrics.items() if name.endswith("_tokens"))),
                duration_s=case.total_duration,
            )
        )
    return rows
