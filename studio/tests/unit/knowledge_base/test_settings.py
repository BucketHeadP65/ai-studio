"""Unit tests for `src/studio/knowledge_base/settings.py` — the DDD capability-group settings tree.

Most settings are constructed with explicit init kwargs (which take precedence
over the JSON source), so the tests are self-contained — no real environment,
no committed-config dependency. The `ApplicationSettings` source tests point the
single :class:`~pydantic_settings.JsonConfigSettingsSource` at a throwaway file via
the `json_file=` init seam, so they never read the repo's
`config/application_settings.json`. The `KnowledgeBaseSettings` validator needs a directory holding a
`knowledge-base-schema.json`, so a mini knowledge base is built in `tmp_path`.

There is no `.env` mechanism here: `APP_*` env vars are the override seam and
the local JSON is the structured secret tier.
"""

from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from engram.config import SourceResources
from engram.testing.specs import research_spec
from pydantic import ValidationError

from studio.knowledge_base import settings as settings_module
from studio.knowledge_base.container import create_container
from studio.knowledge_base.settings import (
    ApplicationSettings,
    AzureOpenAiSettings,
    CoverageSettings,
    DatabaseSettings,
    DbDialect,
    DifficultyThresholdsSettings,
    DocumentIntelligenceSettings,
    EvalsSettings,
    GenerationSettings,
    GraphBackend,
    InfrastructureSettings,
    IngestionSettings,
    KnowledgeBaseEntry,
    KnowledgeBaseSettings,
    LogfireSettings,
    RecommendationSettings,
    RetrievalSettings,
    Schedule,
    SourceSettings,
    TuningSettings,
    fetch_application_settings,
)
from tests.support.http_app import INFRASTRUCTURE_JSON, SETTINGS_JSON

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

_URL = "postgresql+asyncpg://knowledge_bases:knowledge_bases@localhost:5433/knowledge_bases"
_READER_URL = "postgresql+asyncpg://kb_reader:kb_reader@localhost:5433/knowledge_bases"


def _make_knowledge_base(root: Path) -> Path:
    """A minimal directory that passes the `KnowledgeBaseSettings` shape validator."""
    (root / "_meta").mkdir(parents=True, exist_ok=True)
    (root / "knowledge-base-schema.json").write_text("{}", encoding="utf-8")
    return root


def _write_app_json(root: Path, payload: Mapping[str, object]) -> Path:
    """Write a throwaway `application_settings.json` under `root` and return its path."""
    json_file = root / "application_settings.json"
    json_file.write_text(json.dumps(payload), encoding="utf-8")
    return json_file


_EXAMPLE_SETTINGS_FILE = Path(__file__).resolve().parents[4] / "engram" / "config" / "application_settings.example.json"

_INFRASTRUCTURE_JSON: dict[str, JsonValue] = INFRASTRUCTURE_JSON
"""The shared `infrastructure` payload (`tests.support.http_app`), so this module's file-format tests
carry the same values every other settings-tree test builds against."""

_FULL_JSON: dict[str, JsonValue] = SETTINGS_JSON
"""The shared full settings payload (`tests.support.http_app`)."""


def _app_json(**overrides: object) -> dict[str, object]:
    """A settings payload carrying every section, plus arbitrary section overrides."""
    payload: dict[str, object] = dict(_FULL_JSON)
    payload["infrastructure"] = dict(_INFRASTRUCTURE_JSON)
    payload.update(overrides)
    return payload


