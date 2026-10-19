r"""Unit specs for the evals slice's wireup providers (`src/studio/knowledge_base/wiring/evals.py`).

The evals wiring module is the only evals module that knows DI. Its retrieval providers expose
the agent-under-test's retrieval surface as wireup-resolvable dependencies, reusing the serve
slice's single-source retrieval seam (`engram.retrieval.tools`) rather than re-implementing it:

* :func:`~studio.knowledge_base.wiring.evals.retrieval_handlers_provider` builds a
  :class:`~engram.retrieval.tools.RetrievalHandlers` group from the four existing serve query
  handlers (:class:`~engram.retrieval.queries.SearchHandler` &co.) wireup injects — bundling the
  real handler instances as-is (each satisfies the group's structural single-method port);
* the deps-agnostic :class:`~pydantic_ai.FunctionToolset` is **not** a registration: the tool
  loadout is a per-run experiment axis, so the harness builds it per run via
  :func:`~engram.retrieval.tools.build_retrieval_toolset` over that group — pinned here so the
  group-to-toolset contract stays covered.

Its generate-path providers supply the four collaborators the `GenerateEvalDatasetHandler` injects —
the `GenerationConfig` mapped from settings (the boundary that validates archetype names), the
`CaseCurator` built over the index slice's
`BatchEmbedder`, the `SeedRepositoryFactory` that opens a fresh owner session per command, and the
`DrafterFactory` that resolves the runtime `--model` choice to a `PydanticAiDrafter`:

* :func:`~studio.knowledge_base.wiring.evals.generation_config_provider` keys the distribution by `Archetype` and
  rejects an unknown name (a typo'd config fails loudly);
* :func:`~studio.knowledge_base.wiring.evals.case_curator_provider` binds the embedder and the config's dedup cutoff;
* :class:`~studio.knowledge_base.wiring.evals.seed_repository_factory_provider` has a `__call__` that is
  an async context manager yielding a `SeedQueries` over a new owner session;
* :func:`~studio.knowledge_base.wiring.evals.drafter_factory_provider` resolves `None` / `"test-model"` to a
  network-free `PydanticAiDrafter`, an Azure name to one over a real chat model (no network at
  construction).

The providers are plain factory functions decorated for injection, so these specs call the
underlying callables directly (no container). The injected handlers are the **real** serve
handlers over the shared :class:`~engram.testing.fakes.gateway.FakeSqlGateway` /
:class:`~engram.testing.fakes.embedding.FakeEmbedding` (fakes-over-mocks, no database) — so the
arguments are exactly the concrete types the providers declare and the group wires unchanged.

The headline `TestGeneratePathContainerWiring` resolves the whole generate path through the real
`create_container` graph (mirroring the sync slice's `test_container`): resolving the
`GenerateEvalDatasetHandler` leaves the shared `MessageBus` routing `GenerateEvalDataset`. The
engine + embedder are overridden with inert fakes, so no Postgres or Azure is touched.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from core.ai.embedding import BatchEmbedder
from core.cqrs.contracts import MessageBus
from engram.evals.commands import CheckFreshness, ComputeCoverage, GenerateEvalDataset
from engram.evals.generation.contracts import AuthoredCase, CaseCurator, GenerationConfig, build_dataset
from engram.evals.generation.infrastructure.drafter import PydanticAiDrafter
from engram.evals.generation.infrastructure.freshness import SqlFreshnessQueries
from engram.evals.generation.infrastructure.seeds import SeedQueries
from engram.evals.generation.infrastructure.stratum_inventory import StratumInventoryQueries
from engram.evals.handlers import CheckFreshnessHandler, ComputeCoverageHandler, GenerateEvalDatasetHandler
from engram.evals.rca.contracts import Diagnosis, DraftReflection
from engram.evals.rca.infrastructure.reflector import PydanticAiReflector
from engram.evals.recommendation.contracts import StructuralDiagnosis
from engram.evals.recommendation.infrastructure.gateway import SqlKnowledgeBaseGateway
from engram.evals.recommendation.infrastructure.proposer import PydanticAiRecommendationProposer
from engram.evals.shared.domain import Archetype, DifficultyThresholds, ExpectedQa, QaCaseMeta, QaInput, RetrievalAnswer
from engram.evals.tuning.domain.objective import TuningObjective
from engram.evals.tuning.domain.proposal import ProposedPolicy
from engram.evals.tuning.infrastructure.proposer import PydanticAiProposer
from engram.evals.tuning.policy import baseline_policy
from engram.evals.tuning.service import TuningLoop, TuningResult
from engram.evals.tuning.strategy import EVAL_STRATEGY_NAMES
from engram.kb.contracts import CanonicalSection, KnowledgeBaseSchema, PageTypeSpec, load_knowledge_base_schema
from engram.retrieval.queries import FetchHandler, GraphExpandHandler, GraphPathsHandler, SearchHandler
from engram.retrieval.tools import RetrievalHandlers, build_retrieval_toolset
from engram.testing.fakes.embedding import FakeEmbedding
from engram.testing.fakes.gateway import FakeSqlGateway
from engram.testing.specs import research_spec
from pydantic_ai import FunctionToolset
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import (
    ApplicationSettings,
    AzureOpenAiSettings,
    CoverageSettings,
    EvalsSettings,
    GenerationSettings,
    KnowledgeBaseSettings,
    TuningSettings,
)
from studio.knowledge_base.wiring.evals import (
    _resolve_model,
    case_curator_provider,
    coverage_handler_provider,
    drafter_factory_provider,
    freshness_handler_provider,
    freshness_reader_factory_provider,
    generation_config_provider,
    inventory_reader_factory_provider,
    knowledge_base_gateway_factory_provider,
    proposer_factory_provider,
    recommendation_proposer_factory_provider,
    reflector_factory_provider,
    retrieval_handlers_provider,
    seed_repository_factory_provider,
    tuning_objective_provider,
)
from tests.support.http_app import write_settings_json

if TYPE_CHECKING:
    from pathlib import Path

    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.tuning.policy import RetrievalPolicy


def _handlers() -> tuple[SearchHandler, FetchHandler, GraphExpandHandler, GraphPathsHandler]:
    """The four real serve handlers over shared fakes — the concrete types the provider injects."""
    gateway = FakeSqlGateway()
    return (
        SearchHandler(gateway, FakeEmbedding()),
        FetchHandler(gateway, "research"),
        GraphExpandHandler(gateway, "research"),
        GraphPathsHandler(gateway, "research"),
    )


def _generation_settings() -> GenerationSettings:
    """A known generation block: a four-archetype distribution and non-default difficulty bands."""
    return GenerationSettings(
        target_distribution={"pinpoint": 0.5, "synthesis": 0.2, "survey": 0.2, "trace": 0.1},
        total_cases=24,
        target_per_seed=4,
        dedup_threshold=0.88,
    )


def _evals_settings() -> EvalsSettings:
    """An `EvalsSettings` carrying `_generation_settings` (the D11 boundary input)."""
    return EvalsSettings(generation=_generation_settings())


def _knowledge_base_root(tmp_path: Path) -> Path:
    """A research knowledge base on disk, the shape `KnowledgeBaseSettings` accepts."""
    knowledge_base_root = tmp_path / "knowledge_base"
    (knowledge_base_root / "_meta").mkdir(parents=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


def _settings(tmp_path: Path) -> ApplicationSettings:
    """A settings tree read from a real file under `tmp_path` — no env, no network."""
    return ApplicationSettings(json_file=write_settings_json(tmp_path / "application_settings.json"), evals=_evals_settings())


class _StubEngine:
    """Inert stand-in for the admin `AsyncEngine` — the seed-repository factory only stores it."""


class TestResolveModel:
    """`_resolve_model` is the four factories' shared tail: test-model, or an Azure name."""

    def test_test_model_spec_without_seed_is_bare_test_model(self):
        model = _resolve_model("test-model", endpoint="https://unit.invalid", test_output=None)

        assert isinstance(model, TestModel)
        assert model.custom_output_args is None

    def test_test_model_spec_with_seed_carries_custom_output_args(self):
        seed = ProposedPolicy(policy=baseline_policy(), rationale="r")

        model = _resolve_model("test-model", endpoint="https://unit.invalid", test_output=seed)

        assert isinstance(model, TestModel)
        assert model.custom_output_args == seed.model_dump()

    def test_deployment_name_resolves_an_azure_chat_model(self):
        # No network: the resolver constructs the lazily-built chat model, it does not run it.
        model = _resolve_model("gpt-5.1", endpoint="https://unit.invalid", test_output=None)

        assert not isinstance(model, TestModel)


