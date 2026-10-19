"""The evals commands, the events their handlers emit, the reports they return and the ports they depend on.

A host dispatches each command on the bus. A completed event carries the same report its handler returns. Each
repository, reader and gateway factory opens a fresh resource over a new owner session on every call, one per
command, and never hands out a shared one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from core.cqrs.contracts import Command, Event
from pydantic import BaseModel, ConfigDict, field_validator

from engram.evals.generation.contracts import TopUpPlan
from engram.evals.generator_check.contracts import GeneratorCheck
from engram.evals.scoring.contracts import DEFAULT_SEARCH_SETUP, search_setup
from engram.evals.shared.contracts import Archetype

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime

    from engram.evals.generation.contracts import CaseDrafter, CaseFreshnessRef, StratumInventory, SupportsSeeding
    from engram.evals.recommendation.contracts import (
        CandidateEdge,
        KnowledgeBaseStamp,
        MinedLink,
        OversizedSection,
        RecommendationProposer,
        SectionStat,
    )
    from engram.evals.shared.contracts import EvaluationCaseResultRow, EvaluationRunSummary
    from engram.evals.tuning.contracts import PolicyEvaluator, PolicyProposer
    from engram.kb.contracts import ApplyRetrievalPolicy, RetrievalPolicyReport

__all__ = [
    "CheckFreshness",
    "CheckGenerator",
    "ComputeCoverage",
    "CoverageComputed",
    "CoverageComputedReport",
    "DatasetFrozenReport",
    "DrafterFactory",
    "FreezeDataset",
    "FreshnessCheckedReport",
    "FreshnessReader",
    "FreshnessReaderFactory",
    "GenerateEvalDataset",
    "GenerateEvalDatasetReport",
    "GenerationCompleted",
    "GenerationStarted",
    "GeneratorChecked",
    "InventoryReaderFactory",
    "KnowledgeBaseGatewayFactory",
    "PolicyEvaluatorFactory",
    "ProposerFactory",
    "RecommendStructure",
    "RecommendationCompleted",
    "RecommendationKnowledgeBaseGateway",
    "RecommendationProposerFactory",
    "RecommendationReport",
    "RecommendationStarted",
    "RetrievalPolicyApply",
    "RoundEvaluated",
    "RunRepositoryFactory",
    "SeedRepositoryFactory",
    "SupportsCoverageReader",
    "SupportsRunPersistence",
    "TuneRetrieval",
    "TuningCompleted",
    "TuningReport",
    "TuningStarted",
    "VerificationCompleted",
    "VerificationStarted",
    "VerifyRecommendation",
    "VerifyReport",
]


# -- generation: command and events -----------------------------------------


class GenerateEvalDataset(Command):
    """Generate a dataset of structural cases for one knowledge base and write it as the chain's next version.

    A top-up never changes its parent version. `dry_run` applies to a top-up only.

    Attributes:
        collection: The knowledge base the structural seeds are drawn from.
        page_ids: The pages the seeds are narrowed to. Empty means every page.
        archetypes: The structural shapes to cover. Empty means all five.
        model: The drafter's model spec, such as an Azure deployment or `"test-model"`. `None` means the
            configured default.
        out: The base dataset YAML path. The handler takes the dataset's name and directory from it and
            writes the next immutable `<name>.v{N}.yaml` version.
        count: The number of cases to generate, overriding the configured total. `None` keeps the total.
        salt: The sampling salt the stratified seed draw hashes with. `None` means the configured salt,
            or else a generated one, which the version's stamp records.
        top_up_from: The parent version to top up instead of drawing afresh: `"current"` for the
            `<name>.current` pointer of the `out` chain, or else a version path. A top-up drafts only the
            under-covered strata, retires the cases the new draw displaces and writes the next version.
            With no `salt` it reuses the parent stamp's salt.
        dry_run: Whether a top-up puts its per-stratum generate and retire plan on the report and writes
            nothing. A fresh run ignores it.
    """

    collection: str
    page_ids: tuple[str, ...] = ()
    archetypes: tuple[Archetype, ...] = ()
    model: str | None = None
    out: str
    count: int | None = None
    salt: str | None = None
    top_up_from: str | None = None
    dry_run: bool = False


class GenerationStarted(Event):
    """Emitted when a generation run begins, naming the knowledge base and the dataset's base path."""

    collection: str
    out: str