class TestKnowledgeBaseSettings:
    def test_accepts_a_directory_with_a_knowledge_base_schema_json(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path)

        settings = KnowledgeBaseSettings(path=knowledge_base)

        assert settings.path == knowledge_base

    def test_rejects_a_missing_path(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            KnowledgeBaseSettings(path=tmp_path / "does-not-exist")

    def test_rejects_a_directory_with_no_schema_and_names_the_file(self, tmp_path: Path):
        (tmp_path / "_meta").mkdir(parents=True)

        with pytest.raises(ValidationError, match=r"knowledge-base-schema\.json"):
            KnowledgeBaseSettings(path=tmp_path)

    def test_sources_subdir_defaults_to_sources(self, tmp_path: Path):
        settings = KnowledgeBaseSettings(path=_make_knowledge_base(tmp_path))

        assert settings.sources_subdir == "Sources"

    def test_sources_dir_and_archive_dir_derive_from_path_and_subdir(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path)

        settings = KnowledgeBaseSettings(path=knowledge_base)

        assert settings.sources_dir == knowledge_base / "Sources"
        assert settings.archive_dir == knowledge_base / "Sources" / "_archive"

    def test_sources_subdir_override_flows_into_derived_paths(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path)

        settings = KnowledgeBaseSettings(path=knowledge_base, sources_subdir="Mirrors")

        assert settings.sources_dir == knowledge_base / "Mirrors"
        assert settings.archive_dir == knowledge_base / "Mirrors" / "_archive"


class TestRetrievalSettings:
    def test_defaults_to_pg_textsearch_and_age(self):
        settings = RetrievalSettings()

        assert settings.dialect is DbDialect.PG_TEXTSEARCH
        assert settings.graph_backend is GraphBackend.AGE

    def test_retrieval_profile_defaults_to_full(self):
        settings = RetrievalSettings()

        assert settings.retrieval_profile == "full"

    def test_retrieval_profile_set_via_init(self):
        settings = RetrievalSettings(retrieval_profile="search-hybrid")

        assert settings.retrieval_profile == "search-hybrid"

    def test_retrieval_profile_reads_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # `model_validate` still layers env + the module JSON constant, so it is pointed at a
        # throwaway file (never the repo's real, gitignored config) to keep the test hermetic.
        json_file = _write_app_json(tmp_path, _app_json())
        monkeypatch.setattr(settings_module, "_APPLICATION_SETTINGS_JSON", json_file)
        monkeypatch.setenv("APP_RETRIEVAL__RETRIEVAL_PROFILE", "graph-only")

        settings = ApplicationSettings.model_validate({"retrieval": {}})

        # The env var overrides the default at the ApplicationSettings level.
        assert settings.retrieval.retrieval_profile == "graph-only"

    def test_dialect_coerces_from_strings(self):
        assert RetrievalSettings.model_validate({"dialect": "pg_textsearch"}).dialect is DbDialect.PG_TEXTSEARCH
        assert RetrievalSettings.model_validate({"dialect": "pg_search"}).dialect is DbDialect.PG_SEARCH

    def test_graph_backend_coerces_from_strings(self):
        assert RetrievalSettings.model_validate({"graph_backend": "age"}).graph_backend is GraphBackend.AGE
        assert RetrievalSettings.model_validate({"graph_backend": "cte"}).graph_backend is GraphBackend.CTE

    def test_rejects_unknown_dialect(self):
        with pytest.raises(ValidationError):
            RetrievalSettings.model_validate({"dialect": "neo4j"})

    def test_rejects_unknown_graph_backend(self):
        with pytest.raises(ValidationError):
            RetrievalSettings.model_validate({"graph_backend": "neo4j"})

    def test_the_agent_tunes_k_by_default(self):
        # Default-safe: no eval baseline or existing MCP contract moves until a host opts out.
        settings = RetrievalSettings()

        assert settings.agent_tunes_k is True
        assert settings.k_default == 10

    def test_a_host_can_pin_the_cap(self):
        settings = RetrievalSettings(agent_tunes_k=False, k_default=4)

        assert settings.agent_tunes_k is False
        assert settings.k_default == 4

    def test_the_flag_reads_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # `model_validate` still layers env + the module JSON constant, so it is pointed at a
        # throwaway file (never the repo's real, gitignored config) to keep the test hermetic.
        json_file = _write_app_json(tmp_path, _app_json())
        monkeypatch.setattr(settings_module, "_APPLICATION_SETTINGS_JSON", json_file)
        monkeypatch.setenv("APP_RETRIEVAL__AGENT_TUNES_K", "false")
        monkeypatch.setenv("APP_RETRIEVAL__K_DEFAULT", "25")

        settings = ApplicationSettings.model_validate({"retrieval": {}})

        assert settings.retrieval.agent_tunes_k is False
        assert settings.retrieval.k_default == 25

    @pytest.mark.parametrize("k", [0, -1, 101])
    def test_rejects_a_cap_outside_the_serveable_band(self, k: int):
        # A pinned cap the host cannot serve is a config error, not a value to clamp at call time.
        with pytest.raises(ValidationError):
            RetrievalSettings.model_validate({"k_default": k})


class TestIngestionSettings:
    def test_defaults_match_the_contract(self):
        settings = IngestionSettings()

        assert settings.embedding_dimensions == 1024
        assert settings.embedding_batch_size == 64

    def test_overrides_are_honoured(self):
        settings = IngestionSettings(embedding_dimensions=512, embedding_batch_size=8)

        assert settings.embedding_dimensions == 512
        assert settings.embedding_batch_size == 8

    @pytest.mark.parametrize(("field", "value"), [("embedding_dimensions", 0), ("embedding_batch_size", 0), ("embedding_batch_size", -1)])
    def test_rejects_non_positive_values(self, field: str, value: int):
        # `model_validate`, not `IngestionSettings(**{field: value})`: the settings carry a mixed
        # field-type set (`summary_model: str` alongside the two `int` fields), so pyright rejects a
        # dynamic-key `**dict[str, int]` unpack as a possible `int` for the `str` field.
        # `model_validate` takes the same untyped-at-the-edge mapping without that static conflict.
        with pytest.raises(ValidationError):
            IngestionSettings.model_validate({field: value})

    def test_summary_model_defaults_to_gpt_4_1(self):
        assert IngestionSettings().summary_model == "gpt-4.1"

    def test_summary_model_override_is_honoured(self):
        assert IngestionSettings(summary_model="gpt-5").summary_model == "gpt-5"


class TestLoggingSettings:
    def test_a_file_without_the_section_names_the_app_studio_and_prints_nothing(self, tmp_path: Path):
        settings = ApplicationSettings(json_file=_write_app_json(tmp_path, _app_json()))

        assert (settings.logging.service_name, settings.logging.level, settings.logging.console) == ("studio", "INFO", False)

    def test_a_partial_section_keeps_the_service_name(self, tmp_path: Path):
        settings = ApplicationSettings(json_file=_write_app_json(tmp_path, _app_json(logging={"level": "DEBUG", "environment": "test"})))

        assert (settings.logging.service_name, settings.logging.level, settings.logging.environment) == ("studio", "DEBUG", "test")

    def test_the_example_file_builds_its_logging_section(self):
        settings = ApplicationSettings(json_file=_EXAMPLE_SETTINGS_FILE)

        assert settings.logging.service_name == "studio"
        assert settings.logging.console is False


class TestEvalsSettings:
    def test_defaults_match_the_contract(self):
        settings = EvalsSettings()

        assert settings.autogen == "queue"
        assert settings.datasets_dir == Path("evals/datasets")
        assert settings.rca_queries_path == Path("evals/rca/queries.sql")

    def test_autogen_accepts_the_closed_set(self):
        assert EvalsSettings(autogen="inline").autogen == "inline"
        assert EvalsSettings(autogen="off").autogen == "off"

    def test_rejects_unknown_autogen_mode(self):
        with pytest.raises(ValidationError):
            EvalsSettings.model_validate({"autogen": "cron"})

    def test_paths_parse_from_init_kwargs(self):
        settings = EvalsSettings(datasets_dir=Path("custom/datasets"), rca_queries_path=Path("custom/rca.sql"))

        assert settings.datasets_dir == Path("custom/datasets")
        assert settings.rca_queries_path == Path("custom/rca.sql")

    def test_generation_defaults_to_generation_settings(self):
        settings = EvalsSettings()

        assert isinstance(settings.generation, GenerationSettings)
        assert settings.generation.total_cases == 50
        assert settings.generation.target_per_seed == 3
        assert settings.generation.dedup_threshold == pytest.approx(0.92)

    def test_generation_override_flows_through_init(self):
        settings = EvalsSettings(generation=GenerationSettings(total_cases=7))

        assert settings.generation.total_cases == 7

    def test_tuning_defaults_to_tuning_settings(self):
        settings = EvalsSettings()

        assert isinstance(settings.tuning, TuningSettings)
        assert settings.tuning.primary_metric == "recall@5"
        assert settings.tuning.policies_dir == Path("evals/tuning")

    def test_tuning_override_flows_through_init(self):
        settings = EvalsSettings(tuning=TuningSettings(max_rounds=7))

        assert settings.tuning.max_rounds == 7

    def test_recommendation_defaults_to_recommendation_settings(self):
        settings = EvalsSettings()

        assert isinstance(settings.recommendation, RecommendationSettings)
        assert settings.recommendation.section_token_budget == 600
        assert settings.recommendation.recommendations_dir == Path("evals/recommendations")

    def test_recommendation_override_flows_through_init(self):
        settings = EvalsSettings(recommendation=RecommendationSettings(max_proposals=8))

        assert settings.recommendation.max_proposals == 8


class TestTuningSettings:
    def test_defaults_match_the_contract(self):
        tuning = TuningSettings()

        assert tuning.primary_metric == "recall@5"
        assert tuning.cost_metric == "total_tokens"
        assert tuning.cost_budget_pct == pytest.approx(0.10)
        assert tuning.min_gain == pytest.approx(0.01)
        assert tuning.max_rounds == 10
        assert tuning.patience == 3
        assert tuning.token_budget is None
        assert tuning.val_split == pytest.approx(0.3)
        assert tuning.core_size == 100
        assert tuning.tail_size == 25
        assert tuning.min_core_per_stratum == 5
        assert tuning.bootstrap_resamples == 1000
        assert tuning.bootstrap_confidence == pytest.approx(0.95)
        assert tuning.policies_dir == Path("evals/tuning")

    def test_overrides_are_honoured(self):
        tuning = TuningSettings(max_rounds=5, token_budget=2000, policies_dir=Path("custom/policies"))

        assert tuning.max_rounds == 5
        assert tuning.token_budget == 2000
        assert tuning.policies_dir == Path("custom/policies")

    def test_decision_core_knobs_are_honoured(self):
        tuning = TuningSettings(
            core_size=64, tail_size=16, min_core_per_stratum=3, bootstrap_resamples=200, bootstrap_confidence=0.9
        )

        assert tuning.core_size == 64
        assert tuning.tail_size == 16
        assert tuning.min_core_per_stratum == 3
        assert tuning.bootstrap_resamples == 200
        assert tuning.bootstrap_confidence == pytest.approx(0.9)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("cost_budget_pct", -0.01),
            ("min_gain", -0.01),
            ("max_rounds", 0),
            ("patience", 0),
            ("token_budget", 0),
            ("val_split", 1.0),
            ("val_split", -0.1),
            ("bootstrap_resamples", 99),
            ("bootstrap_confidence", 0.0),
            ("bootstrap_confidence", 1.0),
            ("bootstrap_confidence", -0.1),
        ],
    )
    def test_rejects_out_of_range_values(self, field: str, value: float):
        # `model_validate` takes the heterogeneously-typed override as a mapping (the same boundary the
        # other settings range tests cross), so each out-of-range knob surfaces as a `ValidationError`.
        with pytest.raises(ValidationError):
            TuningSettings.model_validate({field: value})