class TestRetrievalHandlersProvider:
    def test_returns_a_group_wrapping_the_injected_handlers(self):
        search, fetch, graph_expand, find_paths = _handlers()

        group = retrieval_handlers_provider(search, fetch, graph_expand, find_paths)

        assert isinstance(group, RetrievalHandlers)
        assert group.search is search
        assert group.fetch is fetch
        assert group.graph_expand is graph_expand
        assert group.find_paths is find_paths


class TestBuildRetrievalToolset:
    """The per-run toolset build the harness runs — a plain function, not a registration."""

    def test_returns_a_function_toolset_over_the_group(self):
        search, fetch, graph_expand, find_paths = _handlers()
        group = retrieval_handlers_provider(search, fetch, graph_expand, find_paths)

        toolset = build_retrieval_toolset(group, collection="workdocs")

        assert isinstance(toolset, FunctionToolset)

    def test_toolset_exposes_the_four_retrieval_tools(self):
        search, fetch, graph_expand, find_paths = _handlers()
        group = retrieval_handlers_provider(search, fetch, graph_expand, find_paths)

        toolset = build_retrieval_toolset(group, collection="workdocs")

        assert {tool.name for tool in toolset.tools.values()} == {"search", "fetch", "graph_expand", "find_paths"}


class TestGenerationConfigProvider:
    """`generation_config_provider` maps the settings block onto the domain `GenerationConfig` (D11)."""

    def test_maps_settings_onto_the_domain_config_with_archetype_keys(self):
        config = generation_config_provider(_evals_settings())

        assert isinstance(config, GenerationConfig)
        assert config.target_distribution == {Archetype.PINPOINT: 0.5, Archetype.SYNTHESIS: 0.2, Archetype.SURVEY: 0.2, Archetype.TRACE: 0.1}
        assert all(isinstance(key, Archetype) for key in config.target_distribution)
        assert config.total_cases == 24
        assert config.target_per_seed == 4
        assert config.dedup_threshold == 0.88

    def test_maps_the_difficulty_block_onto_the_domain_thresholds(self):
        gen = _generation_settings()

        config = generation_config_provider(EvalsSettings(generation=gen))

        assert config.thresholds == DifficultyThresholds(**gen.difficulty.model_dump())

    def test_an_unknown_archetype_name_is_refused_before_the_provider_runs(self):
        with pytest.raises(ValueError, match="wibble"):
            GenerationSettings(target_distribution={"pinpoint": 0.5, "wibble": 0.5})

    def test_maps_the_coverage_salt_onto_the_domain_config(self):
        gen = GenerationSettings(coverage=CoverageSettings(salt="s3cr3t"))

        config = generation_config_provider(EvalsSettings(generation=gen))

        assert config.salt == "s3cr3t"

    def test_salt_defaults_to_none_when_unconfigured(self):
        config = generation_config_provider(_evals_settings())

        assert config.salt is None


