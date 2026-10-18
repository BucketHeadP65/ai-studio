"""What the evals slice registers: the retrieval handlers under test and the collaborators of every evals handler.

The retrieval handlers are the serve slice's own query handlers, so an evaluation measures the real retrieval path. The SQL tools an
evaluation run gives the agent under test are built the way the MCP server builds its own. Each per-command
factory opens one fresh owner session per command over the admin engine, so no singleton holds a session. A model choice of
`"test-model"`, or none where a model is required, resolves to a network-free test model, and any other choice names an Azure
deployment that is built without touching the network.
"""

# Moving an injected type under TYPE_CHECKING breaks registration, because wireup reads these annotations at runtime.
from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai import create_azure_model
from core.ai.embedding import BatchEmbedder, BatchQueryEmbedder
from core.db import SessionScopedFactory
from core.resolver import Resolver
from engram.evals.commands import (
    DrafterFactory,
    FreshnessReaderFactory,
    InventoryReaderFactory,
    KnowledgeBaseGatewayFactory,
    PolicyEvaluatorFactory,
    ProposerFactory,
    RecommendationProposerFactory,
    RunRepositoryFactory,
    SeedRepositoryFactory,
)
from engram.evals.generation.contracts import CaseCurator, GenerationConfig
from engram.evals.generation.infrastructure.drafter import PydanticAiDrafter
from engram.evals.generation.infrastructure.freshness import SqlFreshnessQueries
from engram.evals.generation.infrastructure.seeds import SeedQueries
from engram.evals.generation.infrastructure.stratum_inventory import StratumInventoryQueries
from engram.evals.handlers import (
    CheckFreshnessHandler,
    CheckGeneratorHandler,
    ComputeCoverageHandler,
    FreezeDatasetHandler,
    GenerateEvalDatasetHandler,
    RecommendStructureHandler,
    RollbackPolicyHandler,
    ScoreDatasetHandler,
    TuneRetrievalHandler,
    VerifyRecommendationHandler,
)
from engram.evals.harness.contracts import EvaluationRunConfig, run_eval_on_dataset
from engram.evals.rca.contracts import DraftReflection, ReflectorFactory
from engram.evals.rca.infrastructure.reflector import PydanticAiReflector
from engram.evals.recommendation.contracts import DraftRecommendation
from engram.evals.recommendation.infrastructure.gateway import SqlKnowledgeBaseGateway
from engram.evals.recommendation.infrastructure.proposer import PydanticAiRecommendationProposer
from engram.evals.scoring.contracts import HitSources
from engram.evals.scoring.infrastructure.sources import AdminHitSources
from engram.evals.shared.contracts import (
    Archetype,
    DifficultyThresholds,
    FetchHandler,
    GraphExpandHandler,
    GraphPathsHandler,
    RetrievalHandlers,
    SearchHandler,
)
from engram.evals.shared.infrastructure.postgres import PostgresEvaluationRuns
from engram.evals.tuning.contracts import ProposedPolicy, TuningObjective, baseline_policy
from engram.evals.tuning.infrastructure.proposer import PydanticAiProposer
from engram.kb.contracts import KnowledgeBaseSchema
from engram.retrieval.contracts import DescribeSchemaHandler, SqlKnowledgeBase
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import AsyncEngine
from wireup import injectable

from studio.knowledge_base.settings import AzureOpenAiSettings, EvalsSettings, KnowledgeBaseSettings
from studio.knowledge_base.wiring.contracts import AdminSqlGateway, EvaluationSqlTools, ReaderSqlGateway

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from core.ai.retrieval.contracts import SqlResult
    from engram.evals.generation.contracts import CaseDrafter, EvaluationDataset
    from engram.evals.rca.contracts import Reflector, ReportLike
    from engram.evals.recommendation.contracts import RecommendationProposer
    from engram.evals.tuning.contracts import PolicyEvaluator, PolicyProposer, RetrievalPolicy
    from engram.retrieval.contracts import Search
    from pydantic import BaseModel
    from pydantic_ai.models import Model

# The model choice that resolves to a network-free test model, and the default where a model is required.
_TEST_MODEL = "test-model"


def _resolve_model(spec: str, *, endpoint: str, test_output: BaseModel | None) -> Model:
    """Resolve a model choice to a test model seeded with `test_output`, or to an Azure deployment built without the network."""
    if spec == _TEST_MODEL:
        return TestModel() if test_output is None else TestModel(custom_output_args=test_output.model_dump())
    return create_azure_model(spec, endpoint=endpoint)