class TestRecommendationSettings:
    def test_defaults_match_the_contract(self):
        recommendation = RecommendationSettings()

        assert recommendation.section_token_budget == 600
        assert recommendation.max_hops == 3
        assert recommendation.min_cases_per_proposal == 2
        assert recommendation.max_proposals == 5
        assert recommendation.recommendations_dir == Path("evals/recommendations")

    def test_overrides_are_honoured(self):
        recommendation = RecommendationSettings(section_token_budget=900, max_hops=5, recommendations_dir=Path("custom/dir"))

        assert recommendation.section_token_budget == 900
        assert recommendation.max_hops == 5
        assert recommendation.recommendations_dir == Path("custom/dir")

    @pytest.mark.parametrize(
        ("field", "value"), [("section_token_budget", 0), ("max_hops", 0), ("min_cases_per_proposal", 0), ("max_proposals", 0)]
    )
    def test_rejects_out_of_range_values(self, field: str, value: int):
        # `model_validate` takes the heterogeneously-typed override as a mapping (the same boundary the
        # other settings range tests cross), so each out-of-range knob surfaces as a `ValidationError`.
        with pytest.raises(ValidationError):
            RecommendationSettings.model_validate({field: value})


class TestDifficultyThresholdsSettings:
    def test_defaults_match_the_standard_research_bands(self):
        thresholds = DifficultyThresholdsSettings()

        assert thresholds.single_source == 1
        assert thresholds.deep_path_hops == 2
        assert thresholds.wide_fanout_sources == 4
        assert thresholds.cross_section_sources == 2
        assert thresholds.cross_section_sections == 3

    def test_overrides_are_honoured(self):
        thresholds = DifficultyThresholdsSettings(deep_path_hops=3, wide_fanout_sources=6)

        assert thresholds.deep_path_hops == 3
        assert thresholds.wide_fanout_sources == 6