class TestCaseCuratorProvider:
    """`case_curator_provider` binds the embedder and the config's dedup cutoff."""

    def test_returns_a_curator_using_the_configs_dedup_threshold(self):
        config = generation_config_provider(_evals_settings())

        curator = case_curator_provider(FakeEmbedding(), config)

        assert isinstance(curator, CaseCurator)
        assert curator._dedup_threshold == config.dedup_threshold


class TestSeedRepositoryFactoryProvider:
    """`seed_repository_factory_provider` opens a fresh owner-session repo per command."""

    async def test_call_returns_an_async_context_manager(self, tmp_path: Path):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = seed_repository_factory_provider(engine, load_knowledge_base_schema(_knowledge_base(tmp_path)))

        assert isinstance(factory(), AbstractAsyncContextManager)
        await engine.dispose()

    async def test_call_yields_a_seed_repository_over_a_fresh_session(self, tmp_path: Path):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = seed_repository_factory_provider(engine, load_knowledge_base_schema(_knowledge_base(tmp_path)))

        async with factory() as repo:
            assert isinstance(repo, SeedQueries)
        await engine.dispose()


class TestInventoryReaderFactoryProvider:
    """`inventory_reader_factory_provider` opens a fresh owner-session `StratumInventoryQueries` per command."""

    async def test_call_returns_an_async_context_manager(self, tmp_path: Path):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = inventory_reader_factory_provider(
            engine, load_knowledge_base_schema(_knowledge_base(tmp_path)), generation_config_provider(_evals_settings())
        )

        assert isinstance(factory(), AbstractAsyncContextManager)
        await engine.dispose()

    async def test_call_yields_a_stratum_inventory_reader_over_a_fresh_session(self, tmp_path: Path):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = inventory_reader_factory_provider(
            engine, load_knowledge_base_schema(_knowledge_base(tmp_path)), generation_config_provider(_evals_settings())
        )

        async with factory() as reader:
            assert isinstance(reader, StratumInventoryQueries)
        await engine.dispose()