@injectable
def retrieval_handlers_provider(
    search: SearchHandler, fetch: FetchHandler, graph_expand: GraphExpandHandler, find_paths: GraphPathsHandler
) -> RetrievalHandlers:
    """Provide the retrieval handlers the evaluation toolset wraps.

    They are the serve slice's own query handlers, the same instances the MCP tools use, so an evaluation measures the real
    retrieval path.

    Args:
        search: The serve slice's search handler.
        fetch: The serve slice's fetch handler.
        graph_expand: The serve slice's graph-expand handler.
        find_paths: The serve slice's graph-paths handler.

    Returns:
        The group of the four handlers.
    """
    return RetrievalHandlers(search=search, fetch=fetch, graph_expand=graph_expand, find_paths=find_paths)


# -- the generate path -------------------------------------------------------


@injectable
def generation_config_provider(evals: EvalsSettings) -> GenerationConfig:
    """Provide the generation config, read from the `generation` block of the evals settings.

    The target distribution is keyed by archetype. `GenerationSettings` has already refused any name that is not one.
    The config also carries the case budgets, the difficulty bands and the coverage sampling salt.

    Args:
        evals: The evals settings whose `generation` block holds the generation settings.

    Returns:
        The generation config the generate handler runs with.
    """
    generation = evals.generation
    return GenerationConfig(
        target_distribution={Archetype(name): weight for name, weight in generation.target_distribution.items()},
        total_cases=generation.total_cases,
        target_per_seed=generation.target_per_seed,
        dedup_threshold=generation.dedup_threshold,
        thresholds=DifficultyThresholds(**generation.difficulty.model_dump()),
        salt=generation.coverage.salt,
        min_pool_warn=generation.coverage.min_pool_warn,
    )


@injectable
def case_curator_provider(embedder: BatchEmbedder, config: GenerationConfig) -> CaseCurator:
    """Provide the case curator, which drops near-duplicate questions using the index's own embedding model.

    Args:
        embedder: The embedding model the index uses.
        config: The generation config that holds the near-duplicate cutoff.

    Returns:
        The case curator the generate handler dedups and stratifies its cases with.
    """
    return CaseCurator(embedder, dedup_threshold=config.dedup_threshold)


@injectable
def seed_repository_factory_provider(engine: AsyncEngine, schema: KnowledgeBaseSchema) -> SeedRepositoryFactory:
    """Provide the factory that opens one seed reader per generate command, over a fresh owner session.

    The reader only issues `SELECT` statements.

    Args:
        engine: The admin engine, the role that can read `kb.*`.
        schema: The knowledge base's schema, passed to each reader.

    Returns:
        The factory the generate handler opens one seed reader per command from.
    """
    return SessionScopedFactory(engine, lambda session: SeedQueries(session, schema))


@injectable
def inventory_reader_factory_provider(engine: AsyncEngine, schema: KnowledgeBaseSchema, config: GenerationConfig) -> InventoryReaderFactory:
    """Provide the factory that opens one coverage reader per command, over a fresh owner session.

    Each reader counts pages per stratum over the relations the schema declares, with the same difficulty bands the seeds are
    drawn with.

    Args:
        engine: The admin engine, the role that can read `kb.*`.
        schema: The knowledge base's schema, passed to each reader.
        config: The generation config that holds the difficulty bands.

    Returns:
        The factory the generate and coverage handlers open one coverage reader per command from.
    """
    return SessionScopedFactory(engine, lambda session: StratumInventoryQueries(session, schema, config.thresholds))


