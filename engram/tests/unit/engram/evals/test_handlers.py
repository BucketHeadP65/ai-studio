r"""Unit specs for the `GenerateEvalDatasetHandler`.

The handler is the CQRS seam that wraps `GenerationPipeline` behind the message bus. It owns the IO the
pipeline does not: writing the next immutable dataset version `<name>.v{N}.yaml`, its
`<name>.v{N}.stamp.json` sidecar, and moving the `<name>.current` pointer. Everything else it delegates
to the pipeline, emitting a `GenerationStarted` before and a `GenerationCompleted` after.

Every collaborator is faked network-free, mirroring `test_pipeline.py`, except a real `CaseCurator` over
the deterministic `FakeEmbedding`, which exercises the dedup geometry. `FakeCaseDrafter` quotes verbatim
from each seed's own source bodies, so the quote check keeps the drafts, and the injected clock is
a scripted counter, so the report duration is deterministic. No database and no network: the only IO is
the versioned YAML + stamp + pointer round-trip under `tmp_path`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from core.cqrs.contracts import MessageBus
from pydantic_evals import Case

from engram.evals.commands import (
    CheckFreshness,
    ComputeCoverage,
    CoverageComputed,
    CoverageComputedReport,
    DatasetFrozenReport,
    DrafterFactory,
    FreezeDataset,
    FreshnessCheckedReport,
    GenerateEvalDataset,
    GenerateEvalDatasetReport,
    GenerationCompleted,
    GenerationStarted,
    RollbackPolicy,
    RoundEvaluated,
    TuneRetrieval,
    TuningCompleted,
    TuningReport,
    TuningStarted,
    VerifyRecommendation,
    VerifyReport,
)
from engram.evals.generation.contracts import (
    MIN_SURVEY_FANOUT,
    MIN_SYNTHESIS_FANOUT,
    AuthoredCase,
    CaseCurator,
    DatasetStamp,
    EvaluationDataset,
    FrozenSplit,
    GenerationConfig,
    SeedCaseDraft,
    Stratum,
    StratumCount,
    StratumInventory,
    allocate,
    build_dataset,
    dataset_version_path,
    load_dataset,
    load_frozen_split,
    load_stamp,
    mirror_hash,
    read_current,
    save_dataset,
    save_dataset_version,
    save_frozen_split,
    write_current,
)
from engram.evals.generation.domain.datasets import RetirementRecord
from engram.evals.generation.infrastructure.files import load_coverage_report, load_freshness_report
from engram.evals.generation.service.generate import coverage_summary
from engram.evals.handlers import (
    CheckFreshnessHandler,
    ComputeCoverageHandler,
    FreezeDatasetHandler,
    GenerateEvalDatasetHandler,
    RollbackPolicyHandler,
    TuneRetrievalHandler,
    VerifyRecommendationHandler,
)
from engram.evals.recommendation.contracts import BaselineSnapshot, CaseSnapshot, KnowledgeBaseStamp, save_recommendation
from engram.evals.recommendation.domain.recommendation import (
    PageWorkOrder,
    Prediction,
    ProposalEvidence,
    Recommendation,
    RestructureSectionsOp,
    SchemaEditProposal,
)
from engram.evals.shared.domain import AnchorHash, Archetype, ExpectedQa, QaCaseMeta, QaDifficulty, QaInput, RetrievalAnswer, Seed
from engram.evals.tuning.domain.objective import TuningObjective
from engram.evals.tuning.domain.proposal import ProposedPolicy
from engram.evals.tuning.policy import RetrievalPolicy, baseline_policy, load_policy
from engram.kb.contracts import apply_retrieval_policy
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.recommendation import FakeKnowledgeBaseGateway

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping, Sequence

    from engram.evals.commands import FreshnessReader, PolicyEvaluatorFactory, ProposerFactory
    from engram.evals.generation.contracts import CaseFreshnessRef
    from engram.evals.rca.contracts import ReflectiveFinding
    from engram.evals.shared.read_models import EvaluationCaseResultRow, EvaluationRunSummary

# The source body a draft's support quote is checked against (whitespace-normalized substring).
_BODY = "Graph index construction incurs substantial token cost during entity and relation extraction."


def _pinpoint_seed(suffix: str) -> Seed:
    """A single-source pinpoint seed (one gold id, no hop, one section, easy by default)."""
    return Seed(
        archetype=Archetype.PINPOINT,
        gold_ids=(f"paper.p{suffix}",),
        source_bodies=(_BODY,),
        seed_key=f"pinpoint:paper.p{suffix}",
        anchors=(AnchorHash(unit_id=f"paper.p{suffix}", body_hash=f"hp{suffix}"),),
    )


class FakeSeedQueries:
    """A `SupportsSeeding` stand-in returning canned seeds per archetype, capped at `limit`.

    `seeds` maps an `Archetype` to the seeds its seeder method should return (a public, scriptable
    attribute, repo fakes convention). Each method honours only `limit`, the structural narrowing on
    `collection` / `page_ids` is the real repository's concern, exercised in its own suite.
    """

    def __init__(self, seeds: dict[Archetype, list[Seed]]) -> None:
        self.seeds = {archetype: list(values) for archetype, values in seeds.items()}

    async def pinpoint_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del salt, band, anchor_page_type
        return self.seeds.get(Archetype.PINPOINT, [])[:limit]

    async def survey_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del salt, band, anchor_page_type
        return self.seeds.get(Archetype.SURVEY, [])[:limit]

    async def trace_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del salt, band, anchor_page_type
        return self.seeds.get(Archetype.TRACE, [])[:limit]

    async def synthesis_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del salt, band, anchor_page_type
        return self.seeds.get(Archetype.SYNTHESIS, [])[:limit]

    async def passage_seeds(
        self,
        *,
        collection: str,
        page_ids: tuple[str, ...],
        limit: int,
        salt: str = "",
        band: QaDifficulty | None = None,
        anchor_page_type: str | None = None,
    ) -> list[Seed]:
        del salt, band, anchor_page_type
        return self.seeds.get(Archetype.PASSAGE, [])[:limit]


class FakeRepoFactory:
    """A `SeedRepositoryFactory` that yields one canned repo, recording how often its scope opened/closed.

    Stands in for the real per-command factory (which opens a fresh owner session each call). `entered`
    and `exited` are public counters (fake convention) so a test can assert the session scope was opened
    and cleanly closed around the pipeline run.
    """

    def __init__(self, repo: FakeSeedQueries) -> None:
        self.repo = repo
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FakeSeedQueries]:
        self.entered += 1
        try:
            yield self.repo
        finally:
            self.exited += 1


def _inventory(*cells: tuple[Archetype, QaDifficulty, str, int], collection: str = "research") -> StratumInventory:
    """Build a `StratumInventory` from `(archetype, difficulty, anchor_page_type, available)` tuples."""
    return StratumInventory(
        collection=collection,
        counts=tuple(StratumCount(stratum=Stratum(archetype=a, difficulty=d, anchor_page_type=k), available=n) for a, d, k, n in cells),
    )


def _current_stamp(datasets_dir: Path, name: str) -> DatasetStamp:
    """Load the stamp of the version the dataset's `.current` pointer names (asserts one exists)."""
    version_path = read_current(datasets_dir, name)
    assert version_path is not None
    stamp = load_stamp(version_path)
    assert stamp is not None
    return stamp


class FakeInventoryReader:
    """A `SupportsCoverageReader` stand-in: a canned inventory + fingerprint + page-id set.

    `inventory_value` is returned by every `inventory` call, `fingerprint` by every
    `knowledge_base_fingerprint`, and `page_ids_value` by every `page_ids` (all public, scriptable, fake
    convention), so a spec can drive the coverage summary, the stamp's knowledge base identity, and the top-up
    gold-existence check deterministically. `page_ids_value` defaults to a permissive set covering the
    `paper.p*` seeds these specs use, so a top-up sees every gold as present unless a spec narrows it.
    """

    def __init__(
        self, inventory: StratumInventory, *, fingerprint: str = "spec-hash:research", page_ids: frozenset[str] | None = None
    ) -> None:
        self.inventory_value = inventory
        self.fingerprint = fingerprint
        self.page_ids_value = page_ids if page_ids is not None else frozenset(f"paper.p{index}" for index in range(100))

    async def inventory(self, *, collection: str) -> StratumInventory:
        del collection
        return self.inventory_value

    async def knowledge_base_fingerprint(self, *, collection: str) -> str:
        del collection
        return self.fingerprint

    async def index_build(self, *, collection: str) -> str:
        del collection
        return "7+500-1000 test-embedding"

    async def page_ids(self, *, collection: str) -> frozenset[str]:
        del collection
        return self.page_ids_value


class _DriftingInventoryReader(FakeInventoryReader):
    """A `FakeInventoryReader` whose census drifts after the first read.

    Returns `inventory_value` on the first `inventory` call (the census the pipeline generates against)
    and `drifted` on every later call. The handler must stamp the first census: censusing a second time
    would let the stamp describe a census no case was ever drawn from.
    """

    def __init__(self, first: StratumInventory, drifted: StratumInventory, *, fingerprint: str = "spec-hash:research") -> None:
        super().__init__(first, fingerprint=fingerprint)
        self.drifted = drifted
        self.inventory_calls = 0

    async def inventory(self, *, collection: str) -> StratumInventory:
        del collection
        self.inventory_calls += 1
        return self.inventory_value if self.inventory_calls == 1 else self.drifted


class FakeReaderFactory:
    """An `InventoryReaderFactory` yielding one canned reader, recording its scope open / close.

    Stands in for the real per-command factory (which opens a fresh owner session each call). `entered`
    / `exited` are public counters so a spec can assert the reader scope opened and cleanly closed.
    """

    def __init__(self, reader: FakeInventoryReader) -> None:
        self.reader = reader
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FakeInventoryReader]:
        self.entered += 1
        try:
            yield self.reader
        finally:
            self.exited += 1


class FakeCaseDrafter:
    """A `CaseDrafter` that drafts `target` cases per seed, quoting verbatim from the seed itself.

    Each draft's support quote is a verbatim slice of the seed's first source body, so the `accept`
    gate keeps every draft, the pipeline's quote-checked-by-construction path.
    """

    def __init__(self, *, quote_len: int = 12) -> None:
        self.quote_len = quote_len

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        del instructions
        quote = seed.source_bodies[0][: self.quote_len]
        return [SeedCaseDraft(question=f"Q-{seed.seed_key}-{index}", answer="A", support_quotes=[quote]) for index in range(target)]


class StubClock:
    """A scripted monotonic clock: each call returns the next pre-loaded tick (deterministic duration)."""

    def __init__(self, ticks: list[float]) -> None:
        self.ticks = ticks
        self.index = 0

    def __call__(self) -> float:
        tick = self.ticks[self.index]
        self.index += 1
        return tick


def _make_drafter_factory(drafter: FakeCaseDrafter) -> DrafterFactory:
    """A `DrafterFactory` that ignores the model spec and returns the supplied `drafter`."""

    def _factory(model: str | None) -> FakeCaseDrafter:
        del model
        return drafter

    return _factory


def _config(*, total_cases: int = 10, salt: str | None = None) -> GenerationConfig:
    """A single-archetype `GenerationConfig` weighted entirely on pinpoint."""
    return GenerationConfig(target_distribution={Archetype.PINPOINT: 1.0}, total_cases=total_cases, target_per_seed=1, salt=salt)