class TestCoverageHandlerProvider:
    """`coverage_handler_provider` binds the reader factory, config, and datasets dir."""

    async def test_returns_a_coverage_handler_over_the_reader_factory_and_datasets_dir(self, tmp_path: Path):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        config = generation_config_provider(_evals_settings())
        make_reader = inventory_reader_factory_provider(engine, load_knowledge_base_schema(_knowledge_base(tmp_path)), config)

        handler = coverage_handler_provider(make_reader, config, EvalsSettings(datasets_dir=tmp_path / "datasets"))

        assert isinstance(handler, ComputeCoverageHandler)
        await engine.dispose()


class TestFreshnessReaderFactoryProvider:
    """`freshness_reader_factory_provider` opens a fresh owner-session `SqlFreshnessQueries` per command."""

    async def test_call_returns_an_async_context_manager(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = freshness_reader_factory_provider(engine)

        assert isinstance(factory(), AbstractAsyncContextManager)
        await engine.dispose()

    async def test_call_yields_a_sql_freshness_reader_over_a_fresh_session(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = freshness_reader_factory_provider(engine)

        async with factory() as reader:
            assert isinstance(reader, SqlFreshnessQueries)
        await engine.dispose()


class TestFreshnessHandlerProvider:
    """`freshness_handler_provider` binds the reader factory and the datasets dir."""

    async def test_returns_a_freshness_handler_over_the_reader_factory_and_datasets_dir(self, tmp_path: Path):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        make_reader = freshness_reader_factory_provider(engine)

        handler = freshness_handler_provider(make_reader, EvalsSettings(datasets_dir=tmp_path / "datasets"))

        assert isinstance(handler, CheckFreshnessHandler)
        await engine.dispose()


class TestDrafterFactoryProvider:
    """`drafter_factory_provider` resolves the runtime `--model` choice to a `CaseDrafter`."""

    def test_default_model_builds_a_pydantic_ai_drafter(self):
        factory = drafter_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        drafter = factory(None)

        assert isinstance(drafter, PydanticAiDrafter)
        # `CaseDrafter` is a plain (non-runtime-checkable) Protocol, so assert the structural
        # `draft` capability rather than `isinstance` against it.
        assert callable(drafter.draft)

    def test_test_model_builds_a_pydantic_ai_drafter(self):
        factory = drafter_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        assert isinstance(factory("test-model"), PydanticAiDrafter)

    def test_an_azure_deployment_name_builds_a_pydantic_ai_drafter(self):
        factory = drafter_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        # No network: the factory only constructs the model/drafter, it does not run it.
        assert isinstance(factory("gpt-4.1"), PydanticAiDrafter)


class TestTuningObjectiveProvider:
    """`tuning_objective_provider` returns the shipped default `TuningObjective`."""

    def test_returns_the_default_objective(self):
        objective = tuning_objective_provider(EvalsSettings())

        assert isinstance(objective, TuningObjective)
        assert objective == TuningObjective()

    def test_maps_tuning_settings_to_the_objective(self):
        evals = EvalsSettings(tuning=TuningSettings(max_rounds=7, val_split=0.25))

        objective = tuning_objective_provider(evals)

        assert objective.max_rounds == 7
        assert objective.val_split == 0.25

    def test_maps_the_decision_core_knobs_to_the_objective(self):
        evals = EvalsSettings(
            tuning=TuningSettings(
                core_size=60, tail_size=15, min_core_per_stratum=3, bootstrap_resamples=500, bootstrap_confidence=0.9
            )
        )

        objective = tuning_objective_provider(evals)

        assert objective.core_size == 60
        assert objective.tail_size == 15
        assert objective.min_core_per_stratum == 3
        assert objective.bootstrap_resamples == 500
        assert objective.bootstrap_confidence == 0.9


@dataclass(frozen=True)
class _Score:
    """A stand-in for one `EvaluationResult` — the loop's `diagnose` reads only `.value`."""

    value: float


@dataclass(frozen=True)
class _Case:
    """A stand-in for one `ReportCase` — carries the slice `diagnose` folds over plus the case name."""

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
    """A stand-in for an `EvaluationReport` — `diagnose` reads only `.cases`."""

    cases: list[_Case]


class _FlatPolicyEvaluator:
    """A `PolicyEvaluator` scoring every case of whatever dataset it is handed at one fixed `recall@5`.

    Enough measurement for a plumbing run: the loop's accept/stop arithmetic sees a flat score, so
    the run's outcome depends only on what the proposer emits.
    """

    def __init__(self, recall: float) -> None:
        self._recall = recall

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> _Report:
        return _Report(cases=[self._case(case.name or "") for case in dataset.cases])

    def _case(self, name: str) -> _Case:
        return _Case(
            name=name,
            inputs=QaInput(question=f"q-{name}"),
            metadata=QaCaseMeta(anchor_page_id=name, difficulty="easy", archetype=Archetype.PINPOINT),
            expected_output=RetrievalAnswer(answer="ref", cited_source_ids=[name]),
            output=RetrievalAnswer(answer="got", cited_source_ids=[name]),
            scores={"recall@5": _Score(self._recall), "n_tool_calls": _Score(1.0)},
            metrics={"input_tokens": 100.0},
            total_duration=0.1,
        )


def _two_case_dataset() -> EvaluationDataset:
    """A golden dataset of two pinpoint cases — the smallest input the loop will split and score."""
    records = [
        AuthoredCase(
            question=QaInput(question=f"q-{paper}"),
            expected=ExpectedQa(answer="a", expected_source_ids=(paper,)),
            meta=QaCaseMeta(anchor_page_id=paper, difficulty="easy", archetype=Archetype.PINPOINT),
        )
        for paper in ("p.a", "p.b")
    ]
    return build_dataset(records)


class TestProposerFactoryProvider:
    """`proposer_factory_provider` resolves the runtime `--model` choice to a `PolicyProposer`."""

    def test_default_model_builds_a_pydantic_ai_proposer(self):
        factory = proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        proposer = factory(None)

        assert isinstance(proposer, PydanticAiProposer)
        # `PolicyProposer` is a plain (non-runtime-checkable) Protocol, so assert the structural
        # `propose` capability rather than `isinstance` against it.
        assert callable(proposer.propose)

    def test_test_model_builds_a_pydantic_ai_proposer(self):
        factory = proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        assert isinstance(factory("test-model"), PydanticAiProposer)

    async def test_test_model_proposer_emits_a_valid_policy_when_run(self):
        # A bare TestModel schema-fills ProposedPolicy and exhausts its output retries (the nested
        # RetrievalPolicy.strategy rejects the out-of-set filler), so the factory seeds a fixed valid
        # in-set policy instead (bug 3, mirrors the recommendation-proposer seed).
        factory = proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))
        proposer = factory("test-model")
        diagnosis = Diagnosis(overall={"recall@5": 0.5}, buckets=(), worst_cases=(), mean_duration_s=0.0, mean_total_tokens=None)

        proposed = await proposer.propose(baseline_policy(), diagnosis)

        assert isinstance(proposed, ProposedPolicy)
        assert proposed.policy.strategy in EVAL_STRATEGY_NAMES

    async def test_the_seeded_proposer_drives_the_tuning_loop_to_completion(self):
        # The seeded proposer runs every round of the real loop without exhausting its output
        # retries, so the network-free path is an end-to-end plumbing run. Its proposal re-proposes
        # the incumbent, so the winner falls back to the baseline.
        baseline = baseline_policy()
        proposer = proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))("test-model")
        objective = TuningObjective(val_split=0.0, max_rounds=2, patience=10)

        result = await TuningLoop(_FlatPolicyEvaluator(0.50), proposer, objective).run(dataset=_two_case_dataset(), baseline=baseline)

        assert isinstance(result, TuningResult)
        assert result.winning_policy == baseline

    def test_an_azure_deployment_name_builds_a_pydantic_ai_proposer(self):
        factory = proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        # No network: the factory only constructs the model/proposer, it does not run it.
        assert isinstance(factory("gpt-4.1"), PydanticAiProposer)