class GenerationCompleted(Event):
    """Emitted last, carrying the full `GenerateEvalDatasetReport`."""

    report: GenerateEvalDatasetReport


# -- generation ports ----------------------------------------------------------


@runtime_checkable
class DrafterFactory(Protocol):
    """Builds a `CaseDrafter` for the model spec a generate command names."""

    def __call__(self, model: str | None) -> CaseDrafter:
        """Build a `CaseDrafter` for `model`.

        Args:
            model: The model spec to draft with, or `None` for the configured default.

        Returns:
            The drafter for one generation run.
        """
        ...


@runtime_checkable
class SeedRepositoryFactory(Protocol):
    """Opens a fresh seed repository over a new owner session, one per generate command.

    Every seeder in that command's run shares the one repository.
    """

    def __call__(self) -> AbstractAsyncContextManager[SupportsSeeding]:
        """Open a fresh seed repository over a new owner session for one command.

        Returns:
            An async context manager holding the repository for the command's duration.
        """
        ...


@runtime_checkable
class SupportsCoverageReader(Protocol):
    """The knowledge base reads the generate handler and a results file need: the census, the fingerprint, the index build and the page ids.

    A fresh run never reads `page_ids`. A top-up reads it to retire each case whose gold page is gone as `gold_gone`. Only
    an evaluation run's results file reads `index_build`.
    """

    async def inventory(self, *, collection: str) -> StratumInventory:
        """Count the seed pool of each stratum in `collection`.

        Args:
            collection: The knowledge base to count.

        Returns:
            The per-stratum census the version stamp's coverage summary is built from.
        """
        ...

    async def knowledge_base_fingerprint(self, *, collection: str) -> str:
        """The knowledge base's identity for the version stamp: its index fingerprint hash plus its name.

        Args:
            collection: The knowledge base to identify.

        Returns:
            The fingerprint.
        """
        ...

    async def index_build(self, *, collection: str) -> str:
        """The index build `collection` was last indexed into: its indexer version, embedding model, vocabulary and switches on.

        Args:
            collection: The knowledge base to read.

        Returns:
            The parts separated by spaces, or `not indexed` for a knowledge base never indexed.
        """
        ...

    async def page_ids(self, *, collection: str) -> frozenset[str]:
        """The page ids currently in `collection`, which a top-up checks each case's gold pages against.

        Args:
            collection: The knowledge base to list.

        Returns:
            Every page id the knowledge base holds now.
        """
        ...


@runtime_checkable
class InventoryReaderFactory(Protocol):
    """Opens a fresh coverage reader over a new owner session, one per generate command."""

    def __call__(self) -> AbstractAsyncContextManager[SupportsCoverageReader]:
        """Open a fresh coverage reader over a new owner session for one command.

        Returns:
            An async context manager holding the reader for the command's duration.
        """
        ...