class _AllFreshFreshnessReader:
    """A `FreshnessReader` that keeps every `_pinpoint_seed`-derived case fresh for retirement.

    Reports every requested page present (so no case is `gold_gone`) and recomputes each mirror-era
    case's drafting text to the shared `_BODY` constituent `accept` stamps these seeds with (so no case
    is `mirror_stale`), leaving the top-up staleness pre-pass an empty stale set. The generate handler's
    default when a spec does not script staleness, so the pre-freshness top-up specs run unchanged.
    """

    async def page_state(self, *, collection: str, page_ids: frozenset[str]) -> Mapping[str, str]:
        del collection
        return dict.fromkeys(page_ids, "")

    async def mirror_sources(self, *, refs: Sequence[CaseFreshnessRef]) -> Mapping[str, tuple[str, ...]]:
        return {ref.case_name: (_BODY,) for ref in refs}

    async def edge_pairs_present(self, *, pairs: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        return pairs  # every requested hop is live

    async def inbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        return dict.fromkeys(keys, MIN_SURVEY_FANOUT)  # every hub satisfies the seeder threshold

    async def outbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        return dict.fromkeys(keys, MIN_SYNTHESIS_FANOUT)

def _handler(
    repo: FakeSeedQueries,
    *,
    config: GenerationConfig,
    drafter: FakeCaseDrafter | None = None,
    clock: StubClock | None = None,
    reader: FakeInventoryReader | None = None,
    freshness_reader: FreshnessReader | None = None,
) -> tuple[GenerateEvalDatasetHandler, FakeRepoFactory, FakeReaderFactory]:
    """Build the handler with a real curator over `FakeEmbedding` and per-command repo + reader factories.

    Returns the handler paired with the `FakeRepoFactory` and `FakeReaderFactory`, so a test can assert
    both session scopes opened and closed around the run (the per-command-factory contract). The inventory
    reader defaults to a pinpoint-only census large enough that the whole budget lands on the pinpoint
    cell, and the freshness reader defaults to the all-fresh stand-in so a top-up finds no staleness unless
    a spec scripts it.
    """
    curator = CaseCurator(FakeEmbedding(), dedup_threshold=0.99)  # high cutoff: orthogonal hash vectors never dedup
    make_drafter = _make_drafter_factory(drafter or FakeCaseDrafter())
    make_repo = FakeRepoFactory(repo)
    make_reader = FakeReaderFactory(reader or FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 100))))
    make_freshness = FakeFreshnessReaderFactory(freshness_reader or _AllFreshFreshnessReader())
    if clock is None:
        return GenerateEvalDatasetHandler(make_repo, curator, config, make_drafter, make_reader, make_freshness), make_repo, make_reader
    return (
        GenerateEvalDatasetHandler(make_repo, curator, config, make_drafter, make_reader, make_freshness, clock=clock),
        make_repo,
        make_reader,
    )


class TestGenerateEvalDatasetHandler:
    async def test_writes_a_versioned_dataset_that_round_trips(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, make_repo, make_reader = _handler(repo, config=_config(total_cases=3))
        out = tmp_path / "research.yaml"

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        assert isinstance(report, GenerateEvalDatasetReport)
        # The base `out` never exists: the handler writes the next immutable version instead.
        assert not out.exists()
        version_path = tmp_path / "research.v1.yaml"
        assert version_path.exists()
        reloaded = load_dataset(version_path)
        assert len(reloaded.cases) == 3
        assert report.n_cases == len(reloaded.cases)
        assert report.out == str(version_path)
        assert report.version == "research.v1.yaml"
        assert report.requested_archetypes == (Archetype.PINPOINT,)
        assert (make_repo.entered, make_repo.exited) == (1, 1)  # one owner session per command, cleanly closed
        assert (make_reader.entered, make_reader.exited) == (1, 1)  # one reader session per command, cleanly closed

    async def test_writes_the_stamp_sidecar_and_current_pointer(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 100)), fingerprint="spec-abc:research")
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"), reader=reader)
        out = tmp_path / "research.yaml"

        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        version_path = tmp_path / "research.v1.yaml"
        assert (tmp_path / "research.current").read_text(encoding="utf-8") == "research.v1.yaml"
        stamp = load_stamp(version_path)
        assert stamp is not None
        assert stamp.salt == "fixed-salt"
        assert stamp.knowledge_base_fingerprint == "spec-abc:research"
        assert stamp.budget == 3
        assert stamp.parent_version is None
        assert stamp.retired == ()
        assert stamp.coverage == {"pinpoint/easy/paper": 3}  # the allocation targeted three pinpoint-easy cases

    async def test_stamp_coverage_reflects_the_census_the_pipeline_drew_from(self, tmp_path: Path):
        # The pipeline censuses once to generate; a knowledge base that drifts between reads must not let the stamp
        # describe a census no case was drawn from. This reader returns a pinpoint/easy/paper census
        # first (what the pipeline draws against) and a pinpoint/easy/moc census on any later read.
        first = _inventory((Archetype.PINPOINT, "easy", "paper", 100))
        drifted = _inventory((Archetype.PINPOINT, "easy", "moc", 100))
        reader = _DriftingInventoryReader(first, drifted)
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        config = _config(total_cases=3)
        handler, _, _ = _handler(repo, config=config, reader=reader)
        out = tmp_path / "research.yaml"

        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        stamp = _current_stamp(tmp_path, "research")
        assert stamp.coverage == coverage_summary(allocate(config.target_distribution, config.total_cases, first, config.thresholds))
        assert stamp.coverage != coverage_summary(allocate(config.target_distribution, config.total_cases, drifted, config.thresholds))

    async def test_second_run_bumps_the_version_and_moves_the_pointer(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3))
        out = tmp_path / "research.yaml"

        first = await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))
        second = await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        assert first.version == "research.v1.yaml"
        assert second.version == "research.v2.yaml"
        assert (tmp_path / "research.v1.yaml").exists()  # the prior version is never mutated
        assert (tmp_path / "research.current").read_text(encoding="utf-8") == "research.v2.yaml"

    async def test_salt_resolution_prefers_command_then_config_then_generated(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        out = tmp_path / "research.yaml"

        # 1) The command's salt wins over the configured default.
        handler, _, _ = _handler(repo, config=_config(salt="config-salt"))
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,), salt="command-salt"))
        assert _current_stamp(tmp_path, "research").salt == "command-salt"

        # 2) With no command salt, the configured default is used.
        handler, _, _ = _handler(repo, config=_config(salt="config-salt"))
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))
        assert _current_stamp(tmp_path, "research").salt == "config-salt"

        # 3) With neither, a salt is generated at the command boundary and recorded.
        handler, _, _ = _handler(repo, config=_config(salt=None))
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))
        generated = _current_stamp(tmp_path, "research").salt
        assert len(generated) == 16  # a stdlib secrets.token_hex(8) value: 8 bytes -> 16 hex chars
        assert int(generated, 16) >= 0  # ... that parses as hexadecimal

    async def test_emits_started_and_completed_events(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        handler, _, _ = _handler(repo, config=_config())
        out = tmp_path / "research.yaml"

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        assert [type(event) for event in handler.events] == [GenerationStarted, GenerationCompleted]
        started, completed = handler.events
        assert isinstance(started, GenerationStarted)
        assert started.collection == "research"
        assert started.out == str(out)
        assert isinstance(completed, GenerationCompleted)
        assert completed.report == report

    async def test_empty_archetypes_covers_all_five(self, tmp_path: Path):
        # An empty `archetypes` means all five; only pinpoint has inventory/seeds, so the dataset is
        # pinpoint-only, but the report records the full requested set (every archetype was asked for).
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        handler, _, _ = _handler(repo, config=_config())
        out = tmp_path / "research.yaml"

        report = await handler(GenerateEvalDataset(collection="research", out=str(out)))

        assert report.requested_archetypes == tuple(Archetype)

    async def test_count_overrides_total_cases_and_stamp_budget(self, tmp_path: Path):
        # The config budgets 10 cases; `count=2` overrides `total_cases`, so both the dataset and the
        # stamp's budget cap at 2.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(20)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=10))
        out = tmp_path / "research.yaml"

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,), count=2))

        assert report.n_cases == 2
        assert _current_stamp(tmp_path, "research").budget == 2

    async def test_duration_measured_by_injected_clock(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        handler, _, _ = _handler(repo, config=_config(), clock=StubClock([1.0, 3.5]))
        out = tmp_path / "research.yaml"

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        assert report.duration_seconds == pytest.approx(2.5)

    async def test_dispatch_through_real_message_bus(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("1")]})
        handler, _, _ = _handler(repo, config=_config())
        bus = MessageBus({}, {})
        bus.register(GenerateEvalDataset, handler)
        out = tmp_path / "research.yaml"

        result = await bus.handle(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        assert isinstance(result, GenerateEvalDatasetReport)
        assert result.out == str(tmp_path / "research.v1.yaml")
        assert handler.events == []  # the bus drained the handler's events after dispatch

    async def test_wrong_command_type_raises_type_error(self):
        repo = FakeSeedQueries({})
        handler, _, _ = _handler(repo, config=_config())

        class OtherCommand(GenerateEvalDataset.__bases__[0]):
            pass

        with pytest.raises(TypeError):
            await handler(OtherCommand())


# -- GenerateEvalDatasetHandler top-up mode -----------------------------------
#
# `--top-up` maintains a dataset chain: it drafts only the under-covered strata and retires the
# deterministically displaced parent cases into the new version's ledger, never mutating the parent.
# Pinned here: the version bump, the ledger, the pointer move, parent immutability, the stamp-required
# error, and that a dry run previews the plan while writing nothing. The `FakeSeedQueries` ignores the
# salted draw, so top-up churn is simulated by a budget the fresh v1 under-fills or over-fills; the exact
# hash-displaced set is the pure planner's concern (`test_topup.py`).


class TestGenerateEvalDatasetTopUp:
    async def test_shrunk_budget_retires_and_writes_the_next_version(self, tmp_path: Path):
        # v1 has three pinpoint cases; a top-up at budget one keeps one and retires two into the ledger.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"))
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))
        v1_bytes = (tmp_path / "research.v1.yaml").read_bytes()

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=1))

        assert report.version == "research.v2.yaml"
        assert report.retired == 2
        assert report.n_cases == 1
        assert (tmp_path / "research.v1.yaml").read_bytes() == v1_bytes  # the parent version is never mutated
        assert (tmp_path / "research.current").read_text(encoding="utf-8") == "research.v2.yaml"  # the pointer moved
        stamp = load_stamp(tmp_path / "research.v2.yaml")
        assert stamp is not None
        assert stamp.parent_version == "research.v1.yaml"
        assert len(stamp.retired) == 2
        assert {record.reason for record in stamp.retired} == {"displaced"}

    async def test_grown_budget_fills_the_gap_without_retiring(self, tmp_path: Path):
        # v1 under-fills at budget one; the knowledge base offers three seeds, so a top-up at budget three drafts
        # the two-case gap and retires nothing.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"))
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,), count=1))

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=3))

        assert report.version == "research.v2.yaml"
        assert report.retired == 0
        assert report.n_cases == 3  # the one survivor plus the two-case gap filled
        assert len(load_dataset(tmp_path / "research.v2.yaml").cases) == 3

    async def test_dry_run_previews_the_plan_and_writes_nothing(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"))
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=1, dry_run=True))

        assert report.plan is not None
        assert report.retired == len(report.plan.retire) == 2  # the previewed ledger
        assert report.version == ""
        assert not (tmp_path / "research.v2.yaml").exists()  # a dry run writes nothing
        assert (tmp_path / "research.current").read_text(encoding="utf-8") == "research.v1.yaml"  # the pointer stayed

    async def test_dry_run_plan_matches_the_applied_effect(self, tmp_path: Path):
        # The plan a dry run previews is exactly what the apply writes (same inputs, deterministic planner).
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"))
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        previewed = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=1, dry_run=True))
        await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=1))

        assert previewed.plan is not None
        planned_retired = {record.case_name for record in previewed.plan.retire}
        written_retired = {record.case_name for record in _current_stamp(tmp_path, "research").retired}
        assert planned_retired == written_retired  # the dry-run plan equals the applied ledger

    async def test_vanished_gold_page_retires_as_gold_gone(self, tmp_path: Path):
        # v1 has three pinpoint cases (gold pages p0/p1/p2); the knowledge base then loses p2, so the top-up
        # reads a page set missing it and retires that case as `gold_gone`, not `displaced`.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        reader = FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 100)), page_ids=frozenset({"paper.p0", "paper.p1"}))
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"), reader=reader)
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=3))

        assert report.retired == 1
        assert (
            report.n_cases == 3
        )  # two surviving golds plus a redraft: the vanished-gold case's freed slot reopens as a band deficit
        stamp = load_stamp(tmp_path / "research.v2.yaml")
        assert stamp is not None
        gone = [record for record in stamp.retired if record.reason == "gold_gone"]
        assert [record.seed_key for record in gone] == ["pinpoint:paper.p2"]  # only the case whose gold page vanished

    async def test_top_up_requires_a_stamped_parent(self, tmp_path: Path):
        # A dataset saved without a stamp cannot be topped up: a clean error, not a silent guess.
        unstamped = build_dataset(
            [
                AuthoredCase(
                    question=QaInput(question="q"),
                    expected=ExpectedQa(answer="a", expected_source_ids=("paper.x",)),
                    meta=QaCaseMeta(anchor_page_id="paper.x", difficulty="easy", archetype=Archetype.PINPOINT),
                )
            ]
        )
        version_path = dataset_version_path(tmp_path, "research", 1)
        save_dataset(unstamped, version_path)  # no stamp sidecar
        write_current(tmp_path, "research", version_path)
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("0")]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3))

        with pytest.raises(ValueError, match="no dataset stamp"):
            await handler(GenerateEvalDataset(collection="research", out=str(tmp_path / "research.yaml"), top_up_from="current"))

    async def test_missing_current_pointer_fails_loudly(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("0")]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3))

        with pytest.raises(ValueError, match="no current dataset pointer"):
            await handler(GenerateEvalDataset(collection="research", out=str(tmp_path / "research.yaml"), top_up_from="current"))

    async def test_explicit_parent_path_tops_up_without_the_pointer(self, tmp_path: Path):
        # `--top-up <path>` targets an explicit version file rather than the `current` pointer.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"))
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        report = await handler(
            GenerateEvalDataset(collection="research", out=str(out), top_up_from=str(tmp_path / "research.v1.yaml"), count=1)
        )

        assert report.version == "research.v2.yaml"
        assert report.retired == 2

    async def test_emits_started_and_completed_events(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"))
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))
        handler.events.clear()

        await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=1, dry_run=True))

        assert [type(event) for event in handler.events] == [GenerationStarted, GenerationCompleted]

    # -- staleness-aware top-up ---------------------------------------------
    #
    # The top-up sweeps the parent cases against the live knowledge base (through the freshness reader) before
    # planning: stale cases retire as `reason="stale"` and their bands redraft under chain-walked
    # historically-unique names. `_AllFreshFreshnessReader` is the default, so a spec scripts staleness by
    # handing `_handler` an explicit `FakeFreshnessReader`.

    async def test_mirror_stale_case_retires_as_stale_and_redrafts_at_the_next_ordinal(self, tmp_path: Path):
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        # p2's drafting text can no longer be recomputed (its case name is absent from `sources`), so the
        # sweep flags it `mirror_stale`; p0/p1 recompute to the stamped `_BODY` mirror and stay fresh.
        reader = FakeFreshnessReader(
            hashes={f"paper.p{i}": f"hp{i}" for i in range(3)}, sources={"pinpoint:paper.p0#0": (_BODY,), "pinpoint:paper.p1#0": (_BODY,)}
        )
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"), freshness_reader=reader)
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=3))

        stamp = load_stamp(tmp_path / "research.v2.yaml")
        assert stamp is not None
        assert [record.seed_key for record in stamp.retired if record.reason == "stale"] == ["pinpoint:paper.p2"]
        assert report.retired == 1
        names = {case.name for case in load_dataset(tmp_path / "research.v2.yaml").cases}
        assert "pinpoint:paper.p2#0" not in names  # the stale case retired, not carried over
        assert sum(1 for name in names if name is not None and name.endswith("#1")) == 1  # one fresh-ordinal redraft
        assert len(names) == 3  # the band healed by the deficit redraft

    async def test_gold_gone_verdict_is_excluded_from_the_stale_set(self, tmp_path: Path):
        # The sweep sees p2's gold page vanished (absent from `hashes` -> gold_gone verdict), but the
        # inventory reader still lists it, so `plan_top_up`'s own gold-page path does not fire. Because the
        # pre-pass drops gold_gone verdicts from the stale set, p2 is never smuggled in as `reason="stale"`.
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        reader = FakeFreshnessReader(
            hashes={"paper.p0": "hp0", "paper.p1": "hp1"},  # paper.p2 absent -> its case is gold_gone in the sweep
            sources={f"pinpoint:paper.p{i}#0": (_BODY,) for i in range(3)},
        )
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"), freshness_reader=reader)
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=3))

        stamp = load_stamp(tmp_path / "research.v2.yaml")
        assert stamp is not None
        assert [record for record in stamp.retired if record.reason == "stale"] == []  # gold_gone not smuggled in as stale
        assert report.retired == 0  # the inventory still lists the page, so nothing retires

    async def test_redraft_never_reuses_a_name_retired_earlier_in_the_chain(self, tmp_path: Path):
        # v1 already retired `pinpoint:paper.p0#0` in its ledger and kept `#1` live. A top-up that redrafts
        # the pinpoint band must continue past the chain's highest ordinal (used = live names + every
        # ledger's retired names), so the redraft is `#2`, never the retired `#0`.
        authored = AuthoredCase(
            question=QaInput(question="q-0-1"),
            expected=ExpectedQa(answer="a", expected_source_ids=("paper.p0",)),
            meta=QaCaseMeta(
                anchor_page_id="paper.p0",
                difficulty="easy",
                archetype=Archetype.PINPOINT,
                anchors=(AnchorHash(unit_id="paper.p0", body_hash="hp0"),),
                mirror_hash=mirror_hash((_BODY,)),
            ),
        )
        v1 = build_dataset([authored])
        v1.cases[0].name = "pinpoint:paper.p0#1"
        stamp = DatasetStamp(
            salt="fixed-salt",
            knowledge_base_fingerprint="fp:research",
            budget=2,
            parent_version=None,
            coverage={},
            retired=(RetirementRecord(case_name="pinpoint:paper.p0#0", seed_key="pinpoint:paper.p0", reason="displaced"),),
        )
        version_path = dataset_version_path(tmp_path, "research", 1)
        save_dataset_version(v1, stamp, version_path)
        write_current(tmp_path, "research", version_path)
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed("0")]})
        handler, _, _ = _handler(repo, config=_config(total_cases=2, salt="fixed-salt"))
        out = tmp_path / "research.yaml"

        await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=2))

        names = {case.name for case in load_dataset(tmp_path / "research.v2.yaml").cases}
        assert "pinpoint:paper.p0#0" not in names  # the chain-retired ordinal is never reused
        assert "pinpoint:paper.p0#2" in names  # the redraft continues past the chain's highest ordinal

    async def test_all_fresh_parent_tops_up_identically_to_the_pre_freshness_behavior(self, tmp_path: Path):
        # An explicitly all-fresh sweep (every page present with a matching hash, every mirror recomputes)
        # yields an empty stale set, so a shrunk-budget top-up retires purely by displacement exactly as it
        # did before the pre-pass existed (the regression guard).
        repo = FakeSeedQueries({Archetype.PINPOINT: [_pinpoint_seed(str(i)) for i in range(3)]})
        reader = FakeFreshnessReader(
            hashes={f"paper.p{i}": f"hp{i}" for i in range(3)}, sources={f"pinpoint:paper.p{i}#0": (_BODY,) for i in range(3)}
        )
        handler, _, _ = _handler(repo, config=_config(total_cases=3, salt="fixed-salt"), freshness_reader=reader)
        out = tmp_path / "research.yaml"
        await handler(GenerateEvalDataset(collection="research", out=str(out), archetypes=(Archetype.PINPOINT,)))

        report = await handler(GenerateEvalDataset(collection="research", out=str(out), top_up_from="current", count=1))

        assert report.retired == 2
        assert report.n_cases == 1
        stamp = load_stamp(tmp_path / "research.v2.yaml")
        assert stamp is not None
        assert {record.reason for record in stamp.retired} == {"displaced"}  # nothing stale, no vanished gold