class TestReflectorFactoryProvider:
    """`reflector_factory_provider` resolves the runtime `--model` choice to a `Reflector` or `None`."""

    def test_default_model_yields_no_reflector(self):
        # Reflection is optional: a None model spec yields no reflector, so the loop degrades to
        # empty findings (unlike the required proposer, whose None defaults to test-model).
        factory = reflector_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        assert factory(None) is None

    def test_test_model_builds_a_pydantic_ai_reflector(self):
        factory = reflector_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        assert isinstance(factory("test-model"), PydanticAiReflector)

    async def test_test_model_reflector_emits_a_draft_when_run(self):
        # A network-free plumbing run: the seeded TestModel emits the fixed empty draft, so the gate
        # has nothing to keep.
        factory = reflector_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))
        reflector = factory("test-model")
        assert isinstance(reflector, PydanticAiReflector)
        diagnosis = Diagnosis(overall={"recall@5": 0.5}, buckets=(), worst_cases=(), mean_duration_s=0.0, mean_total_tokens=None)

        draft = await reflector.reflect(diagnosis)

        assert isinstance(draft, DraftReflection)
        assert draft.findings == ()

    def test_an_azure_deployment_name_builds_a_pydantic_ai_reflector(self):
        factory = reflector_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        # No network: the factory only constructs the model/reflector, it does not run it.
        assert isinstance(factory("gpt-4.1"), PydanticAiReflector)