class FreshnessReader(Protocol):
    """The knowledge base reads a freshness check needs: the current page state and the drafting text sources.

    Every read is a SELECT over the owner session and writes nothing.
    """

    async def page_state(self, *, collection: str, page_ids: frozenset[str]) -> Mapping[str, str]:
        """Map each of `page_ids` still in `collection` to its card body hash, in one SELECT.

        An id missing from the mapping has left the knowledge base. The cases citing it are `gold_gone`.

        Args:
            collection: The knowledge base to read.
            page_ids: The anchored and gold page ids of the checked cases.

        Returns:
            The current page id to card body hash mapping.
        """
        ...

    async def mirror_sources(self, *, refs: Sequence[CaseFreshnessRef]) -> Mapping[str, tuple[str, ...]]:
        """Read the current drafting text sources of each mirror-era case.

        Args:
            refs: The checked cases.

        Returns:
            The source texts per case name, with no entry for a case whose basis is gone.
        """
        ...

    async def edge_pairs_present(self, *, pairs: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        """Which of the requested `(src, dst)` pairs exist in `kb.edges`, for the trace premises.

        Args:
            pairs: The `(src, dst)` pairs to look up.

        Returns:
            The pairs that exist.
        """
        ...

    async def inbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        """Count the distinct inbound sources of each `(rel, dst)` key, for the survey premises.

        Args:
            keys: The `(rel, dst)` keys to count.

        Returns:
            The distinct inbound source count per key.
        """
        ...

    async def outbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        """Count the distinct pages each `(rel, src)` key reaches, for the synthesis premises.

        Args:
            keys: The `(rel, src)` keys to count.

        Returns:
            The distinct reached-page count per key.
        """
        ...


@runtime_checkable
class FreshnessReaderFactory(Protocol):
    """Opens a fresh freshness reader over a new owner session, one per freshness command."""

    def __call__(self) -> AbstractAsyncContextManager[FreshnessReader]:
        """Open a fresh freshness reader over a new owner session for one command.

        Returns:
            An async context manager holding the reader for the command's duration.
        """
        ...


# -- generation report -----------------------------------------------------------


class GenerateEvalDatasetReport(BaseModel):
    """The result of a generation run: where it wrote, how many cases, and how long it took.

    Returned by the handler and carried on `GenerationCompleted`. `out` is the version YAML path actually
    written (`<name>.v{N}.yaml`), and `version` is that version's file name, which `<name>.current` points at.
    `requested_archetypes` is the archetype set the run covered, all five when the command left it empty.

    `retired` is how many parent cases a top-up dropped, 0 for a fresh run. `plan` carries the full `TopUpPlan`
    on a dry-run top-up and is `None` otherwise.
    """

    model_config = ConfigDict(frozen=True)

    out: str
    n_cases: int
    requested_archetypes: tuple[Archetype, ...]
    version: str = ""
    retired: int = 0
    plan: TopUpPlan | None = None
    duration_seconds: float = 0.0


# -- coverage: command, event and report ---


class ComputeCoverage(Command):
    """Compute a knowledge base's coverage report and write it to files.

    The command never changes the database or any dataset file.

    Attributes:
        collection: The knowledge base counted per stratum.
        dataset: `None` for the inventory census alone (what the knowledge base offers per stratum against
            the target allocation). A path, or `"current"` for the `<collection>.current` pointer, audits
            that dataset's actual coverage against the inventory.
        out: The directory the `CoverageReport` YAML and its markdown summary are written under, both named
            after the knowledge base.
    """

    collection: str
    dataset: str | None = None
    out: str


class CoverageComputed(Event):
    """Emitted last, carrying the full `CoverageComputedReport`."""

    report: CoverageComputedReport


class CoverageComputedReport(BaseModel):
    """The result of a coverage run: where it wrote, and the gaps, debt, feasibility manifest and version chain.

    Returned by the handler and carried on `CoverageComputed`. Each stratum is an
    `archetype/difficulty/anchor-page-type` label.

    Attributes:
        out: The coverage report YAML path written.
        markdown: The markdown summary path written beside it.
        collection: The knowledge base the report was produced for.
        dataset_version: The audited dataset's version file name, or `None` for the inventory census alone.
        gaps: The feasible under-target strata, as labels (empty when none).
        over_represented: The over-target strata, as labels (empty when none).
        infeasible: The feasibility manifest, as labels (empty when none).
        debt: The single trendable coverage debt score.
        version_chain: The version file names walked through the dataset stamps' parent links.
        thin: The feasible strata whose seed pool is non-empty but below the `min_pool_warn` floor,
            as labels (empty when none).
    """

    model_config = ConfigDict(frozen=True)

    out: str
    markdown: str
    collection: str
    dataset_version: str | None = None
    gaps: tuple[str, ...] = ()
    over_represented: tuple[str, ...] = ()
    infeasible: tuple[str, ...] = ()
    debt: float = 0.0
    version_chain: tuple[str, ...] = ()
    thin: tuple[str, ...] = ()


# -- freshness: command and report --------------------------------


class CheckFreshness(Command):
    """Check a dataset version for staleness against the knowledge base as it is now and write a report.

    The command never changes the database or any dataset file. It completes even when every case is stale.

    Attributes:
        collection: The knowledge base the dataset's anchors and gold ids are checked against.
        dataset: The version to check. `None` or `"current"` resolves the `<collection>.current`
            pointer, and a path checks that version file.
        out: The directory the `FreshnessReport` YAML and its markdown summary are written under, both
            named after the checked version.
    """

    collection: str
    dataset: str | None = None
    out: str


class FreshnessCheckedReport(BaseModel):
    """The result of a freshness check: where it wrote, the verdict tally, and the staleness fraction.

    Attributes:
        collection: The checked knowledge base's name.
        dataset_version: The checked dataset version's file name.
        counts: The number of cases per verdict name (every verdict present, zeros included).
        staleness_fraction: The stale fraction over the cases that can take a verdict, judged by each case's era.
        out: The freshness report YAML path written.
        markdown: The markdown summary path written beside it.
        top_stale_pages: The pages that invalidated the most cases, as (page id, count), ranked by
            descending count then ascending page id and capped to the top N shown. Empty when no case
            went stale.
        duration_seconds: How long the check took, measured by the injected clock.
        n_premise_checked: How many structural premises the check recomputed. Premises are reported
            and never change a verdict.
        n_premise_broken: How many of those premises are broken.
    """

    model_config = ConfigDict(frozen=True)

    collection: str
    dataset_version: str
    counts: Mapping[str, int]
    staleness_fraction: float
    out: str
    markdown: str
    top_stale_pages: tuple[tuple[str, int], ...] = ()
    duration_seconds: float
    n_premise_checked: int = 0
    n_premise_broken: int = 0


# -- freeze: command and report ------------------------------------


class FreezeDataset(Command):
    """Carve a stratified frozen held-out split from one immutable dataset version and write it beside the version.

    The command writes a frozen-split sidecar and its `.frozen.current` pointer. It never changes the
    database, the dataset version or an earlier frozen version.

    Attributes:
        collection: The knowledge base the frozen cases' staleness is checked against, and the default
            dataset chain name.
        dataset: The version to freeze. `None` or `"current"` resolves the chain's `<name>.current` pointer,
            `"NAME:vN"` or `"NAME:current"` names a chain version, and any other value is a version path.
        fraction: The share of cases to freeze, overriding the configured `frozen_fraction`. `None` keeps
            the configured share.
    """

    collection: str
    dataset: str | None = None
    fraction: float | None = None


class DatasetFrozenReport(BaseModel):
    """The result of a freeze run: the pinned version, the new frozen split, and its spread over the strata.

    Attributes:
        dataset_version: The frozen dataset version's file name, the pinned immutable version.
        frozen_version: The 1-based frozen-split version this freeze wrote.
        n_frozen: How many cases the carve froze.
        per_stratum: The frozen case count per `archetype/difficulty` stratum label.
        staleness_at_freeze: The dataset's stale fraction against the knowledge base at freeze time.
        sidecar: The `<name>.frozen.v{M}.json` sidecar path written.
    """

    model_config = ConfigDict(frozen=True)

    dataset_version: str
    frozen_version: int
    n_frozen: int
    per_stratum: Mapping[str, int]
    staleness_at_freeze: float
    sidecar: str


# -- tuning: command and events ------------------------------


class TuneRetrieval(Command):
    """Tune the retrieval policy against a dataset and write the winning policy to a file.

    Attributes:
        dataset: The dataset YAML the loop tunes against.
        model: The model under test, which the evaluator and proposer factories resolve, such as an Azure
            deployment or `"test-model"`. `None` means the configured default.
        collection: The knowledge base the run is for.
        out: The winning-policy YAML path to write.
        max_rounds: The round limit, overriding the configured `TuningObjective`. `None` keeps the configured limit.
        token_budget: The token budget, overriding the configured `TuningObjective`. `None` keeps the configured budget.
        knowledge_base: The knowledge base root the incumbent baseline policy is read from. With
            `apply_runtime`, the winner is applied to this root's `_meta/active-policy.yaml`.
        apply_runtime: Whether this invocation consents to applying the winner. The handler applies it only
            when all four guardrails pass, and records a refusal on the report instead of failing the run.
    """

    dataset: str
    model: str | None = None
    collection: str
    out: str
    max_rounds: int | None = None
    token_budget: int | None = None
    knowledge_base: str = "."
    apply_runtime: bool = False


class TuningStarted(Event):
    """Emitted when a tuning run begins, naming the dataset and the winning-policy destination."""

    dataset: str
    out: str


class RoundEvaluated(Event):
    """Emitted once per tuning round, carrying its one-based index and whether it was accepted."""

    index: int
    accepted: bool


class TuningCompleted(Event):
    """Emitted last, carrying the full `TuningReport`."""

    report: TuningReport


# -- tuning ports --------------------------


@runtime_checkable
class ProposerFactory(Protocol):
    """Builds a `PolicyProposer` for the model spec a tune command names."""

    def __call__(self, model: str | None) -> PolicyProposer:
        """Build a `PolicyProposer` for `model`.

        Args:
            model: The model spec to propose with, or `None` for the configured default.

        Returns:
            The proposer for one tuning run.
        """
        ...


@runtime_checkable
class PolicyEvaluatorFactory(Protocol):
    """Builds a `PolicyEvaluator` for the model spec a command names."""

    def __call__(self, model: str | None) -> PolicyEvaluator:
        """Build a `PolicyEvaluator` for `model`.

        Args:
            model: The model spec under test, or `None` for the configured default.

        Returns:
            The evaluator for one run.
        """
        ...


@runtime_checkable
class RetrievalPolicyApply(Protocol):
    """Dispatches `ApplyRetrievalPolicy`, the route a tuning run takes to apply its winner to a knowledge base.

    The tuning handler checks the guardrails before it calls. The knowledge base owns its policy file and writes it.
    """

    async def __call__(self, command: ApplyRetrievalPolicy) -> RetrievalPolicyReport:
        """Apply the command's policy, or restore the previous one when it carries none.

        Args:
            command: The policy to apply and the knowledge base root to apply it under.

        Returns:
            The report of what was written.
        """
        ...


@runtime_checkable
class SupportsRunPersistence(Protocol):
    """Saves one evaluation run, the tuning handler's only database write."""

    async def add_run(self, run: EvaluationRunSummary, case_results: Sequence[EvaluationCaseResultRow]) -> None:
        """Save a completed run summary and its per-case rows.

        Args:
            run: The run summary to save.
            case_results: The run's per-case rows, empty for a tuning round's summary.
        """
        ...


@runtime_checkable
class RunRepositoryFactory(Protocol):
    """Opens a fresh run repository over a new owner session, one per tune command."""

    def __call__(self) -> AbstractAsyncContextManager[SupportsRunPersistence]:
        """Open a fresh run repository over a new owner session for one command.

        Returns:
            An async context manager holding the repository for the command's duration.
        """
        ...


# -- tuning report ------------------------------------------------


class TuningReport(BaseModel):
    """The result of a tuning run: where it wrote, how it converged, and the recall it moved.

    Returned by the handler and carried on `TuningCompleted`. The `*_recall` fields hold the objective's
    primary metric on each split.

    Attributes:
        out: The winning-policy YAML path written.
        n_rounds: How many tuning rounds ran.
        accepted_rounds: How many of those rounds were accepted (beat the incumbent on train).
        stop_reason: The convergence trigger that ended the loop, or `None` when no round ran.
        baseline_recall: The baseline's primary metric on the train split.
        winner_recall: The winner's primary metric on the train split.
        winner_val_recall: The winner's primary metric on the validation split, or `None` when
            the hold-out is disabled.
        duration_seconds: The run's wall-clock duration, measured by the injected clock.
        dataset_staleness: The stale fraction of the run's dataset against the knowledge base, reported
            and never blocking the run. `None` when no freshness check was wired.
        frozen_baseline_recall: The baseline's primary metric on the frozen split, or `None`
            when the run had no frozen split.
        frozen_winner_recall: The winner's primary metric on the frozen split, or `None`
            when the run had no frozen split.
        frozen_beat: Whether the winner beat the incumbent on the frozen split under the paired
            guard, or `None` when the run had no frozen split.
        applied_runtime: Whether the run applied its winner to the knowledge base's runtime policy,
            which takes `apply_runtime` and all four guardrails passing.
        apply_refusal: The guardrail that refused the runtime apply, verbatim, or `None` when no
            apply was requested or the apply went through.
        n_findings: How many gated reflective findings the run's rounds carried, `0` when no
            reflector was bound.
        n_predictions_graded: How many reflective predictions were graded against the next round's
            measured bucket deltas.
        n_prediction_hits: How many of those graded predictions hit (delta sign matched direction).
    """

    model_config = ConfigDict(frozen=True)

    out: str
    n_rounds: int
    accepted_rounds: int
    stop_reason: str | None
    baseline_recall: float
    winner_recall: float
    winner_val_recall: float | None = None
    duration_seconds: float = 0.0
    dataset_staleness: float | None = None
    frozen_baseline_recall: float | None = None
    frozen_winner_recall: float | None = None
    frozen_beat: bool | None = None
    applied_runtime: bool = False
    apply_refusal: str | None = None
    n_findings: int = 0
    n_predictions_graded: int = 0
    n_prediction_hits: int = 0


class RollbackPolicy(Command):
    """Restore the knowledge base's latest policy-history snapshot as a new runtime apply.

    The live file name never changes. The outgoing policy is itself snapshotted, so the history
    stays a complete append-only record.

    Attributes:
        knowledge_base: The knowledge base root whose `_meta/policy-history/` chain is read and whose
            `_meta/active-policy.yaml` is rewritten.
    """

    knowledge_base: str = "."


class PolicyRolledBackReport(BaseModel):
    """The result of a policy rollback: what was restored and where the outgoing policy went.

    Attributes:
        restored_from: The history snapshot whose content is now live.
        history_snapshot: The snapshot the outgoing, rolled-back policy was saved as, or `None` when
            history existed but no live file did.
    """

    model_config = ConfigDict(frozen=True)

    restored_from: str
    history_snapshot: str | None = None


# -- recommendation: commands and events -----------------------


class RecommendStructure(Command):
    """Diagnose a dataset's failures, recommend schema changes and write the recommendation to a file.

    Attributes:
        dataset: The dataset YAML the recommendation diagnoses against.
        model: The model spec the baseline run's evaluator and the recommendation proposer resolve, such as
            an Azure deployment or `"test-model"`. `None` means the configured default.
        collection: The knowledge base the detectors read and the recommendation is stamped for.
        policy: A policy YAML that pins the baseline run. `None` falls back to the knowledge base's active
            policy, then to the shipped baseline.
        out: The recommendation YAML path to write. The draft ADR is written beside it.
        simulate: Whether to add shadow split evidence. Each restructure proposal's over-budget gold
            sections are split at paragraphs and ranked against the live knowledge base, and the measured
            movement lands on the proposal's evidence.
    """

    dataset: str
    model: str | None = None
    collection: str
    policy: str | None = None
    out: str
    simulate: bool = False


class VerifyRecommendation(Command):
    """Re-run a retained dataset against a saved recommendation and attribute the measured change.

    Attributes:
        recommendation: The recommendation YAML path to verify.
        model: The model spec the baseline re-run resolves, `None` for the configured default.
        dataset: A dataset that replaces the recommendation's own `baseline.dataset`, or `None` to reuse it.
    """

    recommendation: str
    model: str | None = None
    dataset: str | None = None


class RecommendationStarted(Event):
    """Emitted when a recommendation run begins, naming the dataset and the recommendation destination."""

    dataset: str
    out: str


class RecommendationCompleted(Event):
    """Emitted last, carrying the full `RecommendationReport`."""

    report: RecommendationReport


class VerificationStarted(Event):
    """Emitted when a recommendation-verification run begins, naming the recommendation under verification."""

    recommendation: str


class VerificationCompleted(Event):
    """Emitted last, carrying the full `VerifyReport`."""

    report: VerifyReport


# -- recommendation ports ------------------


@runtime_checkable
class RecommendationKnowledgeBaseGateway(Protocol):
    """The knowledge base reads the recommendation loop needs, behind one port.

    Every read runs over an owner session and writes nothing.
    """

    async def sections_over_budget(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[SectionStat]:
        """Return the sections on `page_ids` whose token count exceeds `token_budget`.

        Args:
            page_ids: The pages to inspect. An empty sequence returns nothing.
            token_budget: The token count a section must exceed to count.
            collection: The knowledge base the pages must belong to.

        Returns:
            One `SectionStat` per over-budget canonical section, ordered by page id then canonical slug.
        """
        ...

    async def page_types(self, page_ids: Sequence[str], *, collection: str) -> Mapping[str, str]:
        """Map each of `page_ids` present in `collection` to its page type.

        Args:
            page_ids: The pages to look up. An empty sequence returns an empty mapping.
            collection: The knowledge base the pages must belong to.

        Returns:
            The page type per page id, leaving out the pages not in `collection`.
        """
        ...

    async def reachable_pairs(
        self, pairs: Sequence[tuple[str, str]], *, max_hops: int, collection: str, extra_edges: Sequence[CandidateEdge] = ()
    ) -> Sequence[bool]:
        """Return, in input order, whether each pair's destination is reachable from its source.

        Args:
            pairs: The `(source, destination)` gold pairs to test. An empty sequence returns nothing.
            max_hops: The largest number of hops a path may take.
            collection: The knowledge base whose `kb.edges` the walk follows.
            extra_edges: Candidate edges to add to the graph for a what-if test, empty for the real graph.

        Returns:
            One boolean per input pair, in the same order, True when the destination is reachable.
        """
        ...

    async def unlifted_links(self, *, collection: str) -> Sequence[MinedLink]:
        """Return the knowledge base's prose links that are not lifted into `kb.edges`.

        Args:
            collection: The knowledge base whose links to read.

        Returns:
            One `MinedLink` per unlifted link.
        """
        ...

    async def stamp(self, *, collection: str) -> KnowledgeBaseStamp:
        """Return the indexed knowledge base's identity for `collection`.

        Two identical knowledge bases give identical stamps.

        Args:
            collection: The knowledge base to stamp.

        Returns:
            The knowledge base stamp.
        """
        ...

    async def changed_paths(self, since: datetime, *, collection: str) -> Sequence[str]:
        """Return the manifest paths indexed after `since`, the set a verify run attributes drift to.

        Args:
            since: The exclusive lower bound, usually the recommendation's creation time.
            collection: The knowledge base whose manifest to read.

        Returns:
            The changed paths, ordered by path.
        """
        ...

    async def oversized_section_bodies(self, page_ids: Sequence[str], *, token_budget: int, collection: str) -> Sequence[OversizedSection]:
        """Return the over-budget sections on `page_ids` with their bodies, the input to the shadow split.

        Args:
            page_ids: The pages to inspect. An empty sequence returns nothing.
            token_budget: The token count a section must exceed to count.
            collection: The knowledge base the pages must belong to.

        Returns:
            One `OversizedSection` per over-budget canonical section, ordered by page id then slug.
        """
        ...

    async def snippet_ranks(self, query: str, snippets: Sequence[str], *, collection: str) -> Sequence[int]:
        """Return each snippet's 1-based would-be rank among the knowledge base's sections for `query`.

        Args:
            query: The question to rank against.
            snippets: The texts to rank. An empty sequence returns nothing.
            collection: The knowledge base whose sections the snippets compete with.

        Returns:
            The 1-based ranks, in `snippets` order.
        """
        ...


@runtime_checkable
class KnowledgeBaseGatewayFactory(Protocol):
    """Opens a fresh knowledge base gateway over a new owner session, one per recommend or verify command."""

    def __call__(self) -> AbstractAsyncContextManager[RecommendationKnowledgeBaseGateway]:
        """Open a fresh knowledge base gateway over a new owner session for one command.

        Returns:
            An async context manager holding the gateway for the command's duration.
        """
        ...


@runtime_checkable
class RecommendationProposerFactory(Protocol):
    """Builds a `RecommendationProposer` for the model spec a recommend command names."""

    def __call__(self, model: str | None) -> RecommendationProposer:
        """Build a `RecommendationProposer` for `model`.

        Args:
            model: The model spec to draft with, or `None` for the configured default.

        Returns:
            The proposer for one recommendation run.
        """
        ...


# -- recommendation reports ----------------------------------------


class RecommendationReport(BaseModel):
    """The result of a recommendation run: what it wrote, how many proposals, and the top ids.

    Returned by the handler and carried on `RecommendationCompleted`.

    Attributes:
        recommendation_path: The recommendation YAML path written.
        adr_path: The draft ADR markdown path written beside it.
        collection: The knowledge base the recommendation was produced for.
        dataset: The dataset the baseline was diagnosed from.
        evidence_grade: Whether the baseline ran under a tuned policy or a raw baseline.
        n_proposals: How many schema proposals passed the checks.
        n_findings: How many free-form findings the recommendation carries.
        n_simulated: How many proposals carry shadow split evidence, 0 unless `simulate` ran.
        top_proposals: The accepted proposals' stable ids, in the recommendation's rank order.
        run_id: The saved `evaluation_runs` row id for the baseline run, or None when it was not saved.
        duration_seconds: The run's wall-clock duration, measured by the injected clock.
    """

    model_config = ConfigDict(frozen=True)

    recommendation_path: str
    adr_path: str
    collection: str
    dataset: str
    evidence_grade: Literal["residual-of-tuned", "raw-baseline"]
    n_proposals: int
    n_findings: int
    n_simulated: int = 0
    top_proposals: tuple[str, ...]
    run_id: str | None = None
    duration_seconds: float = 0.0


class VerifyReport(BaseModel):
    """The result of a recommendation-verification run: the retained cases, the prediction counts, the deltas and drift.

    Returned by the handler and carried on `VerificationCompleted`. The full per-case and per-prediction
    detail lives in the `.verify.md` report written beside the recommendation.

    Attributes:
        recommendation: The recommendation YAML path that was verified.
        out: The verification report markdown path written.
        dataset: The dataset the verification re-ran.
        collection: The knowledge base the recommendation was stamped for.
        n_retained: How many baseline cases the re-run could measure.
        n_dropped: How many baseline cases were dropped, their gold gone or the case missing.
        predictions_held: How many predictions held.
        predictions_failed: How many predictions were falsified.
        predictions_unmeasurable: How many predictions had no retained target cases.
        targeted_mean_delta: The mean recall delta over the retained targeted cases.
        untargeted_mean_delta: The mean recall delta over the retained untargeted cases.
        drift_warning: The warning about drift the recommendation does not explain, or None when every
            change is attributed.
        run_id: The saved `evaluation_runs` row id for the verify run, or None when it was not saved.
        duration_seconds: The run's wall-clock duration, measured by the injected clock.
        dataset_staleness: The stale fraction of the re-run dataset against the knowledge base, reported
            and never blocking the verify. `None` when no freshness check was wired.
    """

    model_config = ConfigDict(frozen=True)

    recommendation: str
    out: str
    dataset: str
    collection: str
    n_retained: int
    n_dropped: int
    predictions_held: int
    predictions_failed: int
    predictions_unmeasurable: int
    targeted_mean_delta: float
    untargeted_mean_delta: float
    drift_warning: str | None = None
    run_id: str | None = None
    duration_seconds: float = 0.0
    dataset_staleness: float | None = None


class ScoreDataset(Command):
    """Score a dataset with search alone under one search setup, per document, and write the score report.

    The command never changes the database or the dataset file. A setup that names no search setup is refused when
    the command is built, and the refusal names every search setup.

    Attributes:
        dataset: The dataset file to score.
        collection: The knowledge base searched.
        out: The directory the score YAML and its markdown summary are written under, named after the dataset and,
            for any setup but the default, the setup.
        setup: The name of the search setup every case's search runs under.
    """

    dataset: str
    collection: str
    out: str
    setup: str = DEFAULT_SEARCH_SETUP

    @field_validator("setup")
    @classmethod
    def _known_setup(cls, setup: str) -> str:
        """`setup` unchanged once it names a search setup."""
        search_setup(setup)
        return setup


class DatasetScored(BaseModel):
    """The result of scoring a dataset: the setup, the means, the case count, what named no source and the two files written.

    Attributes:
        dataset: The scored dataset file's name.
        collection: The knowledge base searched.
        setup: The name of the search setup every case's search ran under.
        cases: How many cases were scored.
        means: The mean of each metric (`ndcg@10`, `recall@1`, `recall@5`, `recall@10`, `mrr`) over the scored cases.
        unmapped_hits: How many hits named no source document.
        unmappable_gold: How many gold ids named a page with no source document.
        cases_without_gold: How many cases were left out of the scores and the means because no gold id named a source document.
        out: The score YAML path written.
        markdown: The markdown summary path written beside it.
    """

    model_config = ConfigDict(frozen=True)

    dataset: str
    collection: str
    setup: str
    cases: int
    means: dict[str, float]
    unmapped_hits: int
    unmappable_gold: int
    cases_without_gold: int
    out: str
    markdown: str


class CheckGenerator(Command):
    """Check a generated dataset against a human-judged one of the same knowledge base by how each ranks the search setups.

    Both datasets are scored under every search setup and each score report is written. The command never changes the
    database or either dataset file.

    Attributes:
        human: The human-judged dataset file.
        generated: The generated dataset file, whose stem names the check report.
        collection: The knowledge base both are searched in.
        out: The directory every score report, the check YAML and its markdown summary are written under.
    """

    human: str
    generated: str
    collection: str
    out: str


class GeneratorChecked(BaseModel):
    """The result of a generator check: both rankings of the search setups, tau-b, the verdict and the two files written.

    Attributes:
        human: The human-judged dataset file's name.
        generated: The generated dataset file's name.
        collection: The knowledge base searched.
        check: Each setup's nDCG@10 and rank in both datasets, Kendall's tau-b, the agreeing setup pairs and the verdict.
        out: The check YAML path written.
        markdown: The markdown summary path written beside it.
    """

    model_config = ConfigDict(frozen=True)

    human: str
    generated: str
    collection: str
    check: GeneratorCheck
    out: str
    markdown: str