class _PydanticAiDrafterFactory:
    """Builds a case drafter for a model choice, over the network-free test model when none is given."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def __call__(self, model: str | None) -> CaseDrafter:
        """Build a case drafter for `model`, over the network-free test model when `model` is `None`."""
        return PydanticAiDrafter(_resolve_model(model or _TEST_MODEL, endpoint=self._endpoint, test_output=None))


@injectable
def drafter_factory_provider(azure_openai: AzureOpenAiSettings) -> DrafterFactory:
    """Provide the factory that builds a case drafter for each command's `--model` choice.

    Args:
        azure_openai: The Azure OpenAI resource whose `endpoint` an Azure drafter is built on.

    Returns:
        The factory the generate handler builds each command's drafter from.
    """
    return _PydanticAiDrafterFactory(azure_openai.endpoint)


@injectable
def generate_handler_provider(
    make_repo: SeedRepositoryFactory,
    curator: CaseCurator,
    config: GenerationConfig,
    make_drafter: DrafterFactory,
    make_reader: InventoryReaderFactory,
    make_freshness_reader: FreshnessReaderFactory,
) -> GenerateEvalDatasetHandler:
    """Provide the handler that generates an evaluation dataset.

    Args:
        make_repo: Opens one seed reader per command.
        curator: Drops near-duplicate cases and spreads the rest across strata.
        config: The generation config.
        make_drafter: Builds the drafter for each command's model choice.
        make_reader: Opens one coverage reader per command.
        make_freshness_reader: Opens the freshness reader a top-up checks the current dataset with.

    Returns:
        The generate handler.
    """
    return GenerateEvalDatasetHandler(make_repo, curator, config, make_drafter, make_reader, make_freshness_reader)


@injectable
def coverage_handler_provider(
    make_reader: InventoryReaderFactory, config: GenerationConfig, evals: EvalsSettings
) -> ComputeCoverageHandler:
    """Provide the handler that computes a dataset's coverage of the knowledge base.

    Args:
        make_reader: Opens one coverage reader per command.
        config: The generation config the target allocation is computed from.
        evals: The evals settings whose `datasets_dir` holds the dataset versions.

    Returns:
        The coverage handler.
    """
    return ComputeCoverageHandler(make_reader, config, evals.datasets_dir)


# -- freshness path ----------------------------------------------------------


@injectable
def freshness_reader_factory_provider(engine: AsyncEngine) -> FreshnessReaderFactory:
    """Provide the factory that opens one freshness reader per command, over a fresh owner session.

    Args:
        engine: The admin engine, the only role that can read the `kb.*` tables a dataset is compared against.

    Returns:
        The factory the freshness and freeze handlers open one freshness reader per command from.
    """
    return SessionScopedFactory(engine, SqlFreshnessQueries)


@injectable
def freshness_handler_provider(make_reader: FreshnessReaderFactory, evals: EvalsSettings) -> CheckFreshnessHandler:
    """Provide the handler that checks a dataset against the current state of the knowledge base.

    Args:
        make_reader: Opens one freshness reader per command.
        evals: The evals settings whose `datasets_dir` holds the dataset versions.

    Returns:
        The freshness handler.
    """
    return CheckFreshnessHandler(make_reader, evals.datasets_dir)


class _ResolvedSearch:
    """The search handler, resolved on the first search, so building the bus opens no read engine."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def __call__(self, query: Search) -> SqlResult:
        search = await self._resolver.get(SearchHandler)
        return await search(query)