# -- ComputeCoverageHandler ---------------------------------------------------
#
# The handler is the read-only CQRS seam for `evals coverage`: it censuses the knowledge base per stratum
# (through the same faked coverage reader the generate handler uses), computes the target allocation,
# and either reports the inventory-only census or audits a dataset against it, writing a CoverageReport
# YAML + a markdown summary. It never mutates the DB or any dataset file. Every collaborator is faked
# network-free; the only IO is the two artifact writes and reading the audited dataset under tmp_path.


def _coverage_config() -> GenerationConfig:
    """A two-archetype config: half pinpoint (easy), half survey (hard), a 10-case budget."""
    return GenerationConfig(target_distribution={Archetype.PINPOINT: 0.5, Archetype.SURVEY: 0.5}, total_cases=10, target_per_seed=1)


def _coverage_reader() -> FakeInventoryReader:
    """A reader whose knowledge base offers ample pinpoint-easy and survey-hard seeds (both cells feasible)."""
    return FakeInventoryReader(_inventory((Archetype.PINPOINT, "easy", "paper", 100), (Archetype.SURVEY, "hard", "hub", 100)))


def _labelled_cases(*, pinpoint: int, survey: int) -> list[AuthoredCase]:
    """Authored cases labelled by archetype/difficulty, the audit's per-band recount substrate."""
    cases = [
        AuthoredCase(
            question=QaInput(question=f"pp-{i}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(f"paper.p{i}",)),
            meta=QaCaseMeta(anchor_page_id=f"paper.p{i}", difficulty="easy", archetype=Archetype.PINPOINT),
        )
        for i in range(pinpoint)
    ]
    cases += [
        AuthoredCase(
            question=QaInput(question=f"sv-{i}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(f"paper.h{i}",)),
            meta=QaCaseMeta(anchor_page_id=f"paper.h{i}", difficulty="hard", archetype=Archetype.SURVEY),
        )
        for i in range(survey)
    ]
    return cases


def _save_version(datasets_dir: Path, name: str, version: int, cases: list[AuthoredCase], *, parent: str | None = None) -> Path:
    """Save one immutable dataset version + its stamp (a `parent_version` link) and return its path."""
    path = dataset_version_path(datasets_dir, name, version)
    stamp = DatasetStamp(salt="s", knowledge_base_fingerprint="fp:research", budget=10, parent_version=parent, coverage={})
    save_dataset_version(build_dataset(cases), stamp, path)
    return path


def _coverage_handler(
    datasets_dir: Path, *, reader: FakeInventoryReader | None = None, config: GenerationConfig | None = None
) -> tuple[ComputeCoverageHandler, FakeReaderFactory]:
    """Build the coverage handler over a faked per-command reader factory and the tmp datasets dir."""
    make_reader = FakeReaderFactory(reader or _coverage_reader())
    handler = ComputeCoverageHandler(make_reader, config or _coverage_config(), datasets_dir)
    return handler, make_reader


class TestPathHardeningGuards:
    """CLI/disk-sourced path components fail loudly at the handler layer."""

    def test_next_version_rejects_a_non_slug_chain_name(self, tmp_path: Path):
        from engram.evals.generation.service.generate import next_version

        with pytest.raises(ValueError, match="plain name"):
            next_version(tmp_path, "../evil")

    async def test_coverage_rejects_a_collection_that_is_not_a_plain_slug(self, tmp_path: Path):
        # A collection carrying a separator would write the coverage artifacts outside --out.
        handler, _ = _coverage_handler(tmp_path)

        with pytest.raises(ValueError, match="plain name"):
            await handler(ComputeCoverage(collection="../evil", out=str(tmp_path / "coverage")))


class TestComputeCoverageHandler:
    async def test_inventory_only_reports_gaps_against_target_with_no_dataset(self, tmp_path: Path):
        handler, make_reader = _coverage_handler(tmp_path)
        out = tmp_path / "coverage"

        report = await handler(ComputeCoverage(collection="research", out=str(out)))

        assert isinstance(report, CoverageComputedReport)
        # Covered is zero everywhere, so both feasible targeted cells are gaps; debt is positive.
        assert set(report.gaps) == {"pinpoint/easy/paper", "survey/hard/hub"}
        assert report.over_represented == ()
        assert report.debt > 0.0
        assert report.dataset_version is None
        assert report.version_chain == ()
        assert (make_reader.entered, make_reader.exited) == (1, 1)  # one reader session, cleanly closed

    async def test_inventory_only_writes_the_yaml_and_markdown_artifacts(self, tmp_path: Path):
        handler, _ = _coverage_handler(tmp_path)
        out = tmp_path / "coverage"

        report = await handler(ComputeCoverage(collection="research", out=str(out)))

        yaml_path = out / "research.coverage.yaml"
        markdown_path = out / "research.coverage.md"
        assert report.out == str(yaml_path)
        assert report.markdown == str(markdown_path)
        assert yaml_path.exists()
        assert markdown_path.exists()
        # The persisted report round-trips and its inventory carries the full per-anchor-kind census.
        reloaded = load_coverage_report(yaml_path)
        assert reloaded.collection == "research"
        assert {count.stratum.anchor_page_type for count in reloaded.inventory.counts} == {"paper", "hub"}
        assert all(count.covered == 0 for count in reloaded.inventory.counts)

    async def test_audit_recounts_covered_per_band_and_shrinks_the_gaps(self, tmp_path: Path):
        # Five pinpoint-easy (meets the 5-case target) and two survey-hard (below the 5-case target).
        dataset_path = _save_version(tmp_path, "research", 1, _labelled_cases(pinpoint=5, survey=2))
        handler, _ = _coverage_handler(tmp_path)
        out = tmp_path / "coverage"

        report = await handler(ComputeCoverage(collection="research", dataset=str(dataset_path), out=str(out)))

        # Audit collapses over the anchor kind (the dataset does not record it): the met pinpoint band
        # is neither gap nor over, only the under-covered survey band remains a gap.
        assert report.gaps == ("survey/hard/*",)
        assert report.over_represented == ()
        assert report.dataset_version == "research.v1.yaml"
        # The recounted covered counts land on the persisted report's band-collapsed inventory.
        reloaded = load_coverage_report(out / "research.coverage.yaml")
        covered = {count.stratum: count.covered for count in reloaded.inventory.counts}
        assert covered[Stratum(archetype=Archetype.SURVEY, difficulty="hard", anchor_page_type="*")] == 2
        assert covered[Stratum(archetype=Archetype.PINPOINT, difficulty="easy", anchor_page_type="*")] == 5

    async def test_audit_debt_is_below_the_inventory_only_debt(self, tmp_path: Path):
        # Covering cases can only reduce the shortfall, so an audited dataset's debt is <= the empty one.
        handler_inv, _ = _coverage_handler(tmp_path)
        inventory_only = await handler_inv(ComputeCoverage(collection="research", out=str(tmp_path / "a")))

        dataset_path = _save_version(tmp_path, "research", 1, _labelled_cases(pinpoint=5, survey=5))
        handler_audit, _ = _coverage_handler(tmp_path)
        audited = await handler_audit(ComputeCoverage(collection="research", dataset=str(dataset_path), out=str(tmp_path / "b")))

        assert audited.debt < inventory_only.debt
        assert audited.debt == 0.0  # both bands meet their target

    async def test_audit_never_mutates_the_dataset_file(self, tmp_path: Path):
        dataset_path = _save_version(tmp_path, "research", 1, _labelled_cases(pinpoint=3, survey=1))
        before = dataset_path.read_bytes()
        handler, _ = _coverage_handler(tmp_path)

        await handler(ComputeCoverage(collection="research", dataset=str(dataset_path), out=str(tmp_path / "coverage")))

        assert dataset_path.read_bytes() == before  # read-only: the audited dataset is untouched

    async def test_current_resolves_the_pointer_and_walks_the_version_chain(self, tmp_path: Path):
        _save_version(tmp_path, "research", 1, _labelled_cases(pinpoint=3, survey=1))
        v2 = _save_version(tmp_path, "research", 2, _labelled_cases(pinpoint=4, survey=2), parent="research.v1.yaml")
        write_current(tmp_path, "research", v2)
        handler, _ = _coverage_handler(tmp_path)

        report = await handler(ComputeCoverage(collection="research", dataset="current", out=str(tmp_path / "coverage")))

        assert report.dataset_version == "research.v2.yaml"  # the pointer resolved to v2
        assert report.version_chain == ("research.v1.yaml", "research.v2.yaml")  # walked root-first via the stamp link

    async def test_missing_current_pointer_fails_loudly(self, tmp_path: Path):
        handler, _ = _coverage_handler(tmp_path)

        with pytest.raises(ValueError, match="no current dataset pointer"):
            await handler(ComputeCoverage(collection="research", dataset="current", out=str(tmp_path / "coverage")))

    async def test_emits_only_the_coverage_computed_event(self, tmp_path: Path):
        handler, _ = _coverage_handler(tmp_path)

        report = await handler(ComputeCoverage(collection="research", out=str(tmp_path / "coverage")))

        assert [type(event) for event in handler.events] == [CoverageComputed]
        completed = handler.events[0]
        assert isinstance(completed, CoverageComputed)
        assert completed.report == report

    async def test_dispatch_through_real_message_bus(self, tmp_path: Path):
        handler, _ = _coverage_handler(tmp_path)
        bus = MessageBus({}, {})
        bus.register(ComputeCoverage, handler)
        out = tmp_path / "coverage"

        result = await bus.handle(ComputeCoverage(collection="research", out=str(out)))

        assert isinstance(result, CoverageComputedReport)
        assert result.out == str(out / "research.coverage.yaml")
        assert handler.events == []  # the bus drained the handler's events after dispatch

    async def test_wrong_command_type_raises_type_error(self, tmp_path: Path):
        handler, _ = _coverage_handler(tmp_path)

        class OtherCommand(ComputeCoverage.__bases__[0]):
            pass

        with pytest.raises(TypeError):
            await handler(OtherCommand())


# -- TuneRetrievalHandler ------------------------------------------------
#
# The handler is the CQRS seam wrapping `TuningLoop`: it loads the golden dataset, runs the loop
# over a baseline, persists one summary run per round (the loop stays pure), saves the winning policy
# artifact, and emits the run's facts. Every collaborator is faked network-free, reusing
# `test_tuning.py`'s scaffolding: a `FakePolicyEvaluator` scores a dataset from a per-policy
# recall script, a `FakeProposer` pops one scripted `ProposedPolicy` per round, and a
# `FakeRunRepository` records each `add_run`. The only IO is the dataset + policy YAML round-trip
# under `tmp_path`.


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult` - the loop's `diagnose` reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase` - carries the slice `diagnose` folds over plus the case name."""

    name: str
    inputs: QaInput
    metadata: QaCaseMeta | None
    expected_output: RetrievalAnswer | None
    output: RetrievalAnswer
    scores: dict[str, _Score]
    metrics: dict[str, float | int]
    total_duration: float


@dataclass(frozen=True)
class _Report:
    """A stand-in for an `EvaluationReport` - `diagnose` reads only `.cases`."""

    cases: list[_Case]


class FakePolicyEvaluator:
    """A `PolicyEvaluator` scoring a dataset from a per-policy, per-case recall script (no network).

    Mirrors `test_tuning.py`: `recall` maps the policy's `instructions` (its hashable key) to a
    per-`case.name` `recall@5`, and `evaluate` builds a structural report from whichever dataset it is
    handed, so the train and validation reports differ by their cases exactly as a real run would.
    `evaluate_calls` records each `(instructions, case-names)` pair so a spec can assert which split a
    policy was measured on (the frozen-exclusion specs read it).
    """

    def __init__(self, recall: dict[str, dict[str, float]], *, tokens: float = 100.0) -> None:
        self.recall = recall
        self.tokens = tokens
        self.evaluate_calls: list[tuple[str, tuple[str, ...]]] = []

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        self.evaluate_calls.append((policy.instructions, tuple(case.name or "" for case in dataset.cases)))
        per_case = self.recall[policy.instructions]
        return _Report(cases=[self._case(case.name or "", per_case[case.name or ""]) for case in dataset.cases])

    def _case(self, name: str, recall: float) -> _Case:
        return _Case(
            name=name,
            inputs=QaInput(question=f"q-{name}"),
            metadata=QaCaseMeta(anchor_page_id=name, difficulty="easy", archetype=Archetype.PINPOINT),
            expected_output=RetrievalAnswer(answer="ref", cited_source_ids=[name]),
            output=RetrievalAnswer(answer="got", cited_source_ids=[name]),
            scores={"recall@5": _Score(recall), "n_tool_calls": _Score(1.0)},
            metrics={"input_tokens": self.tokens},
            total_duration=0.1,
        )


@dataclass
class FakeProposer:
    """A `PolicyProposer` that pops one scripted `ProposedPolicy` per round (no model, no network)."""

    proposals: list[ProposedPolicy]

    async def propose(self, policy: RetrievalPolicy, diagnosis: object, *, findings: tuple[ReflectiveFinding, ...] = ()) -> ProposedPolicy:
        del policy, diagnosis, findings
        return self.proposals.pop(0)


class FakeRunRepository:
    """A `SupportsRunPersistence` recording each `add_run` (the summary + its case rows).

    `add_run_calls` is a public recorder list (fake convention) so a spec can assert one summary run
    was persisted per tuning round, each tagged with its round index and accept verdict.
    """

    def __init__(self) -> None:
        self.add_run_calls: list[tuple[EvaluationRunSummary, tuple[EvaluationCaseResultRow, ...]]] = []

    async def add_run(self, run: EvaluationRunSummary, case_results: Sequence[EvaluationCaseResultRow]) -> None:
        self.add_run_calls.append((run, tuple(case_results)))


class FakeRunRepositoryFactory:
    """A `RunRepositoryFactory` yielding one canned repo, recording its scope open / close.

    Stands in for the real per-command factory (which opens a fresh owner session each call).
    `entered` / `exited` are public counters so a spec can assert the run-repo scope opened and cleanly
    closed around the per-round persistence.
    """

    def __init__(self, repo: FakeRunRepository) -> None:
        self.repo = repo
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FakeRunRepository]:
        self.entered += 1
        try:
            yield self.repo
        finally:
            self.exited += 1


def _proposal(instructions: str, *, strategy: str = "full") -> ProposedPolicy:
    """A scripted proposer output wrapping a candidate policy keyed by `instructions`."""
    return ProposedPolicy(policy=RetrievalPolicy(instructions=instructions, strategy=strategy), rationale=f"try {instructions}")


def _tuning_dataset(tmp_path: Path) -> Path:
    """Write a tiny two-case golden dataset to disk (the handler calls `load_dataset(command.dataset)`)."""
    cases = [
        AuthoredCase(
            question=QaInput(question="q-paper.a"),
            expected=ExpectedQa(answer="a", expected_source_ids=("paper.a",)),
            meta=QaCaseMeta(anchor_page_id="paper.a", difficulty="easy", archetype=Archetype.PINPOINT),
        ),
        AuthoredCase(
            question=QaInput(question="q-paper.b"),
            expected=ExpectedQa(answer="b", expected_source_ids=("paper.b",)),
            meta=QaCaseMeta(anchor_page_id="paper.b", difficulty="easy", archetype=Archetype.PINPOINT),
        ),
    ]
    path = tmp_path / "golden.yaml"
    save_dataset(build_dataset(cases), path)
    return path


def _proposer_factory(proposals: list[ProposedPolicy]) -> ProposerFactory:
    """A `ProposerFactory` that ignores the model spec and returns a fresh-queue `FakeProposer`."""

    def _factory(model: str | None) -> FakeProposer:
        del model
        return FakeProposer(proposals=list(proposals))

    return _factory


def _evaluator_factory(recall: dict[str, dict[str, float]]) -> PolicyEvaluatorFactory:
    """A `PolicyEvaluatorFactory` that ignores the model spec and returns a `FakePolicyEvaluator`."""

    def _factory(model: str | None) -> FakePolicyEvaluator:
        del model
        return FakePolicyEvaluator(recall)

    return _factory


def _tune_handler(
    *,
    recall: dict[str, dict[str, float]],
    proposals: list[ProposedPolicy],
    objective: TuningObjective,
    clock: StubClock | None = None,
    freshness_reader: FreshnessReader | None = None,
    evaluator_factory: PolicyEvaluatorFactory | None = None,
) -> tuple[TuneRetrievalHandler, FakeRunRepositoryFactory]:
    """Build the tuning handler with faked proposer / evaluator / run-repo factories.

    Returns the handler paired with the `FakeRunRepositoryFactory` so a spec can assert the run-repo
    scope opened and closed around the per-round persistence. The clock / id / now sources are pinned
    so the report duration and the persisted run ids are deterministic. `freshness_reader` wires the
    warn-never-gate freshness check; None leaves it unwired, so the report's `dataset_staleness` stays
    None. `evaluator_factory` overrides the default recall-scripted factory when a spec needs to capture
    the built evaluator instances.
    """
    make_run_repo = FakeRunRepositoryFactory(FakeRunRepository())
    ids = (f"run-{index}" for index in range(100))
    fixed_now = datetime(2026, 6, 24, tzinfo=UTC)
    make_freshness = FakeFreshnessReaderFactory(freshness_reader) if freshness_reader is not None else None
    handler = TuneRetrievalHandler(
        objective,
        _proposer_factory(proposals),
        evaluator_factory or _evaluator_factory(recall),
        make_run_repo,
        make_freshness_reader=make_freshness,
        clock=clock or StubClock([0.0, 0.0]),
        now=lambda: fixed_now,
        new_run_id=lambda: next(ids),
    )
    return handler, make_run_repo


class TestTuneRetrievalHandler:
    async def test_runs_the_loop_and_writes_the_winning_policy(self, tmp_path: Path):
        # Two strictly-improving rounds, no validation hold-out, so the loop stops on the round cap and
        # the winner is the last accepted candidate (v2). Its artifact must round-trip off disk.
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60), "v2": _uniform(0.80)}
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1"), _proposal("v2")], objective=objective)
        out = tmp_path / "winner.yaml"

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(out)))

        assert isinstance(report, TuningReport)
        assert out.exists()
        assert load_policy(out).instructions == "v2"
        assert report.out == str(out)
        assert report.n_rounds == 2
        assert report.accepted_rounds == 2
        assert report.stop_reason == "max_rounds"
        assert report.baseline_recall == pytest.approx(0.40)
        assert report.winner_recall == pytest.approx(0.80)
        assert report.winner_val_recall is None  # val_split == 0 disables the hold-out

    async def test_persists_one_summary_run_per_round_tagged_with_index_and_accept(self, tmp_path: Path):
        dataset = _tuning_dataset(tmp_path)
        # v1 improves (accepted), v2 ties v1 (rejected): two rounds, one accepted.
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.70), "v2": _uniform(0.70)}
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)
        handler, make_run_repo = _tune_handler(recall=recall, proposals=[_proposal("v1"), _proposal("v2")], objective=objective)
        out = tmp_path / "winner.yaml"

        await handler(TuneRetrieval(collection="research", dataset=str(dataset), model="gpt-4o-eval", out=str(out)))

        assert (make_run_repo.entered, make_run_repo.exited) == (1, 1)  # one run-repo scope, cleanly closed
        calls = make_run_repo.repo.add_run_calls
        assert len(calls) == 2  # one summary run per tuning round
        first, second = (summary for summary, _cases in calls)
        assert [s.variant["tuning_round"] for s in (first, second)] == [1, 2]
        assert [s.variant["accepted"] for s in (first, second)] == [True, False]
        assert first.variant["strategy"] == "full"
        assert first.dataset == str(dataset)
        assert first.model_name == "gpt-4o-eval"
        assert first.metrics["recall@5"] == pytest.approx(0.70)
        assert calls[0][1] == ()  # summary-only run history (no per-case rows)

    async def test_default_model_name_when_command_leaves_model_unset(self, tmp_path: Path):
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        handler, make_run_repo = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective)

        await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml")))

        assert make_run_repo.repo.add_run_calls[0][0].model_name == "<default>"

    async def test_emits_started_round_and_completed_events(self, tmp_path: Path):
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60), "v2": _uniform(0.80)}
        objective = TuningObjective(val_split=0.0, max_rounds=3, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1"), _proposal("v2")], objective=objective)
        out = tmp_path / "winner.yaml"

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(out)))

        assert [type(event) for event in handler.events] == [TuningStarted, RoundEvaluated, RoundEvaluated, TuningCompleted]
        started = handler.events[0]
        assert isinstance(started, TuningStarted)
        assert (started.dataset, started.out) == (str(dataset), str(out))
        rounds = [event for event in handler.events if isinstance(event, RoundEvaluated)]
        assert [(event.index, event.accepted) for event in rounds] == [(1, True), (2, True)]
        completed = handler.events[-1]
        assert isinstance(completed, TuningCompleted)
        assert completed.report == report

    async def test_per_command_objective_overrides_cap_the_rounds(self, tmp_path: Path):
        # The objective ships max_rounds=10; the command's `max_rounds=2` overrides it via model_copy,
        # so the loop runs a single proposal round then stops on the (overridden) round cap.
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60), "v2": _uniform(0.80)}
        objective = TuningObjective(val_split=0.0, max_rounds=10, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1"), _proposal("v2")], objective=objective)

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), max_rounds=2))

        assert report.n_rounds == 1
        assert report.stop_reason == "max_rounds"

    async def test_frozen_sidecar_is_excluded_then_scored_twice(self, tmp_path: Path):
        # A `golden.frozen.current` beside the dataset pulls `paper.a-0` out of
        # every working evaluation; the loop then scores the frozen yardstick exactly twice
        # (baseline, then winner) after the search ends.
        dataset = _tuning_dataset(tmp_path)
        save_frozen_split(
            FrozenSplit(
                dataset_name="golden",
                dataset_version=1,
                case_names=("paper.a-0",),
                salt="s",
                frozen_fraction=0.5,
                frozen_at="2026-07-05T00:00:00Z",
                staleness_at_freeze=0.0,
            ),
            tmp_path,
        )
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        evaluators: list[FakePolicyEvaluator] = []

        def capture_evaluator(model: str | None) -> FakePolicyEvaluator:
            del model
            evaluator = FakePolicyEvaluator(recall)
            evaluators.append(evaluator)
            return evaluator

        handler, _ = _tune_handler(
            recall=recall, proposals=[_proposal("v1")], objective=objective, evaluator_factory=capture_evaluator
        )

        await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml")))

        (evaluator,) = evaluators
        frozen_calls = [call for call in evaluator.evaluate_calls if call[1] == ("paper.a-0",)]
        assert len(frozen_calls) == 2  # exactly twice: baseline then winner, at run end
        assert evaluator.evaluate_calls[-2:] == frozen_calls
        assert [instructions for instructions, _ in frozen_calls] == [baseline_policy().instructions, "v1"]
        working = evaluator.evaluate_calls[:-2]
        assert working
        assert all("paper.a-0" not in case_names for _, case_names in working)

    async def test_duration_measured_by_injected_clock(self, tmp_path: Path):
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, clock=StubClock([1.0, 4.0]))

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml")))

        assert report.duration_seconds == pytest.approx(3.0)

    async def test_dispatch_through_real_message_bus(self, tmp_path: Path):
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective)
        bus = MessageBus({}, {})
        bus.register(TuneRetrieval, handler)
        out = tmp_path / "winner.yaml"

        result = await bus.handle(TuneRetrieval(collection="research", dataset=str(dataset), out=str(out)))

        assert isinstance(result, TuningReport)
        assert result.out == str(out)
        assert handler.events == []  # the bus drained the handler's events after dispatch

    async def test_wrong_command_type_raises_type_error(self):
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective)

        class OtherCommand(TuneRetrieval.__bases__[0]):
            pass

        with pytest.raises(TypeError):
            await handler(OtherCommand())

    # -- warn-never-gate staleness stamping ---------------------------------

    async def test_records_dataset_staleness_from_the_freshness_check(self, tmp_path: Path):
        # cs0's card body drifted (h0 -> DRIFTED), cs1 is intact, so one of the two verdict-able
        # page-anchored cases retires: the run records a 0.5 staleness fraction but never gates.
        dataset = _anchored_tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "DRIFTED", "cs1": "h1"}, sources={})
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml")))

        assert report.dataset_staleness == pytest.approx(0.5)

    async def test_all_fresh_dataset_records_zero_staleness_without_gating(self, tmp_path: Path):
        dataset = _anchored_tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})  # both intact
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml")))

        assert report.dataset_staleness == 0.0
        assert report.winner_recall == pytest.approx(0.60)  # the run itself is unchanged (no gate)

    async def test_staleness_is_none_when_no_freshness_reader_is_wired(self, tmp_path: Path):
        # The pre-Phase-6 construction (no freshness factory) leaves the sweep unwired and the field None.
        dataset = _tuning_dataset(tmp_path)
        recall = {baseline_policy().instructions: _uniform(0.40), "v1": _uniform(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective)

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml")))

        assert report.dataset_staleness is None

    async def test_premise_sweep_reaches_the_report_without_moving_verdicts(self, tmp_path: Path):
        # A trace-named case whose edge reads are empty: its premise is broken (D12, report-only)
        # while its freshness verdict stays fresh, and the handler report carries the tallies.
        dataset = _trace_named_dataset(tmp_path)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})
        handler = CheckFreshnessHandler(FakeFreshnessReaderFactory(reader), tmp_path)

        report = await handler(CheckFreshness(collection="research", dataset=str(dataset), out=str(tmp_path / "fresh")))

        assert (report.n_premise_checked, report.n_premise_broken) == (1, 1)
        artifact = load_freshness_report(Path(report.out))
        (premise,) = artifact.structural
        assert premise.case_name == "trace:cs0>cs1#0"
        assert premise.intact is False
        assert premise.missing == ("edge cs0->cs1 gone",)
        assert all(verdict.verdict == "fresh" for verdict in artifact.verdicts)
        assert "Structural integrity" in Path(report.markdown).read_text(encoding="utf-8")

class TestApplyRuntimeGuardrails:
    """`--apply-runtime` applies iff all four guardrails pass; every refusal is a reported outcome.

    Each spec scripts exactly one failing guardrail on an anchored dataset (so the freshness check can
    verdict staleness) with a one-case frozen sidecar (`cs0-0`). The apply target is a separate
    `knowledge_base/` root so the assertion on `_meta/active-policy.yaml` is unambiguous.
    """

    @staticmethod
    def _freeze_anchored(tmp_path: Path, case_names: tuple[str, ...] = ("cs0-0",)) -> None:
        save_frozen_split(
            FrozenSplit(
                dataset_name="golden",
                dataset_version=1,
                case_names=case_names,
                salt="s",
                frozen_fraction=0.5,
                frozen_at="2026-07-05T00:00:00Z",
                staleness_at_freeze=0.0,
            ),
            tmp_path,
        )

    async def test_all_green_applies_the_winner_to_the_knowledge_base(self, tmp_path: Path):
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})  # all fresh
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert (report.applied_runtime, report.apply_refusal) == (True, None)
        assert report.frozen_beat is True
        assert load_policy(knowledge_base / "_meta" / "active-policy.yaml").instructions == "v1"

    async def test_without_the_flag_nothing_applies_and_no_refusal_is_recorded(self, tmp_path: Path):
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base)))

        assert (report.applied_runtime, report.apply_refusal) == (False, None)
        assert not (knowledge_base / "_meta" / "active-policy.yaml").exists()

    async def test_refuses_without_a_frozen_sidecar(self, tmp_path: Path):
        dataset = _anchored_tuning_dataset(tmp_path)  # no freeze
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert report.applied_runtime is False
        assert report.apply_refusal == "no frozen split for the dataset (run `evals freeze` first)"
        assert not (knowledge_base / "_meta" / "active-policy.yaml").exists()

    async def test_refuses_a_stale_dataset(self, tmp_path: Path):
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "DRIFTED", "cs1": "h1"}, sources={})  # 0.5 stale
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert report.applied_runtime is False
        assert report.apply_refusal == "dataset staleness 0.500 exceeds apply_staleness_max 0.000"

    async def test_refuses_a_stale_frozen_split_even_when_the_dataset_gate_passes(self, tmp_path: Path):
        # A permissive dataset gate (0.5) with a fully stale one-case frozen subset: the dataset's
        # 0.5 staleness passes, the frozen subset's 1.0 refuses (the frozen leg of the gate).
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10, apply_staleness_max=0.5)
        reader = FakeFreshnessReader(hashes={"cs0": "DRIFTED", "cs1": "h1"}, sources={})  # cs0 (frozen) stale
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert report.applied_runtime is False
        assert report.apply_refusal == "frozen-split staleness 1.000 exceeds apply_staleness_max 0.500"

    async def test_refuses_an_unmeasured_staleness(self, tmp_path: Path):
        # No freshness reader wired: humans get a warn-less run, the machine rung refuses.
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.60)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert report.applied_runtime is False
        assert report.apply_refusal == "dataset staleness unmeasured (freshness check unwired)"

    async def test_refuses_when_the_winner_is_the_incumbent(self, tmp_path: Path):
        # v1 ties the baseline everywhere, so no round is accepted and the winner IS the incumbent.
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: _anchored_recall(0.40), "v1": _anchored_recall(0.40)}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert report.applied_runtime is False
        assert report.apply_refusal == "the winner is the incumbent baseline (nothing to apply)"

    async def test_refuses_a_winner_that_did_not_beat_the_incumbent_on_the_frozen_split(self, tmp_path: Path):
        # v1 improves only the non-frozen case (cs1-1): accepted on the core, but the frozen case
        # (cs0-0) never moves, so the frozen-split beat fails and the apply is refused.
        dataset = _anchored_tuning_dataset(tmp_path)
        self._freeze_anchored(tmp_path)
        knowledge_base = tmp_path / "knowledge_base"
        recall = {baseline_policy().instructions: {"cs0-0": 0.40, "cs1-1": 0.40}, "v1": {"cs0-0": 0.40, "cs1-1": 0.80}}
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)
        reader = FakeFreshnessReader(hashes={"cs0": "h0", "cs1": "h1"}, sources={})
        handler, _ = _tune_handler(recall=recall, proposals=[_proposal("v1")], objective=objective, freshness_reader=reader)

        report = await handler(
            TuneRetrieval(collection="research", dataset=str(dataset), out=str(tmp_path / "w.yaml"), knowledge_base=str(knowledge_base), apply_runtime=True)
        )

        assert report.applied_runtime is False
        assert report.apply_refusal == "the winner did not beat the incumbent on the frozen split"
        assert report.frozen_beat is False


class TestRollbackPolicyHandler:
    """The evals seam over the Knowledge base's `ApplyRetrievalPolicy` restore."""

    async def test_restores_the_latest_snapshot_and_reports_the_lineage(self, tmp_path: Path):
        knowledge_base = tmp_path / "knowledge_base"
        apply_retrieval_policy(RetrievalPolicy(instructions="A", strategy="full", tool_descriptions={}), knowledge_base_path=knowledge_base)
        apply_retrieval_policy(RetrievalPolicy(instructions="B", strategy="full", tool_descriptions={}), knowledge_base_path=knowledge_base)

        report = await RollbackPolicyHandler()(RollbackPolicy(knowledge_base=str(knowledge_base)))

        assert load_policy(knowledge_base / "_meta" / "active-policy.yaml").instructions == "A"
        assert report.restored_from.endswith("active-policy.v1.yaml")
        assert report.history_snapshot is not None
        assert report.history_snapshot.endswith("active-policy.v2.yaml")

    async def test_no_history_raises_cleanly(self, tmp_path: Path):
        with pytest.raises(ValueError, match="no policy history"):
            await RollbackPolicyHandler()(RollbackPolicy(knowledge_base=str(tmp_path)))


def _trace_named_dataset(tmp_path: Path) -> Path:
    """A one-case dataset whose case is trace-seed-named (`trace:cs0>cs1#0`) and page-anchored.

    Built from a `Case` directly (not `build_dataset`, which derives positional names) so the premise
    sweep can parse the trace hops out of the name while the verdict core sees ordinary anchors.
    """
    case = Case(
        name="trace:cs0>cs1#0",
        inputs=QaInput(question="How does cs0 lead to cs1?"),
        expected_output=RetrievalAnswer(answer="a", cited_source_ids=["cs0", "cs1"]),
        metadata=QaCaseMeta(
            anchor_page_id="cs0",
            difficulty="easy",
            archetype=Archetype.TRACE,
            anchors=(AnchorHash(unit_id="cs0", body_hash="h0"), AnchorHash(unit_id="cs1", body_hash="h1")),
        ),
    )
    dataset: EvaluationDataset = EvaluationDataset(name="trace-premise", cases=[case], evaluators=[])
    path = tmp_path / "golden.yaml"
    save_dataset(dataset, path)
    return path


def _anchored_tuning_dataset(tmp_path: Path) -> Path:
    """A two-case dataset whose cases carry page anchors, so the freshness check can verdict them.

    `build_dataset` names the cases `cs0-0` and `cs1-1` (`{paper_id}-{index}`), matching `_anchored_recall`.
    """
    cases = [
        _freshness_case("cs0", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="cs0", body_hash="h0"),)),
        _freshness_case("cs1", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="cs1", body_hash="h1"),)),
    ]
    path = tmp_path / "golden.yaml"
    save_dataset(build_dataset(cases), path)
    return path