class TestRecommendationProposerFactoryProvider:
    """`recommendation_proposer_factory_provider` resolves the runtime `--model` choice to a `RecommendationProposer`."""

    def test_default_model_builds_a_pydantic_ai_recommendation_proposer(self):
        factory = recommendation_proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        proposer = factory(None)

        assert isinstance(proposer, PydanticAiRecommendationProposer)
        # `RecommendationProposer` is a plain (non-runtime-checkable) Protocol, so assert the structural
        # `propose` capability rather than `isinstance` against it.
        assert callable(proposer.propose)

    def test_test_model_builds_a_pydantic_ai_recommendation_proposer(self):
        factory = recommendation_proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        assert isinstance(factory("test-model"), PydanticAiRecommendationProposer)

    async def test_test_model_proposer_emits_an_empty_draft_when_run(self):
        # A bare TestModel schema-fills DraftRecommendation and exhausts its output retries (the SchemaEditOp
        # discriminated union rejects the filler), so the factory pins a fixed empty draft instead.
        factory = recommendation_proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))
        proposer = factory("test-model")
        base = Diagnosis(overall={"recall@5": 0.5}, buckets=(), worst_cases=(), mean_duration_s=0.0, mean_total_tokens=None)
        diagnosis = StructuralDiagnosis(base=base, evidence_grade="raw-baseline", section_splits=(), missing_edges=(), over_expansion=None)
        spec = KnowledgeBaseSchema(
            slug="test", page_types=(PageTypeSpec(page_type="paper", id_prefix="p", canonical_sections=(CanonicalSection(slug="summary"),)),), relations=()
        )

        draft = await proposer.propose(diagnosis, spec)

        assert draft.proposals == ()
        assert draft.findings == ()

    def test_an_azure_deployment_name_builds_a_pydantic_ai_recommendation_proposer(self):
        factory = recommendation_proposer_factory_provider(AzureOpenAiSettings(endpoint="https://example.openai.azure.com"))

        # No network: the factory only constructs the model/proposer, it does not run it.
        assert isinstance(factory("gpt-4.1"), PydanticAiRecommendationProposer)