class _ResolvedHitSources:
    """The admin page-to-source lookup, resolved on the first lookup."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    async def page_ids(self, collection: str, ids: Sequence[str]) -> Collection[str]:
        admin = await self._resolver.get(AdminSqlGateway)
        return await AdminHitSources(admin).page_ids(collection, ids)

    async def source_ids(self, collection: str, page_ids: Sequence[str]) -> Mapping[str, str]:
        admin = await self._resolver.get(AdminSqlGateway)
        return await AdminHitSources(admin).source_ids(collection, page_ids)


@injectable
def hit_sources_provider(resolver: Resolver) -> HitSources:
    """Provide the page-to-source lookup an evaluation run maps each case's citations through.

    It reads `kb.pages` through the admin gateway, which is resolved on the first lookup, not when the container is built.

    Args:
        resolver: Resolves the admin gateway on the first lookup.

    Returns:
        The lookup.
    """
    return _ResolvedHitSources(resolver)


@injectable
def evaluation_sql_tools_provider(
    reader: ReaderSqlGateway, describe_handler: DescribeSchemaHandler, embedder: BatchEmbedder, schema: KnowledgeBaseSchema
) -> EvaluationSqlTools:
    """Provide the SQL tools an evaluation run with the SQL switch gives the agent under test, under their own type.

    They read through the reader gateway bound to the knowledge base's own collection, describe the `api` surface through the
    schema handler and embed a `vector_query` with the query embedder, as the MCP server's do. A refused statement goes back to
    the model as a retry, up to the read tools' retry budget in a row. The knowledge base's purpose is the domain sentence the
    agent reads.

    Args:
        reader: The read-only gateway, bound here to the knowledge base's collection.
        describe_handler: Renders the `api` surface's schema.
        embedder: The embedder a `vector_query` text is embedded with.
        schema: The knowledge base's schema, whose collection and purpose the tools take.

    Returns:
        The SQL tools, as a capability inside their own type.
    """
    collection = schema.retrieval.collection
    capability = SqlKnowledgeBase[object](
        gateway=reader.for_collection(collection),
        describe_handler=describe_handler,
        domain=schema.purpose or f"The knowledge base {collection!r}.",
        embed_query=BatchQueryEmbedder(embedder),
        retry_refusals=True,
    )
    return EvaluationSqlTools(capability=capability)


@injectable
def score_handler_provider(resolver: Resolver) -> ScoreDatasetHandler:
    """Provide the handler that scores a dataset with search alone, mapping page hits to sources through the admin gateway.

    Args:
        resolver: Resolves the search handler and the admin gateway when a dataset is scored, not when the bus is built.

    Returns:
        The score handler.
    """
    return ScoreDatasetHandler(_ResolvedSearch(resolver), _ResolvedHitSources(resolver))


@injectable
def check_generator_handler_provider(resolver: Resolver) -> CheckGeneratorHandler:
    """Provide the handler that checks a generated dataset against a human-judged one under every search setup.

    Args:
        resolver: Resolves the search handler and the admin gateway when a check runs, not when the bus is built.

    Returns:
        The generator check handler.
    """
    return CheckGeneratorHandler(_ResolvedSearch(resolver), _ResolvedHitSources(resolver))


# -- freeze / rollback path ---------------------------------------------------


@injectable
def freeze_handler_provider(make_reader: FreshnessReaderFactory, evals: EvalsSettings) -> FreezeDatasetHandler:
    """Provide the handler that freezes a held-out split of a dataset.

    Args:
        make_reader: Opens the freshness reader the freeze records its staleness with.
        evals: The evals settings whose `datasets_dir` holds the dataset versions and whose `generation` block sizes the frozen split.

    Returns:
        The freeze handler.
    """
    return FreezeDatasetHandler(
        make_reader,
        evals.datasets_dir,
        frozen_fraction=evals.generation.frozen_fraction,
        frozen_min_per_stratum=evals.generation.frozen_min_per_stratum,
    )


@injectable
def rollback_handler_provider() -> RollbackPolicyHandler:
    """Provide the handler that rolls the active retrieval policy back, which needs no collaborators.

    Returns:
        The rollback handler.
    """
    return RollbackPolicyHandler()


# -- the tune path ------------------------------------------------------


@injectable
def tuning_objective_provider(evals: EvalsSettings) -> TuningObjective:
    """Provide the tuning objective, read from the `tuning` block of the evals settings.

    The objective maximizes `primary_metric` under the `cost_metric` limit, within the round cap, the patience and the
    validation split. A command's own round and token limits still override it per run. `policies_dir` is not part of it.

    Args:
        evals: The evals settings whose `tuning` block holds the tuning settings.

    Returns:
        The tuning objective the tune handler runs with.
    """
    tuning = evals.tuning
    return TuningObjective(
        primary_metric=tuning.primary_metric,
        cost_metric=tuning.cost_metric,
        cost_budget_pct=tuning.cost_budget_pct,
        min_gain=tuning.min_gain,
        max_rounds=tuning.max_rounds,
        patience=tuning.patience,
        token_budget=tuning.token_budget,
        val_split=tuning.val_split,
        core_size=tuning.core_size,
        tail_size=tuning.tail_size,
        min_core_per_stratum=tuning.min_core_per_stratum,
        bootstrap_resamples=tuning.bootstrap_resamples,
        bootstrap_confidence=tuning.bootstrap_confidence,
        apply_staleness_max=tuning.apply_staleness_max,
    )


# TestModel cannot fill this output type, so without this seed the test proposer exhausts its retries.
def _test_proposed_policy() -> ProposedPolicy:
    """The shipped baseline policy with a fixed rationale, the answer of the test proposer."""
    return ProposedPolicy(policy=baseline_policy(), rationale="test-model proposal, incumbent unchanged")


class _PydanticAiProposerFactory:
    """Builds a policy proposer for a model choice, over the network-free test model when none is given."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def __call__(self, model: str | None) -> PolicyProposer:
        """Build a policy proposer for `model`, over the network-free test model when `model` is `None`."""
        resolved = _resolve_model(model or _TEST_MODEL, endpoint=self._endpoint, test_output=_test_proposed_policy())
        return PydanticAiProposer(resolved)