def _anchored_recall(recall: float) -> dict[str, float]:
    """The same `recall@5` for both anchored-dataset cases (`cs0-0`, `cs1-1`)."""
    return {"cs0-0": recall, "cs1-1": recall}


def _uniform(recall: float) -> dict[str, float]:
    """The same `recall@5` for both tuning-dataset cases.

    `build_dataset` names a case `"{paper_id}-{enumeration_index}"`, so the two cases (`paper.a` at
    index 0, `paper.b` at index 1) become `paper.a-0` and `paper.b-1`.
    """
    return {"paper.a-0": recall, "paper.b-1": recall}


# -- CheckFreshnessHandler ----------------------------------------------------
#
# The handler is the read-only CQRS seam for `evals freshness`: it resolves a dataset version
# (`.current` or an explicit path), extracts the freshness refs, reads the current page state + the
# drafting-text sources through a faked reader, runs the pure verdict core, and writes a
# FreshnessReport YAML + a markdown summary. It never mutates the DB or any dataset file; the only IO
# is the two artifact writes and reading the swept dataset under tmp_path.


class FakeFreshnessReader:
    """A `FreshnessReader` stand-in over canned knowledge base state (public, scriptable, fake convention).

    `hashes` maps a page id to its current card body hash (an id absent from it reads as a vanished
    page); `sources` maps a case name to its recomputed drafting-text constituents (a mirror-era case
    absent from it is a recompute failure the handler maps to None, so the pure core yields
    `mirror_stale`, D10). `page_state_calls` / `mirror_sources_calls` record the reads for assertions.
    """

    def __init__(self, *, hashes: Mapping[str, str], sources: Mapping[str, tuple[str, ...]]) -> None:
        self.hashes = dict(hashes)
        self.sources = dict(sources)
        self.page_state_calls: list[frozenset[str]] = []
        self.mirror_sources_calls: list[tuple[str, ...]] = []
        # The premise reads (D12), scripted per spec; empty defaults read as "nothing live".
        self.edge_pairs: frozenset[tuple[str, str]] = frozenset()
        self.inbound: dict[tuple[str, str], int] = {}
        self.outbound: dict[tuple[str, str], int] = {}
        self.cells: dict[str, int] = {}

    async def page_state(self, *, collection: str, page_ids: frozenset[str]) -> Mapping[str, str]:
        del collection
        self.page_state_calls.append(page_ids)
        return {page_id: self.hashes[page_id] for page_id in page_ids if page_id in self.hashes}

    async def mirror_sources(self, *, refs: Sequence[CaseFreshnessRef]) -> Mapping[str, tuple[str, ...]]:
        requested = tuple(ref.case_name for ref in refs)
        self.mirror_sources_calls.append(requested)
        return {name: body for name, body in self.sources.items() if name in requested}

    async def edge_pairs_present(self, *, pairs: frozenset[tuple[str, str]]) -> frozenset[tuple[str, str]]:
        return self.edge_pairs & pairs

    async def inbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        return {key: fan for key, fan in self.inbound.items() if key in keys}

    async def outbound_fans(self, *, keys: frozenset[tuple[str, str]]) -> Mapping[tuple[str, str], int]:
        return {key: fan for key, fan in self.outbound.items() if key in keys}

