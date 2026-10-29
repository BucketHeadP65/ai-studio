"""Policy shapes engram's library factories accept.

Connection and credential values stay with the host application, in the infrastructure section `core.settings`
defines. These classes are the rest of the typed contract a host fills in: the enums the retrieval registries key
on and the pydantic models the index, retrieval, sources and evals factories take.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from core.ai.decision_models import RequestShape
from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.settings import DocumentIntelligenceSettings
    from pydantic import JsonValue


class DbDialect(StrEnum):
    """The BM25 search extension the knowledge base's SQL is written for, named after the Postgres extension."""

    PG_SEARCH = "pg_search"
    PG_TEXTSEARCH = "pg_textsearch"


class GraphBackend(StrEnum):
    """The backends the knowledge base's link-graph traversal can target.

    `CTE` reads the edge relation with plain SQL, one hop at a time. `AGE` matches one hop at a time in an
    Apache AGE / openCypher graph, and the studio selects it by default. The `retrieval.graph_backend` setting, or alembic's
    `APP_RETRIEVAL__GRAPH_BACKEND` override, selects one.
    """

    CTE = "cte"
    AGE = "age"


def _check_knowledge_base_shape(path: Path) -> None:
    """Fail fast unless `path` is a directory holding a `knowledge-base-schema.json`."""
    if not path.is_dir():
        msg = f"knowledge_base.path is not an existing directory: {path}"
        raise ValueError(msg)
    if not (path / "knowledge-base-schema.json").is_file():
        msg = f"knowledge_base.path {path} holds no knowledge-base-schema.json file"
        raise ValueError(msg)


type PassageText = Literal["snippet", "chunk"]
"""What a re-rank judges for each hit: the snippet the hit carries, or the hit's whole text cut to fit."""


class RerankSettings(BaseModel):
    """How search hits are re-ranked by a decision model: whether, by which model, on which text, in how many requests."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    model: str = "jev-latest"
    """The name TypeSafe serves the decision model under, such as `jev-latest` or a versioned id."""
    passage: PassageText = "snippet"
    """What the model reads for each hit: the snippet the hit carries, or the hit's whole text cut to fit."""
    requests: RequestShape = "per_hit"
    """One request per hit, or one request for every hit."""
    deadline_seconds: float = Field(default=3.0, gt=0, le=60)
    """How long a re-rank may take, counted from when the search returns, before the hits come back in the search's own order."""
    max_input_tokens: int = Field(default=32_000, ge=1_000)
    """The most tokens one request may carry. A chunk is cut to fit it."""
    max_concurrent_requests: int = Field(default=8, ge=1)
    """How many re-rank requests may run at once in one process. Requests past it wait their turn within the deadline."""


class _RetrievalBlock(BaseModel):
    rerank: RerankSettings = Field(default_factory=RerankSettings)


class _RerankFile(BaseModel):
    retrieval: _RetrievalBlock = Field(default_factory=_RetrievalBlock)


def read_rerank_settings(path: Path) -> RerankSettings:
    """The `retrieval.rerank` block of the settings file at `path`, the defaults when the block is absent.

    Args:
        path: The settings file.

    Returns:
        The re-rank settings.

    Raises:
        OSError: There is no readable file at `path`.
        ValueError: The file is not JSON, or its `retrieval.rerank` block is invalid.
    """
    return _RerankFile.model_validate_json(path.read_text(encoding="utf-8")).retrieval.rerank


class KnowledgeBaseSettings(BaseModel):
    """The knowledge base location, the domain source of truth for indexing.

    `path` is the knowledge base root (required, shape-validated). Mirror notes written by
    the sync slice live in the `sources_subdir` folder (`Sources/` by default)
    under the root, soft-deleted mirrors go in its `_archive`, both exposed as
    derived absolute paths.
    """

    path: Path
    sources_subdir: str = "Sources"

    @model_validator(mode="after")
    def _check_shape(self) -> KnowledgeBaseSettings:
        _check_knowledge_base_shape(self.path)
        return self

    @property
    def sources_dir(self) -> Path:
        """Absolute path to the mirrors folder under the knowledge base root."""
        return self.path / self.sources_subdir

    @property
    def archive_dir(self) -> Path:
        """Absolute path to the soft-delete `_archive` folder under the mirrors folder."""
        return self.path / self.sources_subdir / "_archive"