class TestGenerationSettings:
    def test_defaults_match_the_contract(self):
        gen = GenerationSettings()

        assert gen.total_cases == 50
        assert gen.target_per_seed == 3
        assert gen.dedup_threshold == pytest.approx(0.92)
        assert gen.target_distribution == {"pinpoint": 0.5, "survey": 0.2, "trace": 0.1, "synthesis": 0.1, "passage": 0.1}
        assert sum(gen.target_distribution.values()) == pytest.approx(1.0)
        assert isinstance(gen.difficulty, DifficultyThresholdsSettings)

    def test_frozen_split_knobs_default_to_the_contract(self):
        gen = GenerationSettings()

        assert gen.frozen_fraction == pytest.approx(0.2)
        assert gen.frozen_min_per_stratum == 3

    def test_frozen_split_knobs_flow_through_init(self):
        gen = GenerationSettings(frozen_fraction=0.35, frozen_min_per_stratum=5)

        assert gen.frozen_fraction == pytest.approx(0.35)
        assert gen.frozen_min_per_stratum == 5

    @pytest.mark.parametrize(("field", "value"), [("frozen_fraction", 0.0), ("frozen_fraction", 1.0), ("frozen_min_per_stratum", 0)])
    def test_rejects_out_of_range_frozen_knobs(self, field: str, value: float):
        # `model_validate` takes the heterogeneously-typed override as a mapping (the range-test boundary
        # the other settings specs cross), so each out-of-range knob surfaces as a `ValidationError`.
        with pytest.raises(ValidationError):
            GenerationSettings.model_validate({field: value})

    def test_difficulty_defaults_to_the_standard_bands(self):
        gen = GenerationSettings()

        assert gen.difficulty.single_source == 1
        assert gen.difficulty.deep_path_hops == 2
        assert gen.difficulty.wide_fanout_sources == 4
        assert gen.difficulty.cross_section_sources == 2
        assert gen.difficulty.cross_section_sections == 3

    def test_each_construction_gets_an_independent_distribution(self):
        first = GenerationSettings()
        second = GenerationSettings()

        first.target_distribution["pinpoint"] = 0.9

        assert second.target_distribution["pinpoint"] == pytest.approx(0.5)

    def test_anchor_preset_is_small_and_pinpoint_weighted(self):
        anchor = GenerationSettings.anchor()

        assert isinstance(anchor, GenerationSettings)
        assert anchor.total_cases == 10
        # The pinpoint band dominates the anchor set.
        weights = anchor.target_distribution
        assert weights["pinpoint"] == max(weights.values())
        assert weights["passage"] > 0

    def test_diagnostic_preset_is_larger_and_balanced(self):
        diagnostic = GenerationSettings.diagnostic()

        assert isinstance(diagnostic, GenerationSettings)
        assert diagnostic.total_cases >= 50
        assert sum(diagnostic.target_distribution.values()) == pytest.approx(1.0)

    def test_anchor_is_smaller_than_diagnostic(self):
        assert GenerationSettings.anchor().total_cases < GenerationSettings.diagnostic().total_cases

    def test_rejects_an_empty_distribution(self):
        with pytest.raises(ValidationError):
            GenerationSettings(target_distribution={})

    def test_rejects_a_negative_weight(self):
        with pytest.raises(ValidationError):
            GenerationSettings(target_distribution={"pinpoint": 1.5, "survey": -0.5})

    def test_an_unknown_archetype_in_the_distribution_is_refused_by_name(self):
        with pytest.raises(ValidationError, match=r"unknown archetypes: contrast\. Known: pinpoint, survey, trace, synthesis, passage"):
            GenerationSettings(target_distribution={"pinpoint": 0.5, "contrast": 0.5})

    def test_accepts_a_custom_well_formed_distribution(self):
        gen = GenerationSettings(target_distribution={"pinpoint": 0.5, "survey": 0.5})

        assert gen.target_distribution == {"pinpoint": 0.5, "survey": 0.5}

    def test_coverage_defaults_to_coverage_settings(self):
        gen = GenerationSettings()

        assert isinstance(gen.coverage, CoverageSettings)
        assert gen.coverage.salt is None
        assert gen.coverage.min_pool_warn == 3

    def test_coverage_override_flows_through_init(self):
        gen = GenerationSettings(coverage=CoverageSettings(salt="abc", min_pool_warn=7))

        assert gen.coverage.salt == "abc"
        assert gen.coverage.min_pool_warn == 7