class FakeFreshnessReaderFactory:
    """A `FreshnessReaderFactory` yielding one canned reader, recording its scope open / close.

    Typed against the `FreshnessReader` port so it carries either the scriptable `FakeFreshnessReader`
    (the sweep specs) or the permissive `_AllFreshFreshnessReader` (the generate handler's default).
    """

    def __init__(self, reader: FreshnessReader) -> None:
        self.reader = reader
        self.entered = 0
        self.exited = 0

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[FreshnessReader]:
        self.entered += 1
        try:
            yield self.reader
        finally:
            self.exited += 1


def _freshness_case(
    paper_id: str,
    *,
    archetype: Archetype | None = None,
    anchors: tuple[AnchorHash, ...] = (),
    mirror_hash_value: str | None = None,
    gold: tuple[str, ...] | None = None,
) -> AuthoredCase:
    """An authored case carrying the anchors / mirror hash the freshness refs are extracted from."""
    return AuthoredCase(
        question=QaInput(question=f"q-{paper_id}"),
        expected=ExpectedQa(answer="a", expected_source_ids=gold if gold is not None else (paper_id,)),
        meta=QaCaseMeta(anchor_page_id=paper_id, difficulty="easy", archetype=archetype, anchors=anchors, mirror_hash=mirror_hash_value),
    )