class TestKnowledgeBaseGatewayFactoryProvider:
    """`knowledge_base_gateway_factory_provider` opens a fresh owner-session `SqlKnowledgeBaseGateway` per command."""

    async def test_call_returns_an_async_context_manager(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = knowledge_base_gateway_factory_provider(engine)

        assert isinstance(factory(), AbstractAsyncContextManager)
        await engine.dispose()

    async def test_call_yields_a_knowledge_base_gateway_over_a_fresh_session(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = knowledge_base_gateway_factory_provider(engine)

        async with factory() as gateway:
            assert isinstance(gateway, SqlKnowledgeBaseGateway)
        await engine.dispose()


def _knowledge_base(tmp_path: Path) -> Path:
    """A knowledge base root carrying the research template's own knowledge-base-schema.json."""
    knowledge_base_root = tmp_path / "knowledge_base"
    (knowledge_base_root / "_meta").mkdir(parents=True)
    (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
    return knowledge_base_root


class TestGeneratePathContainerWiring:
    """The real wireup graph: `create_container` resolves the whole generate path (the headline)."""

    async def test_resolving_the_handler_routes_the_generate_command_on_the_shared_bus(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        with (
            container.override.injectable(target=AsyncEngine, new=_StubEngine()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            handler = await container.get(GenerateEvalDatasetHandler)
            bus = await container.get(MessageBus)

        assert isinstance(handler, GenerateEvalDatasetHandler)
        assert bus._command_handlers[GenerateEvalDataset] is handler

    async def test_handler_is_a_singleton_so_the_route_registers_once(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        with (
            container.override.injectable(target=AsyncEngine, new=_StubEngine()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            first = await container.get(GenerateEvalDatasetHandler)
            second = await container.get(GenerateEvalDatasetHandler)

        assert first is second

    async def test_resolving_the_coverage_handler_routes_the_command_on_the_shared_bus(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        with (
            container.override.injectable(target=AsyncEngine, new=_StubEngine()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            handler = await container.get(ComputeCoverageHandler)
            bus = await container.get(MessageBus)

        assert isinstance(handler, ComputeCoverageHandler)
        assert bus._command_handlers[ComputeCoverage] is handler

    async def test_resolving_the_freshness_handler_routes_the_command_on_the_shared_bus(self, tmp_path: Path):
        container = create_container(_settings(tmp_path), knowledge_base=KnowledgeBaseSettings(path=_knowledge_base_root(tmp_path)))

        with (
            container.override.injectable(target=AsyncEngine, new=_StubEngine()),
            container.override.injectable(target=BatchEmbedder, new=FakeEmbedding()),
        ):
            handler = await container.get(CheckFreshnessHandler)
            bus = await container.get(MessageBus)

        assert isinstance(handler, CheckFreshnessHandler)
        assert bus._command_handlers[CheckFreshness] is handler