class TestCoverageSettings:
    def test_defaults_match_the_contract(self):
        coverage = CoverageSettings()

        assert coverage.salt is None
        assert coverage.min_pool_warn == 3

    def test_overrides_are_honoured(self):
        coverage = CoverageSettings(salt="s1", min_pool_warn=5)

        assert coverage.salt == "s1"
        assert coverage.min_pool_warn == 5

    def test_rejects_a_non_positive_min_pool_warn(self):
        with pytest.raises(ValidationError):
            CoverageSettings(min_pool_warn=0)


class TestInfrastructureModels:
    def test_database_settings_require_both_urls(self):
        resource = DatabaseSettings(url=_URL, reader_url=_READER_URL)

        assert resource.url == _URL
        assert resource.reader_url == _READER_URL

    def test_database_settings_reject_a_missing_url(self):
        with pytest.raises(ValidationError):
            DatabaseSettings.model_validate({"reader_url": _READER_URL})

    def test_azure_openai_settings_default_to_an_empty_endpoint(self):
        assert AzureOpenAiSettings().endpoint == ""

    def test_azure_openai_settings_hold_the_endpoint_only(self):
        resource = AzureOpenAiSettings(endpoint="https://x.openai.azure.com")

        assert resource.endpoint == "https://x.openai.azure.com"
        assert set(AzureOpenAiSettings.model_fields) == {"endpoint"}

    def test_document_intelligence_settings_default_to_empty(self):
        assert DocumentIntelligenceSettings().endpoint == ""

    def test_document_intelligence_settings_are_honoured_when_given(self):
        resource = DocumentIntelligenceSettings(endpoint="https://di.cognitiveservices.azure.com")

        assert resource.endpoint == "https://di.cognitiveservices.azure.com"

    def test_logfire_settings_default_to_empty_tokens_and_eu_base_url(self):
        resource = LogfireSettings()

        assert resource.write_token == ""
        assert resource.read_token == ""
        assert resource.base_url == "https://logfire-eu.pydantic.dev"

    def test_logfire_settings_are_honoured_when_given(self):
        resource = LogfireSettings(write_token="wt", read_token="rt", base_url="https://logfire-us.pydantic.dev")

        assert resource.write_token == "wt"
        assert resource.read_token == "rt"
        assert resource.base_url == "https://logfire-us.pydantic.dev"

    def test_infrastructure_refuses_a_missing_database_naming_it(self):
        declared = {key: value for key, value in _INFRASTRUCTURE_JSON.items() if key != "database"}

        with pytest.raises(ValidationError, match="database"):
            InfrastructureSettings.model_validate(declared)

    def test_infrastructure_defaults_every_system_but_the_database(self):
        settings = InfrastructureSettings.model_validate({"database": _INFRASTRUCTURE_JSON["database"]})

        assert settings.document_intelligence.endpoint == ""
        assert settings.openrouter.api_key == ""


class TestEverySectionIsMandatory:
    """A settings file missing a section refuses construction, naming the section."""

    def test_a_file_without_retrieval_refuses(self, tmp_path: Path):
        json_file = tmp_path / "application_settings.json"
        json_file.write_text(json.dumps({"infrastructure": _INFRASTRUCTURE_JSON}), encoding="utf-8")

        with pytest.raises(ValidationError, match="retrieval"):
            ApplicationSettings(json_file=json_file)

    def test_a_file_without_the_infrastructure_section_refuses(self, tmp_path: Path):
        payload = {key: value for key, value in _FULL_JSON.items() if key != "infrastructure"}
        json_file = tmp_path / "application_settings.json"
        json_file.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValidationError, match="infrastructure"):
            ApplicationSettings(json_file=json_file)

    def test_a_file_without_the_harness_address_reads_the_loopback_default(self, tmp_path: Path):
        infrastructure = {key: value for key, value in _INFRASTRUCTURE_JSON.items() if key != "harness"}
        json_file = tmp_path / "application_settings.json"
        json_file.write_text(json.dumps(_FULL_JSON | {"infrastructure": infrastructure}), encoding="utf-8")

        settings = ApplicationSettings(json_file=json_file)

        assert settings.infrastructure.harness.base_url == "http://127.0.0.1:8770"

    def test_a_full_file_constructs(self, tmp_path: Path):
        json_file = tmp_path / "application_settings.json"
        json_file.write_text(json.dumps(_FULL_JSON), encoding="utf-8")

        settings = ApplicationSettings(json_file=json_file)

        assert settings.retrieval.dialect is DbDialect.PG_TEXTSEARCH
        assert settings.infrastructure.gateway.base_url == "https://gateway-eu.pydantic.dev/proxy"
        assert settings.infrastructure.harness.base_url == "http://127.0.0.1:8770"

    def test_the_committed_example_file_carries_every_section(self):
        settings = ApplicationSettings(json_file=_EXAMPLE_SETTINGS_FILE)

        assert settings.evals.autogen == "queue"
        assert settings.infrastructure.document_intelligence.endpoint == ""

    def test_the_settings_carry_no_knowledge_base_field(self):
        assert "knowledge_base" not in ApplicationSettings.model_fields