def _save_freshness_version(datasets_dir: Path, name: str, version: int, cases: list[AuthoredCase]) -> Path:
    """Save one immutable dataset version + its stamp, returning its path (the sweep's input)."""
    path = dataset_version_path(datasets_dir, name, version)
    stamp = DatasetStamp(salt="s", knowledge_base_fingerprint="fp:research", budget=10, parent_version=None, coverage={})
    save_dataset_version(build_dataset(cases), stamp, path)
    return path


def _freshness_handler(
    datasets_dir: Path, reader: FakeFreshnessReader, *, clock: StubClock | None = None
) -> tuple[CheckFreshnessHandler, FakeFreshnessReaderFactory]:
    """Build the freshness handler over a faked per-command reader factory and the tmp datasets dir."""
    make_reader = FakeFreshnessReaderFactory(reader)
    if clock is None:
        return CheckFreshnessHandler(make_reader, datasets_dir), make_reader
    return CheckFreshnessHandler(make_reader, datasets_dir, clock=clock), make_reader


class TestCheckFreshnessHandler:
    async def test_fresh_dataset_reports_all_fresh_and_writes_artifacts(self, tmp_path: Path):
        constituents = ("claim", "quote", "card")
        cases = [
            _freshness_case(
                "p0",
                archetype=Archetype.PINPOINT,
                anchors=(AnchorHash(unit_id="p0", body_hash="h0"),),
                mirror_hash_value=mirror_hash(constituents),
            )
        ]
        path = _save_freshness_version(tmp_path, "research", 1, cases)
        reader = FakeFreshnessReader(hashes={"p0": "h0"}, sources={"p0-0": constituents})
        handler, make_reader = _freshness_handler(tmp_path, reader, clock=StubClock([1.0, 3.5]))
        out = tmp_path / "freshness"

        report = await handler(CheckFreshness(collection="research", dataset=str(path), out=str(out)))

        assert isinstance(report, FreshnessCheckedReport)
        assert report.counts["fresh"] == 1
        assert report.counts["mirror_stale"] == 0
        assert report.staleness_fraction == 0.0
        assert report.top_stale_pages == ()  # an all-fresh sweep invalidates no page
        assert report.dataset_version == "research.v1.yaml"
        assert report.duration_seconds == pytest.approx(2.5)
        yaml_path = out / "research.v1.freshness.yaml"
        markdown_path = out / "research.v1.freshness.md"
        assert (report.out, report.markdown) == (str(yaml_path), str(markdown_path))
        assert yaml_path.exists()
        assert markdown_path.exists()
        assert load_freshness_report(yaml_path).staleness_fraction == 0.0
        assert (make_reader.entered, make_reader.exited) == (1, 1)  # one reader session, cleanly closed

    async def test_drifted_page_reports_page_drifted_and_mirror_stale_with_attribution(self, tmp_path: Path):
        constituents = ("b", "bq", "bcard")
        cases = [
            _freshness_case(
                "pa", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="pa", body_hash="old"),)
            ),  # page-anchored (no mirror)
            _freshness_case(
                "pb",
                archetype=Archetype.PINPOINT,
                anchors=(AnchorHash(unit_id="pb", body_hash="hb"),),
                mirror_hash_value=mirror_hash(constituents),
            ),
        ]
        path = _save_freshness_version(tmp_path, "research", 1, cases)
        # pa's card body drifted (old -> new); pb's mirror recomputes differently (its claim text moved).
        reader = FakeFreshnessReader(hashes={"pa": "new", "pb": "hb"}, sources={"pb-1": ("b CHANGED", "bq", "bcard")})
        handler, _ = _freshness_handler(tmp_path, reader)
        out = tmp_path / "freshness"

        report = await handler(CheckFreshness(collection="research", dataset=str(path), out=str(out)))

        assert report.counts["page_drifted"] == 1
        assert report.counts["mirror_stale"] == 1
        assert report.staleness_fraction == 1.0  # both verdict-able cases retire (page-era drift + mirror drift)
        assert load_freshness_report(out / "research.v1.freshness.yaml").stale_by_page == {"pa": 1}

    async def test_top_stale_pages_are_ranked_by_count_then_id_and_capped(self, tmp_path: Path):
        # Two page-anchored cases whose anchors all drift: "hot" is invalidated by both (count 2), the
        # rest by one each, so the report carries the count-desc / id-asc top-5 (dropping the sixth, "e").
        cases = [
            _freshness_case(
                "c1",
                archetype=Archetype.PINPOINT,
                gold=("g",),
                anchors=tuple(AnchorHash(unit_id=unit, body_hash="old") for unit in ("hot", "a", "b", "c", "d", "e")),
            ),
            _freshness_case("c2", archetype=Archetype.PINPOINT, gold=("g",), anchors=(AnchorHash(unit_id="hot", body_hash="old"),)),
        ]
        path = _save_freshness_version(tmp_path, "research", 1, cases)
        reader = FakeFreshnessReader(
            hashes={"g": "hg", "hot": "new", "a": "new", "b": "new", "c": "new", "d": "new", "e": "new"}, sources={}
        )
        handler, _ = _freshness_handler(tmp_path, reader)
        out = tmp_path / "freshness"

        report = await handler(CheckFreshness(collection="research", dataset=str(path), out=str(out)))

        assert report.top_stale_pages == (("hot", 2), ("a", 1), ("b", 1), ("c", 1), ("d", 1))

    async def test_missing_pinpoint_basis_reports_mirror_stale(self, tmp_path: Path):
        cases = [
            _freshness_case(
                "pc",
                archetype=Archetype.PINPOINT,
                anchors=(AnchorHash(unit_id="pc", body_hash="hc"),),
                mirror_hash_value=mirror_hash(("c", "cq", "ccard")),
            )
        ]
        path = _save_freshness_version(tmp_path, "research", 1, cases)
        # The gold page and its anchor are intact, but the pinpoint basis (the claim row) is gone.
        reader = FakeFreshnessReader(hashes={"pc": "hc"}, sources={})
        handler, _ = _freshness_handler(tmp_path, reader)
        out = tmp_path / "freshness"

        report = await handler(CheckFreshness(collection="research", dataset=str(path), out=str(out)))

        assert report.counts == {"fresh": 0, "page_drifted": 0, "mirror_stale": 1, "gold_gone": 0, "unanchored": 0}
        assert report.staleness_fraction == 1.0

    async def test_resolves_the_current_pointer_and_an_explicit_path(self, tmp_path: Path):
        cases = [
            _freshness_case(
                "pd",
                archetype=Archetype.PINPOINT,
                anchors=(AnchorHash(unit_id="pd", body_hash="hd"),),
                mirror_hash_value=mirror_hash(("d",)),
            )
        ]
        path = _save_freshness_version(tmp_path, "research", 1, cases)
        write_current(tmp_path, "research", path)
        reader = FakeFreshnessReader(hashes={"pd": "hd"}, sources={"pd-0": ("d",)})
        out = tmp_path / "freshness"

        via_none = await _freshness_handler(tmp_path, reader)[0](CheckFreshness(collection="research", dataset=None, out=str(out)))
        via_current = await _freshness_handler(tmp_path, reader)[0](CheckFreshness(collection="research", dataset="current", out=str(out)))
        via_explicit = await _freshness_handler(tmp_path, reader)[0](CheckFreshness(collection="research", dataset=str(path), out=str(out)))

        assert via_none.dataset_version == "research.v1.yaml"
        assert via_current.dataset_version == "research.v1.yaml"
        assert via_explicit.dataset_version == "research.v1.yaml"
        assert {via_none.counts["fresh"], via_current.counts["fresh"], via_explicit.counts["fresh"]} == {1}

    async def test_unanchored_cases_are_excluded_from_the_fraction(self, tmp_path: Path):
        cases = [
            _freshness_case(
                "pe",
                archetype=Archetype.PINPOINT,
                anchors=(AnchorHash(unit_id="pe", body_hash="he"),),
                mirror_hash_value=mirror_hash(("e",)),
            ),
            _freshness_case("leg"),  # no anchors, no mirror hash, positional name -> unanchored era
        ]
        path = _save_freshness_version(tmp_path, "research", 1, cases)
        reader = FakeFreshnessReader(hashes={"pe": "he", "leg": "hleg"}, sources={"pe-0": ("e",)})
        handler, _ = _freshness_handler(tmp_path, reader)
        out = tmp_path / "freshness"

        report = await handler(CheckFreshness(collection="research", dataset=str(path), out=str(out)))

        assert report.counts["unanchored"] == 1
        assert report.counts["fresh"] == 1
        assert report.staleness_fraction == 0.0  # unanchored excluded from the denominator; the sole verdict-able case is fresh