class KnowledgeBaseEntry(BaseModel):
    """A named registry member: a knowledge base root plus a human description.

    `path` carries the same shape validation as `KnowledgeBaseSettings.path` (a `knowledge-base-schema.json` at its
    root), so a registered knowledge base always constructs as the active `KnowledgeBaseSettings`. Frozen: a registry is a mapping of these, replaced whole on edit, never changed in place.
    """

    model_config = ConfigDict(frozen=True)

    path: Path
    description: str = ""

    @model_validator(mode="after")
    def _check_shape(self) -> KnowledgeBaseEntry:
        _check_knowledge_base_shape(self.path)
        return self


class IngestionSettings(BaseModel):
    """Index-stage chunking, embedding and disclosure-summary policy, including which deployments embed, summarise and plan chunks.

    Every deployment runs on the Azure OpenAI resource the infrastructure section names. Indexing and query-time
    embedding read the same `embedding_deployment` and `embedding_dimensions`, so the two must change together.
    An empty `embedding_deployment` builds, and the embedding path refuses it at use time. The chunk target must not
    exceed the chunk maximum. An empty `chunk_planner_model`, or no Azure OpenAI endpoint, indexes with no chunk planner.
    The index fingerprint records the chunk sizes, the chunk planner and its ceiling, so a change makes the next index a full one.
    """

    embedding_deployment: str = ""
    """The Azure OpenAI deployment that embeds pages at index time and queries at search time."""
    embedding_dimensions: int = Field(default=1024, gt=0)
    embedding_batch_size: int = Field(default=64, gt=0)
    """The most texts one embedding request holds."""
    embedding_batch_tokens: int = Field(default=50_000, gt=0)
    """The most tokens one embedding request holds, counted after truncation."""
    embedding_concurrency: int = Field(default=4, ge=1)
    """The most embedding requests in flight at once."""
    summary_model: str = "gpt-4.1"
    """The Azure OpenAI chat deployment the disclosure-summary stage (`index --summaries`) generates through."""
    chunk_target_tokens: int = Field(default=400, gt=0)
    """The tokens the index aims for in one chunk of a source mirror."""
    chunk_max_tokens: int = Field(default=800, gt=0, le=7_500)
    """The most tokens one chunk of a source mirror holds, at most 7,500. A heading section within it stays one chunk."""
    chunk_planner_model: str = ""
    """The Azure OpenAI deployment the chunk planner runs on, at a low thinking level. Empty, the default, indexes with no chunk planner."""
    chunk_planner_ceiling_tokens: int = Field(default=12_000, gt=0)
    """The most tokens of text the chunk planner is shown at once. Longer text is filled."""
    chunk_planner_deadline_seconds: float = Field(default=60.0, gt=0)
    """How long one chunk planner call may take before its text is filled."""
    chunk_planner_concurrency: int = Field(default=4, ge=1)
    """How many chunk planner calls may run at once in one index."""

    @model_validator(mode="after")
    def _chunk_target_within_maximum(self) -> IngestionSettings:
        """Refuse a chunk target over the chunk maximum."""
        if self.chunk_target_tokens > self.chunk_max_tokens:
            msg = f"ingestion.chunk_target_tokens {self.chunk_target_tokens} exceeds ingestion.chunk_max_tokens {self.chunk_max_tokens}"
            raise ValueError(msg)
        return self


class DifficultyThresholdsSettings(BaseModel):
    """Tunable difficulty-band thresholds, which the host maps to its domain thresholds.

    Defaults are the standard research bands. A seed is `single_source` when it
    has at most that many sources, `deep_path` once a graph walk reaches `deep_path_hops`,
    `wide_fanout` past `wide_fanout_sources`, and `cross_section` once it spans
    `cross_section_sources` across `cross_section_sections`.
    """

    single_source: int = 1
    deep_path_hops: int = 2
    wide_fanout_sources: int = 4
    cross_section_sources: int = 2
    cross_section_sections: int = 3