class TestApplicationSettings:
    def test_composes_all_sub_settings(self, tmp_path: Path):
        azure_openai = {"endpoint": "https://x.openai.azure.com"}
        ingestion = {"embedding_deployment": "d", "embedding_dimensions": 1024, "embedding_batch_size": 64}
        json_file = _write_app_json(
            tmp_path, _app_json(infrastructure=_INFRASTRUCTURE_JSON | {"azure_openai": azure_openai}, ingestion=ingestion)
        )

        settings = ApplicationSettings(json_file=json_file)

        assert settings.infrastructure.database.url.endswith("/knowledge_bases")
        assert settings.infrastructure.azure_openai.endpoint == "https://x.openai.azure.com"
        assert settings.ingestion.embedding_deployment == "d"
        assert settings.retrieval.dialect is DbDialect.PG_TEXTSEARCH
        assert settings.ingestion.embedding_dimensions == 1024
        assert settings.evals.autogen == "queue"
        # The secret tier exposes the optional Logfire connection at the file's values.
        assert settings.infrastructure.logfire.base_url == "https://logfire-eu.pydantic.dev"


class TestKnowledgeBasesRegistry:
    """`ApplicationSettings.knowledge_bases` — the registry a knowledge base is looked up in."""

    def test_an_explicit_empty_registry_reads_as_empty(self, tmp_path: Path):
        json_file = _write_app_json(tmp_path, _app_json())

        settings = ApplicationSettings(json_file=json_file)

        assert settings.knowledge_bases == {}
        assert settings.active_knowledge_base_name(KnowledgeBaseSettings(path=_make_knowledge_base(tmp_path / "kb"))) is None

    def test_omitting_the_section_still_defaults_to_an_empty_registry(self, tmp_path: Path):
        """Unlike the five mandatory sections, `knowledge_bases` is a mapping with its own field default."""
        payload = _app_json()
        del payload["knowledge_bases"]
        json_file = _write_app_json(tmp_path, payload)

        settings = ApplicationSettings(json_file=json_file)

        assert settings.knowledge_bases == {}

    def test_accepts_a_registry_the_knowledge_base_is_a_member_of(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path / "primary")
        other = _make_knowledge_base(tmp_path / "other")
        registry = {"primary": {"path": str(knowledge_base)}, "other": {"path": str(other), "description": "a second kb"}}
        json_file = _write_app_json(tmp_path, _app_json(knowledge_bases=registry))

        settings = ApplicationSettings(json_file=json_file)

        assert set(settings.knowledge_bases) == {"primary", "other"}
        assert settings.knowledge_bases["other"].description == "a second kb"
        assert settings.active_knowledge_base_name(KnowledgeBaseSettings(path=knowledge_base)) == "primary"

    def test_rejects_a_blank_registry_name(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path / "kb")
        json_file = _write_app_json(tmp_path, _app_json(knowledge_bases={"": {"path": str(knowledge_base)}}))

        with pytest.raises(ValidationError):
            ApplicationSettings(json_file=json_file)

    def test_rejects_two_entries_sharing_a_path(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path / "kb")
        registry = {"a": {"path": str(knowledge_base)}, "b": {"path": str(knowledge_base)}}
        json_file = _write_app_json(tmp_path, _app_json(knowledge_bases=registry))

        with pytest.raises(ValidationError):
            ApplicationSettings(json_file=json_file)

    def test_json_file_populates_the_registry(self, tmp_path: Path):
        knowledge_base = _make_knowledge_base(tmp_path / "primary")
        other = _make_knowledge_base(tmp_path / "other")
        payload = _app_json(knowledge_bases={"primary": {"path": str(knowledge_base)}, "other": {"path": str(other), "description": "d"}})
        json_file = _write_app_json(tmp_path, payload)

        settings = ApplicationSettings(json_file=json_file)

        assert settings.active_knowledge_base_name(KnowledgeBaseSettings(path=knowledge_base)) == "primary"
        assert settings.knowledge_bases["other"].description == "d"