# -- VerifyRecommendationHandler staleness stamping ---------------------------------
#
# The verify verb sweeps its re-run dataset for staleness the same way tune does and stamps
# `dataset_staleness` on the report (warn-never-gate): the freshness reader is a keyword-optional
# collaborator, so constructing without one leaves the field None. These specs reuse the module-level
# `_Report` / `_Case` / `_Score` evaluator stand-ins and `_freshness_case`.


class _FakeVerifyEvaluatorFactory:
    """A `PolicyEvaluatorFactory` whose evaluator returns a scripted verify re-run report."""

    def __init__(self, report: _Report) -> None:
        self.report = report

    def __call__(self, model: str | None) -> _FakeVerifyEvaluatorFactory:
        del model
        return self

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        del policy, dataset
        return self.report


class _CannedFactory[T]:
    """A one-item async-context-manager factory over a canned gateway or run repository."""

    def __init__(self, item: T) -> None:
        self.item = item

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[T]:
        yield self.item


def _verify_report_case(question: str, gold: str, *, recall: float = 1.0) -> _Case:
    """One re-run report case scored on `gold`, the slice `diagnose` and the verify folds read."""
    return _Case(
        name=gold,
        inputs=QaInput(question=question),
        metadata=QaCaseMeta(anchor_page_id=gold, difficulty="easy", archetype=Archetype.PINPOINT),
        expected_output=RetrievalAnswer(answer="ref", cited_source_ids=[gold]),
        output=RetrievalAnswer(answer="got", cited_source_ids=[gold]),
        scores={"recall@5": _Score(recall), "n_tool_calls": _Score(2.0)},
        metrics={"input_tokens": 100.0},
        total_duration=0.2,
    )


def _verify_dataset(tmp_path: Path) -> Path:
    """A two-case dataset whose cases carry page anchors (named `va-0`, `vb-1`), swept for staleness."""
    cases = [
        _freshness_case("va", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="va", body_hash="hva"),)),
        _freshness_case("vb", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="vb", body_hash="hvb"),)),
    ]
    path = tmp_path / "golden.yaml"
    save_dataset(build_dataset(cases), path)
    return path