@injectable
def proposer_factory_provider(azure_openai: AzureOpenAiSettings) -> ProposerFactory:
    """Provide the factory that builds a policy proposer for each command's `--model` choice.

    Args:
        azure_openai: The Azure OpenAI resource whose `endpoint` an Azure proposer is built on.

    Returns:
        The factory the tune handler builds each command's proposer from.
    """
    return _PydanticAiProposerFactory(azure_openai.endpoint)


# -- reflection path ---------------------------------------------------------


def _test_draft_reflection() -> DraftReflection:
    """An empty reflection with no findings, the answer of the test reflector."""
    return DraftReflection(findings=())


class _PydanticAiReflectorFactory:
    """Builds a reflector for a model choice, or none when no model is given, because reflection is optional."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def __call__(self, model: str | None) -> Reflector | None:
        """Build a reflector for `model`, or `None` when `model` is `None` and reflection is not configured."""
        if model is None:
            return None
        resolved = _resolve_model(model, endpoint=self._endpoint, test_output=_test_draft_reflection())
        return PydanticAiReflector(resolved)


@injectable
def reflector_factory_provider(azure_openai: AzureOpenAiSettings) -> ReflectorFactory:
    """Provide the factory that builds a reflector for each command's `--model` choice, or none.

    Reflection is optional. With no model choice there is no reflector, and the tuning run goes on with no findings.

    Args:
        azure_openai: The Azure OpenAI resource whose `endpoint` an Azure reflector is built on.

    Returns:
        The factory the tune handler builds each command's reflector from.
    """
    return _PydanticAiReflectorFactory(azure_openai.endpoint)


class _RunEvalPolicyEvaluator:
    """Scores a policy by evaluating a dataset with the policy's instructions, strategy and tool descriptions."""

    def __init__(self, resolver: Resolver, *, model: str | None) -> None:
        self._resolver = resolver
        self._model = model

    async def evaluate(self, policy: RetrievalPolicy, *, dataset: EvaluationDataset) -> ReportLike:
        """Score `policy` over `dataset` and return the evaluation report."""
        config = EvaluationRunConfig(
            model_spec=self._model or _TEST_MODEL,
            instructions=policy.instructions,
            retrieval_strategy=policy.strategy,
            tool_descriptions=policy.tool_descriptions,
        )
        return await run_eval_on_dataset(self._resolver, config=config, dataset=dataset)


class _RunEvalPolicyEvaluatorFactory:
    """Builds, for a model choice, an evaluator that resolves the model and the retrieval handlers from the container each round."""

    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    def __call__(self, model: str | None) -> PolicyEvaluator:
        """Build a policy evaluator bound to `model`, over the network-free test model when `model` is `None`."""
        return _RunEvalPolicyEvaluator(self._resolver, model=model)


@injectable
def run_repository_factory_provider(engine: AsyncEngine) -> RunRepositoryFactory:
    """Provide the factory that opens one evaluation run store per command, over a fresh owner session.

    Args:
        engine: The admin engine, the only role that can write the `kb.evaluation_runs` tables.

    Returns:
        The factory the tune, recommend and verify handlers open one run store per command from.
    """
    return SessionScopedFactory(engine, PostgresEvaluationRuns)


@injectable
def tune_handler_provider(
    objective: TuningObjective,
    make_proposer: ProposerFactory,
    make_evaluator: PolicyEvaluatorFactory,
    make_run_repo: RunRepositoryFactory,
    make_freshness_reader: FreshnessReaderFactory,
    make_reflector: ReflectorFactory,
) -> TuneRetrievalHandler:
    """Provide the handler that tunes the retrieval policy.

    Args:
        objective: The tuning objective, including the `apply_staleness_max` threshold.
        make_proposer: Builds the policy proposer for each command's model choice.
        make_evaluator: Builds the policy evaluator for each command's model choice.
        make_run_repo: Opens one evaluation run store per command.
        make_freshness_reader: Opens the freshness reader whose check warns and never refuses.
        make_reflector: Builds the optional reflector for each command's model choice.

    Returns:
        The tune handler.
    """
    return TuneRetrievalHandler(
        objective, make_proposer, make_evaluator, make_run_repo, make_freshness_reader=make_freshness_reader, make_reflector=make_reflector
    )


# -- the recommendation path --------------------------------------------------