class CoverageSettings(BaseModel):
    """Tunable knobs for coverage-based generation (mapped in the host's wiring).

    `salt` is the default sampling salt the stratified seed draw hashes with (a per-command `--salt` overrides
    it, and an unset salt is generated once at the command boundary and recorded in the dataset stamp so the
    draw stays reproducible). `min_pool_warn` is the seed-pool floor below which the `evals coverage` report
    flags a thin stratum. An out-of-range value raises `ValidationError` at construction.
    """

    salt: str | None = None
    min_pool_warn: int = Field(default=3, ge=1)


ARCHETYPES: tuple[str, ...] = ("pinpoint", "survey", "trace", "synthesis", "passage")
"""The archetypes a generation distribution may weight, in the evals' own order."""


class GenerationSettings(BaseModel):
    """Tunable knobs for structural eval-dataset generation, which the host maps to its generation config.

    `target_distribution` is keyed by archetype name. The host validates the names when it builds its
    generation config. `total_cases`
    caps the generated set, `target_per_seed` is how many cases each structural seed aims to yield,
    `dedup_threshold` is the cosine-similarity ceiling above which two cases collapse to one, `difficulty`
    carries the band thresholds, and `coverage` the coverage-scaling knobs (the sampling salt and the
    thin-pool warning floor). `frozen_fraction` and `frozen_min_per_stratum` tune the `evals freeze` carve: the
    fraction of an immutable dataset version the frozen held-out split freezes, and the min-per-stratum floor
    below which a thin stratum folds into its parent difficulty band. An out-of-range value raises
    `ValidationError` at construction.
    """

    target_distribution: dict[str, float] = Field(
        default_factory=lambda: {"pinpoint": 0.5, "survey": 0.2, "trace": 0.1, "synthesis": 0.1, "passage": 0.1}
    )
    total_cases: int = 50
    target_per_seed: int = 3
    dedup_threshold: float = 0.92
    difficulty: DifficultyThresholdsSettings = Field(default_factory=DifficultyThresholdsSettings)
    coverage: CoverageSettings = Field(default_factory=CoverageSettings)
    frozen_fraction: float = Field(default=0.2, gt=0, lt=1)
    frozen_min_per_stratum: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _check_distribution(self) -> GenerationSettings:
        """Reject an empty `target_distribution`, a negative weight, or an archetype that does not exist."""
        if not self.target_distribution:
            msg = "generation.target_distribution must not be empty"
            raise ValueError(msg)
        negative = sorted(name for name, weight in self.target_distribution.items() if weight < 0)
        if negative:
            msg = f"generation.target_distribution has negative weights: {', '.join(negative)}"
            raise ValueError(msg)
        unknown = sorted(set(self.target_distribution) - set(ARCHETYPES))
        if unknown:
            msg = (
                f"generation.target_distribution names unknown archetypes: {', '.join(unknown)}. "
                f"Known: {', '.join(ARCHETYPES)}"
            )
            raise ValueError(msg)
        return self

    @classmethod
    def anchor(cls) -> GenerationSettings:
        """A small pinpoint-weighted set for preemptive anchoring (fast, heavy on fact recall).

        Returns:
            Settings for ten cases, weighted towards `pinpoint`.
        """
        return cls(
            target_distribution={"pinpoint": 0.65, "survey": 0.15, "trace": 0.05, "synthesis": 0.05, "passage": 0.1}, total_cases=10
        )

    @classmethod
    def diagnostic(cls) -> GenerationSettings:
        """A larger balanced set for full diagnosis (the default distribution, wider coverage).

        Returns:
            Settings for eighty cases over the default distribution.
        """
        return cls(total_cases=80)