def _verify_recommendation(dataset_path: Path) -> Recommendation:
    """A recommendation whose baseline cases match the dataset's names, with one increase prediction."""
    return Recommendation(
        collection="research",
        created_at=datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        baseline=BaselineSnapshot(
            dataset=str(dataset_path),
            run_id="run-0",
            policy_source="default",
            evidence_grade="raw-baseline",
            knowledge_base=KnowledgeBaseStamp(collection="research", spec_hash="spec-1", manifest_digest="manifest-1", n_pages=2),
            overall={"recall@5": 0.0},
            cases=(
                CaseSnapshot(case_name="va-0", recall=0.0, gold_ids=("va",), cited_ids=()),
                CaseSnapshot(case_name="vb-1", recall=0.0, gold_ids=("vb",), cited_ids=()),
            ),
        ),
        proposals=(
            SchemaEditProposal(
                id="restructure-paper-sections",
                op=RestructureSectionsOp(page_type="paper", canonical_sections=("summary", "findings-a", "findings-b")),
                evidence=ProposalEvidence(
                    case_names=("va-0",), recall_deficit=0.5, counterfactual=None, mined_links=(), oversized_sections=()
                ),
                predictions=(Prediction(description="va recovers", target_cases=("va-0",), metric="recall@5", direction="increase"),),
                migration=(PageWorkOrder(page_id="va", note_path="Memory/paper/a.md", action="split findings"),),
                rationale="oversized findings",
            ),
        ),
        findings=(),
    )


def _verify_handler(
    knowledge_base: Path, gateway: FakeKnowledgeBaseGateway, *, report: _Report, freshness_reader: FreshnessReader | None = None
) -> VerifyRecommendationHandler:
    """Build the verify handler over canned evaluator / gateway / run-repo factories, wiring the sweep when given."""
    make_freshness = FakeFreshnessReaderFactory(freshness_reader) if freshness_reader is not None else None
    return VerifyRecommendationHandler(
        _FakeVerifyEvaluatorFactory(report),
        _CannedFactory(gateway),
        _CannedFactory(FakeRunRepository()),
        knowledge_base,
        make_freshness_reader=make_freshness,
        now=lambda: datetime(2026, 7, 4, tzinfo=UTC),
    )


def _verify_gateway() -> FakeKnowledgeBaseGateway:
    """A gateway that keeps both gold pages present (no drop) and reports no knowledge base drift."""
    return FakeKnowledgeBaseGateway(
        page_types_result={"va": "paper", "vb": "paper"},
        stamp_result=KnowledgeBaseStamp(collection="research", spec_hash="spec-1", manifest_digest="manifest-1", n_pages=2),
        changed_paths_result=(),
    )


class TestVerifyRecommendationStaleness:
    async def test_records_dataset_staleness_from_the_sweep(self, tmp_path: Path):
        # va's card body drifted (hva -> DRIFTED), vb intact: one of two verdict-able cases retires.
        dataset_path = _verify_dataset(tmp_path)
        recommendation_path = tmp_path / "research.yaml"
        save_recommendation(_verify_recommendation(dataset_path), recommendation_path)
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        reader = FakeFreshnessReader(hashes={"va": "DRIFTED", "vb": "hvb"}, sources={})
        report_cases = _Report(cases=[_verify_report_case("q-va", "va"), _verify_report_case("q-vb", "vb")])
        handler = _verify_handler(knowledge_base, _verify_gateway(), report=report_cases, freshness_reader=reader)

        report = await handler.handle(VerifyRecommendation(recommendation=str(recommendation_path)))

        assert isinstance(report, VerifyReport)
        assert report.dataset_staleness == pytest.approx(0.5)

    async def test_all_fresh_dataset_records_zero_staleness_without_gating(self, tmp_path: Path):
        dataset_path = _verify_dataset(tmp_path)
        recommendation_path = tmp_path / "research.yaml"
        save_recommendation(_verify_recommendation(dataset_path), recommendation_path)
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        reader = FakeFreshnessReader(hashes={"va": "hva", "vb": "hvb"}, sources={})  # both intact
        report_cases = _Report(cases=[_verify_report_case("q-va", "va"), _verify_report_case("q-vb", "vb")])
        handler = _verify_handler(knowledge_base, _verify_gateway(), report=report_cases, freshness_reader=reader)

        report = await handler.handle(VerifyRecommendation(recommendation=str(recommendation_path)))

        assert report.dataset_staleness == 0.0
        assert report.n_retained == 2  # the run itself is unchanged (no gate)

    async def test_staleness_is_none_when_no_freshness_reader_is_wired(self, tmp_path: Path):
        dataset_path = _verify_dataset(tmp_path)
        recommendation_path = tmp_path / "research.yaml"
        save_recommendation(_verify_recommendation(dataset_path), recommendation_path)
        knowledge_base = tmp_path / "knowledge_base"
        knowledge_base.mkdir()
        report_cases = _Report(cases=[_verify_report_case("q-va", "va"), _verify_report_case("q-vb", "vb")])
        handler = _verify_handler(knowledge_base, _verify_gateway(), report=report_cases)

        report = await handler.handle(VerifyRecommendation(recommendation=str(recommendation_path)))

        assert report.dataset_staleness is None

# -- FreezeDatasetHandler ------------------------------------------------------
#
# The `evals freeze` seam: resolve an immutable dataset version, sweep it for staleness against the live
# knowledge base, carve a stratified salted-hash frozen subset, and write an append-only `<name>.frozen.v{M}.json`
# sidecar plus its `.frozen.current` pointer. Faked network-free over the same `FakeFreshnessReader` the
# sweep specs use; the only IO is the version + sidecar round-trip under `tmp_path`.


def _freeze_handler(
    datasets_dir: Path, reader: FakeFreshnessReader, *, fraction: float = 0.5, min_per_stratum: int = 1, salt: str = "carve"
) -> tuple[FreezeDatasetHandler, FakeFreshnessReaderFactory]:
    """Build the freeze handler over a faked per-command reader factory with a deterministic stamp + salt."""
    make_reader = FakeFreshnessReaderFactory(reader)
    handler = FreezeDatasetHandler(
        make_reader,
        datasets_dir,
        frozen_fraction=fraction,
        frozen_min_per_stratum=min_per_stratum,
        now=lambda: datetime(2026, 7, 5, tzinfo=UTC),
        make_salt=lambda: salt,
    )
    return handler, make_reader


def _freeze_cases() -> list[AuthoredCase]:
    """Four all-fresh cases across two archetype strata (page-anchored, gold == the anchor)."""
    return [
        _freshness_case("p0", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="p0", body_hash="h0"),), gold=("p0",)),
        _freshness_case("p1", archetype=Archetype.PINPOINT, anchors=(AnchorHash(unit_id="p1", body_hash="h1"),), gold=("p1",)),
        _freshness_case("s0", archetype=Archetype.SURVEY, anchors=(AnchorHash(unit_id="s0", body_hash="hs0"),), gold=("s0",)),
        _freshness_case("s1", archetype=Archetype.SURVEY, anchors=(AnchorHash(unit_id="s1", body_hash="hs1"),), gold=("s1",)),
    ]


_ALL_FRESH_HASHES = {"p0": "h0", "p1": "h1", "s0": "hs0", "s1": "hs1"}


class TestFreezeDatasetHandler:
    async def test_carves_and_writes_the_frozen_sidecar_and_pointer(self, tmp_path: Path):
        path = _save_freshness_version(tmp_path, "research", 1, _freeze_cases())
        write_current(tmp_path, "research", path)
        reader = FakeFreshnessReader(hashes=_ALL_FRESH_HASHES, sources={})
        handler, make_reader = _freeze_handler(tmp_path, reader, fraction=0.5, min_per_stratum=1)

        report = await handler(FreezeDataset(collection="research", dataset="current"))

        assert isinstance(report, DatasetFrozenReport)
        assert report.dataset_version == "research.v1.yaml"
        assert report.frozen_version == 1
        assert report.n_frozen == 2  # round(0.5 * 4)
        assert report.staleness_at_freeze == 0.0
        assert sum(report.per_stratum.values()) == 2
        assert report.sidecar == str(tmp_path / "research.frozen.v1.json")
        split = load_frozen_split(tmp_path, "research")
        assert split is not None
        assert len(split.case_names) == 2
        assert split.dataset_version == 1
        assert split.salt == "carve"
        assert split.parent_frozen_version is None
        assert split.frozen_at == "2026-07-05T00:00:00+00:00"
        assert (tmp_path / "research.frozen.current").read_text(encoding="utf-8").strip() == "research.frozen.v1.json"
        assert (make_reader.entered, make_reader.exited) == (1, 1)  # one reader session, cleanly closed

    async def test_records_the_staleness_at_freeze(self, tmp_path: Path):
        path = _save_freshness_version(tmp_path, "research", 1, _freeze_cases())
        write_current(tmp_path, "research", path)
        drifted = {**_ALL_FRESH_HASHES, "s0": "changed"}  # one page's card body hash drifted from its anchor
        reader = FakeFreshnessReader(hashes=drifted, sources={})
        handler, _ = _freeze_handler(tmp_path, reader, fraction=1.0)

        report = await handler(FreezeDataset(collection="research", dataset="current"))

        assert report.staleness_at_freeze == pytest.approx(0.25)  # 1 of 4 verdict-able cases drifted
        split = load_frozen_split(tmp_path, "research")
        assert split is not None
        assert split.staleness_at_freeze == pytest.approx(0.25)

    async def test_command_fraction_overrides_the_configured_default(self, tmp_path: Path):
        path = _save_freshness_version(tmp_path, "research", 1, _freeze_cases())
        write_current(tmp_path, "research", path)
        reader = FakeFreshnessReader(hashes=_ALL_FRESH_HASHES, sources={})
        handler, _ = _freeze_handler(tmp_path, reader, fraction=0.5)  # configured default 0.5

        report = await handler(FreezeDataset(collection="research", dataset="current", fraction=1.0))

        assert report.n_frozen == 4  # the command's 1.0 freezes the whole version, overriding 0.5

    async def test_freezing_again_writes_the_next_version_with_parent_lineage(self, tmp_path: Path):
        path = _save_freshness_version(tmp_path, "research", 1, _freeze_cases())
        write_current(tmp_path, "research", path)
        reader = FakeFreshnessReader(hashes=_ALL_FRESH_HASHES, sources={})
        handler, _ = _freeze_handler(tmp_path, reader, fraction=0.5)

        first = await handler(FreezeDataset(collection="research", dataset="current"))
        second = await handler(FreezeDataset(collection="research", dataset="current"))

        assert (first.frozen_version, second.frozen_version) == (1, 2)
        assert (tmp_path / "research.frozen.v1.json").exists()  # append-only: v1 is untouched
        split = load_frozen_split(tmp_path, "research")
        assert split is not None
        assert split.parent_frozen_version == 1
        assert (tmp_path / "research.frozen.current").read_text(encoding="utf-8").strip() == "research.frozen.v2.json"

    async def test_explicit_name_version_resolves_without_the_pointer(self, tmp_path: Path):
        _save_freshness_version(tmp_path, "research", 1, _freeze_cases())  # no `.current` pointer written
        reader = FakeFreshnessReader(hashes=_ALL_FRESH_HASHES, sources={})
        handler, _ = _freeze_handler(tmp_path, reader, fraction=0.5)

        report = await handler(FreezeDataset(collection="research", dataset="research:v1"))

        assert report.dataset_version == "research.v1.yaml"
        assert report.frozen_version == 1

    async def test_missing_current_pointer_fails_loudly(self, tmp_path: Path):
        reader = FakeFreshnessReader(hashes={}, sources={})
        handler, _ = _freeze_handler(tmp_path, reader)

        with pytest.raises(ValueError, match="no current dataset pointer"):
            await handler(FreezeDataset(collection="research", dataset="current"))

    async def test_wrong_command_type_raises_type_error(self, tmp_path: Path):
        reader = FakeFreshnessReader(hashes={}, sources={})
        handler, _ = _freeze_handler(tmp_path, reader)

        with pytest.raises(TypeError, match="expected FreezeDataset"):
            await handler(CheckFreshness(collection="research", dataset=None, out=str(tmp_path)))