class TestApplicationSettingsJsonSource:
    def test_json_file_populates_the_nested_models(self, tmp_path: Path):
        payload = _app_json(retrieval={"graph_backend": "age"}, ingestion={"embedding_dimensions": 512})
        json_file = _write_app_json(tmp_path, payload)

        settings = ApplicationSettings(json_file=json_file)

        assert settings.infrastructure.database.reader_url.startswith("postgresql+asyncpg://kb_reader")
        assert settings.retrieval.graph_backend is GraphBackend.AGE
        assert settings.ingestion.embedding_dimensions == 512

    def test_an_app_env_var_overrides_the_json_value(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        json_file = _write_app_json(tmp_path, _app_json(retrieval={"dialect": "pg_search"}))
        monkeypatch.setenv("APP_RETRIEVAL__DIALECT", "pg_textsearch")

        settings = ApplicationSettings(json_file=json_file)

        # env wins over JSON for the overridden field; the rest still comes from JSON.
        assert settings.retrieval.dialect is DbDialect.PG_TEXTSEARCH
        assert settings.infrastructure.database.url.endswith("/knowledge_bases")

    def test_an_evals_env_var_maps_onto_the_capability_group(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        json_file = _write_app_json(tmp_path, _app_json())
        monkeypatch.setenv("APP_EVALS__AUTOGEN", "inline")

        settings = ApplicationSettings(json_file=json_file)

        # The `APP_EVALS__*` nested override maps onto the policy group.
        assert settings.evals.autogen == "inline"
        # The other evals leaves still come from the file.
        assert settings.evals.datasets_dir == Path("evals/datasets")

    def test_an_evals_recommendation_env_var_maps_onto_the_nested_group(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        json_file = _write_app_json(tmp_path, _app_json())
        monkeypatch.setenv("APP_EVALS__RECOMMENDATION__SECTION_TOKEN_BUDGET", "900")
        monkeypatch.setenv("APP_EVALS__RECOMMENDATION__RECOMMENDATIONS_DIR", "custom/dir")

        settings = ApplicationSettings(json_file=json_file)

        # The `APP_EVALS__RECOMMENDATION__*` nested overrides map onto the recommendation group.
        assert settings.evals.recommendation.section_token_budget == 900
        assert settings.evals.recommendation.recommendations_dir == Path("custom/dir")
        # The other recommendation leaves still fall back to their defaults.
        assert settings.evals.recommendation.max_hops == 3

    def test_an_evals_generation_block_round_trips_through_json(self, tmp_path: Path):
        generation = {"total_cases": 12, "target_per_seed": 5, "difficulty": {"deep_path_hops": 4}}
        json_file = _write_app_json(tmp_path, _app_json(evals={"generation": generation}))

        settings = ApplicationSettings(json_file=json_file)

        assert settings.evals.generation.total_cases == 12
        assert settings.evals.generation.target_per_seed == 5
        assert settings.evals.generation.difficulty.deep_path_hops == 4
        # Unspecified leaves still fall back to their defaults.
        assert settings.evals.generation.dedup_threshold == pytest.approx(0.92)
        assert settings.evals.generation.difficulty.single_source == 1

    def test_a_nested_resource_env_var_overrides_a_single_leaf(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        json_file = _write_app_json(tmp_path, _app_json())
        override = "postgresql+asyncpg://knowledge_bases:knowledge_bases@cloud:5432/knowledge_bases"
        monkeypatch.setenv("APP_INFRASTRUCTURE__DATABASE__URL", override)

        settings = ApplicationSettings(json_file=json_file)

        # The single nested leaf is overridden; the sibling reader_url still comes from JSON.
        assert settings.infrastructure.database.url == override
        assert settings.infrastructure.database.reader_url.startswith("postgresql+asyncpg://kb_reader")

    def test_init_sub_instance_is_not_polluted_by_the_json_source(self, tmp_path: Path):
        json_file = _write_app_json(tmp_path, _app_json(retrieval={"dialect": "pg_textsearch"}))

        settings = ApplicationSettings(json_file=json_file, retrieval=RetrievalSettings(dialect=DbDialect.PG_SEARCH, graph_backend=GraphBackend.AGE))

        # The explicit sub-instance wins wholesale — JSON does not bleed into it.
        assert settings.retrieval.dialect is DbDialect.PG_SEARCH
        assert settings.retrieval.graph_backend is GraphBackend.AGE

    def test_a_missing_json_file_falls_back_to_init_kwargs(self, tmp_path: Path):
        settings = ApplicationSettings(
            json_file=tmp_path / "does-not-exist.json",
            retrieval=RetrievalSettings(),
            ingestion=IngestionSettings(),
            sources=SourceSettings({}),
            evals=EvalsSettings(),
            infrastructure=InfrastructureSettings.model_validate(_INFRASTRUCTURE_JSON),
        )

        assert settings.retrieval.dialect is DbDialect.PG_TEXTSEARCH
        assert settings.infrastructure.database.url.endswith("/knowledge_bases")


class TestFetchApplicationSettings:
    def test_zero_arg_reads_the_module_json_and_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The zero-arg path layers the single JSON module constant — read at call time in
        # `settings_customise_sources`, so pointing it at a throwaway file carrying the full tree
        # keeps the test hermetic — under `APP_*` env.
        json_file = _write_app_json(tmp_path, _app_json())
        monkeypatch.setattr(settings_module, "_APPLICATION_SETTINGS_JSON", json_file)

        settings = fetch_application_settings()

        assert isinstance(settings, ApplicationSettings)
        assert settings.infrastructure.database.url.endswith("/knowledge_bases")

    def test_threads_the_json_file_seam(self, tmp_path: Path):
        # The `json_file` seam threads through the fetch so a caller can stay hermetic without
        # pointing the module constant somewhere else.
        json_file = _write_app_json(tmp_path, _app_json(ingestion={"embedding_dimensions": 512}))

        settings = fetch_application_settings(json_file=json_file)

        assert settings.ingestion.embedding_dimensions == 512

    def test_a_knowledge_base_outside_the_registry_has_no_name(self, tmp_path: Path) -> None:
        json_file = _write_app_json(tmp_path, _app_json())

        settings = fetch_application_settings(json_file=json_file)

        assert settings.active_knowledge_base_name(KnowledgeBaseSettings(path=_make_knowledge_base(tmp_path / "kb"))) is None

    def test_two_calls_in_one_process_do_not_share_an_instance(self, tmp_path: Path):
        # An `lru_cache` here would hand the second caller the first caller's tree.
        json_file = _write_app_json(tmp_path, _app_json())

        first = fetch_application_settings(json_file=json_file)
        second = fetch_application_settings(json_file=json_file)

        assert first is not second


class TestSchedule:
    """`Schedule` — a registry entry's optional ingest cadence: exactly one of two closed shapes."""

    def test_accepts_every_seconds_at_the_floor(self):
        schedule = Schedule(every_seconds=300)

        assert schedule.every_seconds == 300
        assert schedule.daily_at is None

    def test_rejects_every_seconds_below_the_floor(self):
        with pytest.raises(ValidationError):
            Schedule(every_seconds=299)

    def test_accepts_a_valid_daily_at(self):
        schedule = Schedule(daily_at="06:00")

        assert schedule.daily_at == "06:00"
        assert schedule.every_seconds is None

    @pytest.mark.parametrize("value", ["24:00", "6:00", "06:60", "not-a-time"])
    def test_rejects_a_malformed_daily_at(self, value: str):
        with pytest.raises(ValidationError):
            Schedule(daily_at=value)

    def test_rejects_neither_field_set(self):
        with pytest.raises(ValidationError):
            Schedule()

    def test_rejects_both_fields_set(self):
        with pytest.raises(ValidationError):
            Schedule(every_seconds=600, daily_at="06:00")


class TestKnowledgeBaseEntrySchedule:
    """`KnowledgeBaseEntry.schedule` — the studio-only addition over engram's registry-member shape."""

    def test_defaults_to_none(self, tmp_path: Path):
        entry = KnowledgeBaseEntry(path=_make_knowledge_base(tmp_path))

        assert entry.schedule is None

    def test_carries_an_explicit_schedule(self, tmp_path: Path):
        entry = KnowledgeBaseEntry(path=_make_knowledge_base(tmp_path), schedule=Schedule(every_seconds=600))

        assert entry.schedule is not None
        assert entry.schedule.every_seconds == 600

    def test_still_rejects_a_bad_path_like_the_base_class(self, tmp_path: Path):
        with pytest.raises(ValidationError):
            KnowledgeBaseEntry(path=tmp_path / "does-not-exist")


class TestKnowledgeBaseEntryPrivacy:
    """A registered row's `private` key, the one key that keeps it out of a host's read over several knowledge bases."""

    def test_a_row_carrying_private_validates(self, tmp_path: Path):
        entry = KnowledgeBaseEntry.model_validate({"path": str(_make_knowledge_base(tmp_path)), "private": True})

        assert entry.private is True

    def test_a_row_carrying_neither_validates_as_shared(self, tmp_path: Path):
        assert KnowledgeBaseEntry.model_validate({"path": str(_make_knowledge_base(tmp_path))}).private is False

    def test_an_undeclared_library_key_leaves_private_as_declared(self, tmp_path: Path):
        entry = KnowledgeBaseEntry.model_validate({"path": str(_make_knowledge_base(tmp_path)), "private": True, "library": True})

        assert entry.private is True


class TestApplyStalenessMax:
    """The freshness check's threshold: default 0.0, bounded to [0, 1]."""

    def test_defaults_to_zero(self):
        assert TuningSettings().apply_staleness_max == 0.0

    def test_accepts_an_explicit_threshold(self):
        assert TuningSettings(apply_staleness_max=0.25).apply_staleness_max == pytest.approx(0.25)

    @pytest.mark.parametrize("value", [-0.1, 1.1])
    def test_rejects_out_of_range_values(self, value: float):
        with pytest.raises(ValidationError):
            TuningSettings(apply_staleness_max=value)


class TestSourceSettings:
    """`sources`, the raw per-name settings blocks, from the JSON file through the container into a connector's resources."""

    def test_an_explicit_empty_value_reads_as_no_blocks(self, tmp_path: Path) -> None:
        settings = ApplicationSettings(json_file=_write_app_json(tmp_path, _app_json()))

        assert settings.sources.root == {}

    def test_omitting_the_section_refuses_naming_it(self, tmp_path: Path) -> None:
        payload = _app_json()
        del payload["sources"]
        json_file = _write_app_json(tmp_path, payload)

        with pytest.raises(ValidationError, match="sources"):
            ApplicationSettings(json_file=json_file)

    def test_blocks_parse_from_the_json_source_verbatim(self, tmp_path: Path) -> None:
        blocks = {"zotero": {"collection": "Reading"}, "xai": {"api_key": "xai-secret"}}

        settings = ApplicationSettings(json_file=_write_app_json(tmp_path, _app_json(sources=blocks)))

        assert settings.sources.root == {"zotero": {"collection": "Reading"}, "xai": {"api_key": "xai-secret"}}

    def test_one_leaf_is_overridden_by_its_nested_env_variable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        blocks = {"zotero": {"collection": "Reading"}, "xai": {"api_key": "from-the-file", "model": "grok-4"}}
        json_file = _write_app_json(tmp_path, _app_json(sources=blocks))
        monkeypatch.setenv("APP_SOURCES__XAI__API_KEY", "from-the-environment")

        settings = ApplicationSettings(json_file=json_file)

        assert settings.sources.root == {"zotero": {"collection": "Reading"}, "xai": {"api_key": "from-the-environment", "model": "grok-4"}}

    async def test_the_blocks_reach_a_provider_bundle_unchanged_through_the_container(self, tmp_path: Path) -> None:
        """The app carries a block from the file to `SourceResources.settings` without reading a key of it."""
        knowledge_base_root = _make_knowledge_base(tmp_path / "kb")
        (knowledge_base_root / "knowledge-base-schema.json").write_text(research_spec().model_dump_json(), encoding="utf-8")
        blocks: dict[str, dict[str, JsonValue]] = {"zotero": {"collection": "Reading"}, "podcast": {"feed": "https://example.org/feed"}}
        settings = ApplicationSettings(json_file=_write_app_json(tmp_path, _app_json(sources=blocks)))

        container = create_container(settings, knowledge_base=KnowledgeBaseSettings(path=knowledge_base_root))
        resources = await container.get(SourceResources)

        assert resources.settings == blocks