class TuningSettings(BaseModel):
    """Tunable knobs for the tuning loop, mapped to the domain tuning objective in the host's wiring.

    The metric and convergence fields default to the objective's own defaults (maximize `recall@5` under
    the `total_tokens` cost guard, a 10-round cap, patience 3, a 0.3 validation hold-out). The decision-core
    fields (`core_size`, `tail_size`, `min_core_per_stratum`, `bootstrap_resamples`, `bootstrap_confidence`)
    set the subsampled tuning. `policies_dir` is where winning-policy artifacts are written, relative to the
    engram project root like `datasets_dir`. An out-of-range value raises `ValidationError` at construction.
    """

    primary_metric: str = "recall@5"
    cost_metric: str = "total_tokens"
    cost_budget_pct: float = Field(default=0.10, ge=0)
    min_gain: float = Field(default=0.01, ge=0)
    max_rounds: int = Field(default=10, ge=1)
    patience: int = Field(default=3, ge=1)
    token_budget: int | None = Field(default=None, gt=0)
    val_split: float = Field(default=0.3, ge=0, lt=1)
    core_size: int = 100
    tail_size: int = 25
    min_core_per_stratum: int = 5
    bootstrap_resamples: int = Field(default=1000, ge=100)
    bootstrap_confidence: float = Field(default=0.95, gt=0, lt=1)
    policies_dir: Path = Path("evals/tuning")
    # `tune --apply-runtime` refuses when the dataset or frozen-split stale fraction exceeds this.
    # A human run only warns.
    apply_staleness_max: float = Field(default=0.0, ge=0, le=1)


class RecommendationSettings(BaseModel):
    """Tunable knobs for the recommendation loop.

    Policy for the detectors and the gate. `section_token_budget` is the retrieval-unit token
    ceiling the section-split detector flags an over-budget canonical section against, `max_hops`
    the reachability horizon the counterfactual edge-injection walk explores, `min_cases_per_proposal`
    the evidence floor the gate enforces before a proposal survives, and `max_proposals` the cap on how
    many ranked proposals a recommendation carries. An out-of-range value raises `ValidationError` at
    construction. `recommendations_dir` is where recommendation artifacts are written, relative to the
    engram project root like `policies_dir`.
    """

    section_token_budget: int = Field(default=600, ge=1)
    max_hops: int = Field(default=3, ge=1)
    min_cases_per_proposal: int = Field(default=2, ge=1)
    max_proposals: int = Field(default=5, ge=1)
    recommendations_dir: Path = Path("evals/recommendations")


class EvalsSettings(BaseModel):
    """Eval-loop policy, QA-generation mode + on-disk golden/RCA locations.

    Pure policy (non-secret): the Logfire *connection* lives in the
    infrastructure section, these fields only steer the eval pipeline.
    `autogen` is the indexing-event QA-generation mode, `queue` (the default)
    enqueues changed digest pages for a later worker run, `inline` generates during
    index, `off` disables it. `datasets_dir` is the golden-YAML location and
    `rca_queries_path` the saved-SQL file, both relative to the engram project root.
    `generation` carries the tunable structural eval-dataset generation knobs,
    `tuning` the tunable tuning-loop knobs, and `recommendation` the tunable
    recommendation-loop knobs.
    """

    autogen: Literal["queue", "inline", "off"] = "queue"
    datasets_dir: Path = Path("evals/datasets")
    rca_queries_path: Path = Path("evals/rca/queries.sql")
    generation: GenerationSettings = Field(default_factory=GenerationSettings)
    tuning: TuningSettings = Field(default_factory=TuningSettings)
    recommendation: RecommendationSettings = Field(default_factory=RecommendationSettings)


@dataclass(frozen=True)
class SourceResources:
    """Everything a `Connector.build` or `ScannerClass.build` classmethod might need, as one value.

    `knowledge_base` is the only field a caller must supply. `document_intelligence` is `None` when no endpoint is
    configured, and a converter that needs it raises naming it. `connectors` carries the knowledge base's
    declared connector names in declared order, so a consumer classifies a URL the way the knowledge base
    does without reading its schema from disk. `settings` carries whatever else a connector or a scanner
    needs, keyed by a name the consumer documents, as raw JSON the consumer validates with its own model at
    build time. A new need is a block in `settings`, never a new `build` parameter.
    """

    knowledge_base: KnowledgeBaseSettings
    document_intelligence: DocumentIntelligenceSettings | None = None
    connectors: tuple[str, ...] = ()
    settings: Mapping[str, Mapping[str, JsonValue]] = field(default_factory=dict)