class _PydanticAiRecommendationProposerFactory:
    """Builds a recommendation proposer for a model choice, over the network-free test model when none is given."""

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def __call__(self, model: str | None) -> RecommendationProposer:
        """Build a recommendation proposer for `model`, over the network-free test model when `model` is `None`."""
        # TestModel cannot fill this output type, so without this seed the test proposer exhausts its retries.
        empty_draft = DraftRecommendation(proposals=(), findings=(), rationale="test-model draft, no proposals")
        resolved = _resolve_model(model or _TEST_MODEL, endpoint=self._endpoint, test_output=empty_draft)
        return PydanticAiRecommendationProposer(resolved)


@injectable
def recommendation_proposer_factory_provider(azure_openai: AzureOpenAiSettings) -> RecommendationProposerFactory:
    """Provide the factory that builds a recommendation proposer for each command's `--model` choice.

    Args:
        azure_openai: The Azure OpenAI resource whose `endpoint` an Azure proposer is built on.

    Returns:
        The factory the recommend handler builds each command's proposer from.
    """
    return _PydanticAiRecommendationProposerFactory(azure_openai.endpoint)


@injectable
def knowledge_base_gateway_factory_provider(engine: AsyncEngine) -> KnowledgeBaseGatewayFactory:
    """Provide the factory that opens one knowledge base gateway per command, over a fresh owner session.

    Args:
        engine: The admin engine, the only role that can read the `kb.*` tables.

    Returns:
        The factory the recommend and verify handlers open one gateway per command from.
    """
    return SessionScopedFactory(engine, SqlKnowledgeBaseGateway)


@injectable
def recommend_handler_provider(
    make_evaluator: PolicyEvaluatorFactory,
    make_gateway: KnowledgeBaseGatewayFactory,
    make_proposer: RecommendationProposerFactory,
    make_run_repo: RunRepositoryFactory,
    evals: EvalsSettings,
    knowledge_base: KnowledgeBaseSettings,
) -> RecommendStructureHandler:
    """Provide the handler that recommends changes to the knowledge base's structure.

    Args:
        make_evaluator: Builds the policy evaluator the baseline evaluation runs with.
        make_gateway: Opens one knowledge base gateway per command.
        make_proposer: Builds the recommendation proposer for each command's model choice.
        make_run_repo: Opens one evaluation run store per command.
        evals: The evals settings whose `recommendation` block holds the recommendation settings.
        knowledge_base: The knowledge base whose active policy and schema the handler reads.

    Returns:
        The recommend handler.
    """
    return RecommendStructureHandler(make_evaluator, make_gateway, make_proposer, make_run_repo, evals.recommendation, knowledge_base.path)


@injectable
def verify_handler_provider(
    make_evaluator: PolicyEvaluatorFactory,
    make_gateway: KnowledgeBaseGatewayFactory,
    make_run_repo: RunRepositoryFactory,
    knowledge_base: KnowledgeBaseSettings,
    make_freshness_reader: FreshnessReaderFactory,
) -> VerifyRecommendationHandler:
    """Provide the handler that verifies an applied recommendation by evaluating the dataset again.

    Args:
        make_evaluator: Builds the policy evaluator the second evaluation runs with.
        make_gateway: Opens the knowledge base gateway that reads page existence, the current stamp and the changed pages.
        make_run_repo: Opens the evaluation run store the run is saved in.
        knowledge_base: The knowledge base whose policy the second evaluation runs with.
        make_freshness_reader: Opens the freshness reader whose `dataset_staleness` stamp warns and never refuses.

    Returns:
        The verify handler.
    """
    return VerifyRecommendationHandler(make_evaluator, make_gateway, make_run_repo, knowledge_base.path, make_freshness_reader=make_freshness_reader)


REGISTRATIONS: list[object] = [
    retrieval_handlers_provider,
    generation_config_provider,
    case_curator_provider,
    seed_repository_factory_provider,
    inventory_reader_factory_provider,
    drafter_factory_provider,
    generate_handler_provider,
    coverage_handler_provider,
    freshness_reader_factory_provider,
    freshness_handler_provider,
    hit_sources_provider,
    evaluation_sql_tools_provider,
    score_handler_provider,
    check_generator_handler_provider,
    freeze_handler_provider,
    rollback_handler_provider,
    tuning_objective_provider,
    proposer_factory_provider,
    reflector_factory_provider,
    injectable(_RunEvalPolicyEvaluatorFactory, as_type=PolicyEvaluatorFactory),
    run_repository_factory_provider,
    tune_handler_provider,
    recommendation_proposer_factory_provider,
    knowledge_base_gateway_factory_provider,
    recommend_handler_provider,
    verify_handler_provider,
]
"""Everything this slice puts in the container, each under the type its consumers declare."""
